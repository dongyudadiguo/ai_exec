import base64
import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "vendor"
INPUT_FILE = ROOT / "input.json"
AE_FILE = ROOT / "ae.py"
RUNNER_PID_FILE = ROOT / ".ae_runner.pid"
HOST = "127.0.0.1"
PORT = int(os.environ.get("AE_VIEWER_PORT", "8765"))
_process = None
_process_lock = threading.Lock()
_send_lock = threading.Lock()
_state_cache = {"mtime": None, "messages": None, "model": "", "usage": None}
_state_cache_lock = threading.Lock()
_blob_cache = {}
TOOL_PREVIEW = int(os.environ.get("AE_TOOL_PREVIEW", "800"))
CONTEXT_LIMIT = int(os.environ.get("AE_CONTEXT_LIMIT", "128000"))


def read_input():
    return json.loads(INPUT_FILE.read_text(encoding="utf-8"))


def write_input(data):
    temp = INPUT_FILE.with_name(f"{INPUT_FILE.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, INPUT_FILE)


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _runner_pid_unlocked():
    if _process is not None and _process.poll() is None:
        return _process.pid
    try:
        pid = int(RUNNER_PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if _pid_alive(pid):
        return pid
    try:
        RUNNER_PID_FILE.unlink()
    except OSError:
        pass
    return None


def running():
    with _process_lock:
        return _runner_pid_unlocked() is not None


def pending_tool_progress(messages):
    """Return (done, total) for the latest assistant tool group, or None."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        call_ids = []
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if call_id:
                call_ids.append(call_id)
        if not call_ids:
            return 0, 0
        done = set()
        for item in messages[index + 1 :]:
            if item.get("role") != "tool":
                # A later non-tool message means this group is already complete history.
                return None
            call_id = item.get("tool_call_id")
            if call_id in call_ids:
                done.add(call_id)
        return len(done), len(call_ids)
    return None


def runner_phase(messages, is_running):
    """Classify runner wait state from transcript shape.

    ae.py flow while alive:
    - POST model request  -> waiting_ai
    - run each tool child -> waiting_tool
    - after all tool results are written, loop back to POST -> waiting_ai
    """
    if not is_running:
        return {
            "phase": "idle",
            "label": "空闲",
            "tool_done": None,
            "tool_total": None,
        }

    progress = pending_tool_progress(messages)
    if progress is not None:
        done, total = progress
        if total == 0 or done < total:
            label = "等待工具" if total == 0 else f"等待工具 {done}/{total}"
            return {
                "phase": "waiting_tool",
                "label": label,
                "tool_done": done,
                "tool_total": total,
            }
        return {
            "phase": "waiting_ai",
            "label": "等待 AI",
            "tool_done": done,
            "tool_total": total,
        }

    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
        return {
            "phase": "finishing",
            "label": "即将结束",
            "tool_done": None,
            "tool_total": None,
        }

    return {
        "phase": "waiting_ai",
        "label": "等待 AI",
        "tool_done": None,
        "tool_total": None,
    }


def agent_python():
    """Prefer pythonw so ae.py tool children (sys.executable -c) do not open consoles."""
    exe = Path(sys.executable)
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
        # common layout: .../Python3xx/python.exe
        sibling = exe.parent / "pythonw.exe"
        if sibling.exists():
            return str(sibling)
    return str(exe)


def noconsole_site_dir():
    return ROOT / "noconsole_site"


def prepend_pythonpath(env, path):
    """Ensure sitecustomize is importable for ae.py and every python -c tool child."""
    path = str(path)
    current = env.get("PYTHONPATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if path not in parts:
        env["PYTHONPATH"] = path + (os.pathsep + current if current else "")
    return env


def start_process():
    global _process
    with _process_lock:
        if _runner_pid_unlocked() is not None:
            return False
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        env = os.environ.copy()
        # Lets compaction children stop this runner without editing ae.py.
        env["AE_RUNNER"] = "1"
        # Patch subprocess in this process tree so tool calls do not flash consoles.
        prepend_pythonpath(env, noconsole_site_dir())
        # Prefer pythonw; also hide window if a console python is the only option.
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        _process = subprocess.Popen(
            [agent_python(), str(AE_FILE), str(INPUT_FILE)],
            cwd=str(ROOT),
            creationflags=creationflags,
            env=env,
            startupinfo=startupinfo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        RUNNER_PID_FILE.write_text(str(_process.pid), encoding="ascii")
        return True


_server = None


def shutdown_viewer():
    """Stop runner (if any) and shut down this viewer HTTP process (current port)."""
    stop_process()
    server = _server
    def _close():
        try:
            if server is not None:
                server.shutdown()
        finally:
            # Ensure process exits even if shutdown hangs on threads.
            os._exit(0)
    threading.Thread(target=_close, name="viewer-shutdown", daemon=True).start()
    return True


def stop_process():
    """Kill the ae.py runner tree. Returns True only if it looks stopped."""
    global _process
    with _process_lock:
        pid = _runner_pid_unlocked()
        if pid is None:
            _process = None
            return False

        stopped = False
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            stopped = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode == 0
            if not stopped and _process is not None and _process.poll() is None:
                try:
                    _process.terminate()
                except OSError:
                    pass
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                stopped = True
            except (ProcessLookupError, PermissionError):
                if _process is not None and _process.poll() is None:
                    try:
                        _process.terminate()
                        stopped = True
                    except OSError:
                        pass

        # taskkill may report failure even when the process is already gone.
        if not _pid_alive(pid):
            stopped = True

        if stopped:
            try:
                RUNNER_PID_FILE.unlink()
            except OSError:
                pass
            _process = None
        return stopped


def simple_token_count(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]", text))
    return cjk + latin


def usage_from_messages(messages):
    total = 0
    by_role = {}
    for message in messages:
        count = simple_token_count(message)
        total += count
        role = message.get("role", "unknown")
        by_role[role] = by_role.get(role, 0) + count
    return {"estimated_total": total, "by_role": by_role}


def file_part(part):
    filename = part.get_filename() or "attachment"
    raw = part.get_payload(decode=True) or b""
    mime = part.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if mime.startswith("image/"):
        encoded = base64.b64encode(raw).decode("ascii")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}
    try:
        text = raw.decode("utf-8")
        return {"type": "input_text", "text": f"附件 {filename}:\n{text}"}
    except UnicodeDecodeError:
        encoded = base64.b64encode(raw).decode("ascii")
        return {"type": "input_text", "text": f"附件 {filename} ({mime}, base64):\n{encoded}"}


def _user_item_text(item):
    """Extract editable plain text from a Responses API user input item."""
    content = item.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in ("input_text", "output_text", "text"):
            texts.append(str(part.get("text", "")))
        elif part_type == "refusal":
            texts.append(str(part.get("refusal", part.get("text", ""))))
    return "\n".join(t for t in texts if t)


def pop_last_user_message():
    """Remove the final user input item when it is still the last transcript item.

    Returns the editable plain text so the viewer can put it back into the composer.
    """
    data = read_input()
    body = data.setdefault("json", {})
    items = body.setdefault("input", [])
    if not items:
        raise ValueError("no messages to edit")
    last = items[-1]
    if last.get("role") != "user":
        raise ValueError("last message is not a user message")
    # Only allow retracting a pure user turn (no function_call/output after it).
    # Since we check the last input item, anything after would already mean it is not last.
    text = _user_item_text(last)
    items.pop()
    write_input(data)
    return text


def append_user_message(text, files):
    """Append a Responses API user input item."""
    data = read_input()
    body = data.setdefault("json", {})
    items = body.setdefault("input", [])
    parts = []
    if text.strip():
        parts.append({"type": "input_text", "text": text.strip()})
    parts.extend(files)
    if not parts:
        raise ValueError("message is empty")
    items.append({"role": "user", "content": parts})
    write_input(data)


def parse_multipart(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    header = f"Content-Type: {handler.headers.get('Content-Type', '')}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=default).parsebytes(header + body)
    text = ""
    files = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name == "message":
            raw = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset)
            except UnicodeDecodeError:
                text = raw.decode("utf-8", errors="replace")
        elif name == "files" and part.get_filename():
            files.append(file_part(part))
    return text, files



def _blob_url(data_url):
    key = str(abs(hash(data_url)))
    _blob_cache[key] = data_url
    return f"/api/blob?id={key}"


def response_transcript(body):
    """Convert Responses API input/output items to the viewer's chat-like shape."""
    result = []
    instructions = body.get("instructions")
    if instructions:
        result.append({"role": "system", "content": instructions})

    pending_calls = []

    def flush_calls():
        nonlocal pending_calls
        if pending_calls:
            result.append({"role": "assistant", "tool_calls": pending_calls})
            pending_calls = []

    for item in body.get("input", []):
        kind = item.get("type")
        if kind == "function_call":
            pending_calls.append({
                "id": item.get("call_id"),
                "function": {
                    "name": item.get("name", "python"),
                    "arguments": item.get("arguments", ""),
                },
            })
            continue

        flush_calls()
        if kind == "function_call_output":
            result.append({
                "role": "tool",
                "tool_call_id": item.get("call_id"),
                "content": item.get("output", ""),
            })
            continue

        role = item.get("role")
        if role:
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                for part in content:
                    part_type = part.get("type")
                    if part_type in ("input_text", "output_text", "text", "refusal"):
                        parts.append({"type": "text", "text": part.get("text", part.get("refusal", ""))})
                    elif part_type in ("input_image", "image_url"):
                        image_url = part.get("image_url")
                        if isinstance(image_url, dict):
                            image_url = image_url.get("url", "")
                        parts.append({"type": "image_url", "image_url": {"url": image_url or ""}})
                content = parts[0]["text"] if len(parts) == 1 and parts[0].get("type") == "text" else parts
            result.append({"role": role, "content": content})

    flush_calls()
    return result


def display_content(content):
    if isinstance(content, list):
        out = []
        for part in content:
            if part.get("type") == "image_url":
                p = dict(part)
                img = dict(p.get("image_url") or {})
                url = img.get("url", "")
                if url.startswith("data:image/"):
                    img["url"] = _blob_url(url)
                p["image_url"] = img
                out.append(p)
            else:
                out.append(part)
        return out
    if isinstance(content, str) and "data:image/" in content:
        return re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+", lambda m: _blob_url(m.group(0).replace("\n", "").replace("\r", "")), content)
    return content


def display_message(m):
    d = {"role": m.get("role", "message")}
    if "content" in m:
        d["content"] = display_content(m.get("content"))
    if m.get("role") == "tool":
        c = str(m.get("content", ""))
        d["content"] = c[:TOOL_PREVIEW] + (f"\n\n…… 已截断，完整长度 {len(c)} 字符" if len(c) > TOOL_PREVIEW else "")
        d["tool_content_length"] = len(c)
        d["tool_call_id"] = m.get("tool_call_id")
        # Prefer explicit markers / real tracebacks / ExceptionType: lines.
        # Avoid bare "error:" / "ERROR:" stdout false positives.
        d["tool_failed"] = bool(re.search(
            r"(?m)^\[tool_error\]|^Traceback \(most recent call last\):|^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Error(?:Group)?:|^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Exception:",
            c,
        ))
    if m.get("tool_calls"):
        d["tool_calls"] = [{"id": c.get("id"), "function": {"name": (c.get("function") or {}).get("name", "python")}} for c in m.get("tool_calls", [])]
    return d


def load_cached():
    st = INPUT_FILE.stat()
    mtime = st.st_mtime
    with _state_cache_lock:
        if _state_cache["mtime"] != mtime or _state_cache["messages"] is None:
            try:
                data = read_input()
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError):
                # ae.py rewrites input.json in place; a concurrent read can see a partial file.
                if _state_cache["messages"] is not None:
                    return _state_cache["mtime"], _state_cache["model"], _state_cache["messages"]
                raise
            body = data.get("json", {})
            messages = response_transcript(body)
            _state_cache.update({"mtime": mtime, "messages": messages, "model": body.get("model", ""), "usage": None})
        return mtime, _state_cache["model"], _state_cache["messages"]


def state_payload(light_if_unchanged=False, since=None, after=None):
    mtime, model, messages = load_cached()
    is_running = running()
    phase = runner_phase(messages, is_running)
    if light_if_unchanged and since is not None and mtime <= since:
        return {
            "unchanged": True,
            "running": is_running,
            "updated": mtime,
            "count": len(messages),
            **phase,
        }
    reset = after is None or after < 0 or after > len(messages)
    selected = messages if reset else messages[after:]
    return {
        "running": is_running,
        "model": model,
        "messages": [display_message(m) for m in selected],
        "updated": mtime,
        "count": len(messages),
        "offset": 0 if reset else after,
        "reset": reset,
        **phase,
    }


def usage_payload():
    mtime, model, messages = load_cached()
    with _state_cache_lock:
        if _state_cache.get("usage") is None:
            _state_cache["usage"] = usage_from_messages(messages)
        usage = _state_cache["usage"]
    return {"updated": mtime, "usage": usage, "context_limit": CONTEXT_LIMIT}


def tool_output_payload(call_id):
    _, _, messages = load_cached()
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            return {"call_id": call_id, "output": str(message.get("content", ""))}
    return None


PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>input.json 查看器</title>
<style>
:root{color-scheme:light;--bg:#f7f7f4;--panel:#ffffff;--text:#202124;--muted:#62676f;--line:#d8dadd;--accent:#0f766e;--danger:#b42318;--tool:#eef2f6;--user:#e8f3ee;--assistant:#fff;--system:#f4efe6}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;overflow-x:hidden}.app{min-height:100vh;padding:16px}.messages{max-width:980px;margin:0 auto;display:flex;flex-direction:column;gap:10px;width:100%;min-width:0}.message-list{display:flex;flex-direction:column;gap:10px;min-width:0}.model-message .content{font-weight:600}.model-message .role{text-transform:none}.runner-status{display:flex;align-items:center;gap:8px;padding:8px 4px;color:var(--muted);font-size:13px}.status-spinner{width:14px;height:14px;border:2px solid #cbd2d9;border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}.msg{border:0;background:var(--panel);border-radius:12px;padding:10px 12px;max-width:100%;overflow:hidden}.msg.user{background:var(--user)}.msg.system{background:var(--system)}.msg.assistant{background:transparent;padding-left:0;padding-right:0}.role{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;margin-bottom:6px;text-transform:uppercase;letter-spacing:0}.content{overflow-wrap:anywhere;word-break:break-word;max-width:100%;min-width:0;overflow-x:auto}.content p{margin:0 0 8px}.content p:last-child{margin-bottom:0}.content h1,.content h2,.content h3,.content h4,.content h5,.content h6{margin:10px 0 6px;line-height:1.25}.content h1{font-size:22px}.content h2{font-size:18px}.content h3{font-size:16px}.content h4{font-size:15px}.content h5{font-size:14px}.content h6{font-size:13px;color:var(--muted)}.content ul,.content ol{margin:6px 0 8px 22px;padding:0}.content li>ul,.content li>ol{margin-top:4px;margin-bottom:4px}.content table{border-collapse:collapse;margin:8px 0;width:max-content;max-width:100%;display:block;overflow:auto}.content th,.content td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}.content th{background:#f1f3f5;font-weight:650}.content tr:nth-child(even) td{background:#fafafa}.content blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--line);background:#fafafa;color:#4f5660}.content pre{margin:8px 0;padding:10px;overflow:auto;background:#1f2328;color:#f6f8fa;border-radius:6px}.content code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}.content :not(pre)>code{background:#eef2f6;border-radius:4px;padding:1px 4px}.content img{display:block;max-width:100%;width:auto;height:auto;max-height:min(520px,70vh);border:1px solid var(--line);border-radius:6px;margin:8px 0;background:#fff}.content a{color:var(--accent);text-decoration:underline;text-underline-offset:2px}.content a:hover{opacity:.85}.content del{text-decoration:line-through;color:#6b7280}.content hr{border:0;border-top:1px solid var(--line);margin:12px 0}.content .task-list-item,.content li.task-list-item,.content li:has(>input[type=checkbox]){list-style:none;margin-left:-1.2em}.content .task-list-item input[type=checkbox],.content li input[type=checkbox]{margin-right:6px;vertical-align:middle}.content pre code[class*=language-]{font-family:inherit}.content li>p{margin:0 0 4px}.content li>p:last-child{margin-bottom:0}.content ul.contains-task-list,.content ol.contains-task-list{margin-left:8px}.content th[align=center],.content td[align=center]{text-align:center}.content th[align=right],.content td[align=right]{text-align:right}.content th[align=left],.content td[align=left]{text-align:left}.tool-group{border:0;background:transparent;border-radius:0;color:#6b7280;animation:messageIn .2s ease-out}.tool-group.running{border:0}.tool-group.has-failure{border:0;background:transparent}.tool-group summary{display:flex;align-items:center;gap:6px;min-height:28px;padding:2px 0;cursor:pointer;user-select:none;list-style:none}.tool-group summary::-webkit-details-marker{display:none}.tool-group summary:before{content:"›";font-size:18px;line-height:1;color:#7a828c;transition:transform .18s}.tool-group[open] summary:before{transform:rotate(90deg)}.tool-title{font-weight:500;color:#6b7280}.tool-meta{margin-left:0;color:var(--muted);font-size:12px}.tool-duration{color:#7a828c;font-size:12px;font-variant-numeric:tabular-nums}.tool-events{border-top:0;padding:2px 0 4px 14px}.tool-group[open] .tool-events{animation:reveal .18s ease-out}.tool-event{display:grid;grid-template-columns:minmax(80px,auto) minmax(0,1fr) auto auto;align-items:center;gap:10px;min-width:0;padding:5px 0;font-size:12px;border-bottom:1px solid rgba(0,0,0,.05)}.tool-event-duration{color:#7a828c;font-size:12px;font-variant-numeric:tabular-nums;min-width:3.5em;text-align:right}.tool-event:last-child{border-bottom:0}.tool-name{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:#38414a}.tool-state{text-align:right;color:#7a828c}.tool-event.done .tool-state{color:#537066}.tool-event.failed .tool-state{color:#7a828c;font-weight:400}.tool-output-button{border:0;background:transparent;color:var(--accent);font:inherit;cursor:pointer;padding:2px 4px}.tool-output{grid-column:1/-1;width:100%;max-width:100%;min-width:0;max-height:320px;overflow:auto;margin:3px 0 5px!important;font-size:12px!important;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}.tool-output.loading{color:#c8d0d8}.composer{max-width:980px;margin:18px auto 0;position:relative;padding:0 0 44px}.composer-inner{display:block}.usage-direct{position:absolute;right:0;bottom:0;margin:0;text-align:right;color:var(--muted);font-size:12px;min-height:0;width:auto;pointer-events:none;line-height:1.3}.token-track{width:120px;height:3px;margin:3px 0 0 auto;background:#dde1e4;border-radius:99px;overflow:hidden}.token-bar{width:0;height:100%;background:var(--accent);border-radius:inherit;transition:width .35s ease,background-color .25s}.token-bar.warn{background:#d18b16}.token-bar.danger{background:var(--danger)}.drop{border:0;background:transparent;border-radius:0;padding:0;min-height:72px}.drop.drag{outline:none;box-shadow:inset 0 0 0 2px var(--accent);border-radius:10px;padding:8px}textarea{width:100%;min-height:72px;max-height:none;height:auto;resize:none;border:0;outline:0;font:inherit;background:transparent;color:var(--text);padding:0 0 40px 0;line-height:1.55}textarea::placeholder{color:#9aa3af}.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.file{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:#fafafa}.file button{border:0;background:transparent;cursor:pointer;color:var(--muted)}button.run{position:absolute;right:0;bottom:28px;height:34px;border:0;border-radius:10px;padding:0 14px;background:var(--accent);color:#fff;font-weight:650;cursor:pointer;min-width:84px;z-index:2}button.stop{background:var(--danger)}.msg.user.editable-last{cursor:pointer}.msg.user.editable-last:hover{box-shadow:inset 0 0 0 1px rgba(15,118,110,.28)}.msg.user.editable-last:active{transform:scale(.997)}.hidden{display:none}.empty{max-width:980px;margin:40px auto;color:var(--muted);text-align:center}.msg{animation:messageIn .2s ease-out}.new-messages{position:fixed;right:20px;bottom:20px;z-index:3;border:1px solid var(--line);border-radius:999px;padding:8px 13px;background:#fff;color:var(--text);box-shadow:0 6px 24px rgba(0,0,0,.14);cursor:pointer;animation:popIn .18s ease-out}.run{transition:transform .12s,background-color .2s,opacity .2s}.run:active{transform:scale(.97)}@keyframes spin{to{transform:rotate(360deg)}}@keyframes messageIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}@keyframes reveal{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}@keyframes popIn{from{opacity:0;transform:translateY(6px) scale(.96)}to{opacity:1;transform:none}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}@media(max-width:700px){.app{padding-left:10px;padding-right:10px}.composer-inner{display:block}}
.model-message{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;background:transparent !important;box-shadow:none !important;padding:4px 2px !important}.model-message .model-meta{min-width:0;flex:1}.model-message .content{font-weight:600}.model-message .role{text-transform:none}.kill-process{flex:0 0 auto;border:1px solid #e5e7eb;background:#fff;color:#374151;border-radius:999px;padding:5px 10px;font:12px/1.2 system-ui,-apple-system,"Segoe UI",sans-serif;font-weight:600;cursor:pointer;transition:background-color .15s ease,color .15s ease,border-color .15s ease,opacity .15s ease,transform .12s ease}.kill-process:hover{background:#111827;color:#fff;border-color:#111827}.kill-process:active{transform:scale(.97)}.kill-process:disabled{opacity:.65;cursor:wait}
</style>
</head>
<body>
<main class="app">
  <section class="messages">
    <article class="msg model-message"><div class="model-meta"><div class="role">模型</div><div id="model" class="content">model</div></div><button id="killProcess" class="kill-process" type="button" title="关闭当前端口的查看器进程">关闭查看器</button></article>
    <div id="messages" class="message-list"></div>
    <div id="runnerStatus" class="runner-status hidden"><span class="status-spinner"></span><span id="runnerLabel">正在运行…</span></div>
    <div id="empty" class="empty hidden">暂无消息</div>
  </section>
  <form id="composer" class="composer"><div class="composer-inner"><div id="drop" class="drop"><textarea id="message" name="message" placeholder="输入消息，或拖入/粘贴文件、图片；留空可直接运行"></textarea><div id="files" class="files"></div><input id="fileInput" name="files" type="file" multiple class="hidden"></div><button id="run" class="run" type="submit">运行</button></div><div class="usage-direct"><div id="usageText">Token：计算中…</div><div class="token-track"><div id="tokenBar" class="token-bar"></div></div></div></form>
</main>
<button id="newMessages" class="new-messages hidden" type="button">↓ 新消息</button>
<script src="/vendor/marked.min.js"></script>
<script src="/vendor/purify.min.js"></script>
<script>
const messagesEl=document.getElementById('messages'), emptyEl=document.getElementById('empty'), composer=document.getElementById('composer'), runBtn=document.getElementById('run'), msgInput=document.getElementById('message'), fileInput=document.getElementById('fileInput'), drop=document.getElementById('drop'), filesEl=document.getElementById('files'), usageText=document.getElementById('usageText'), tokenBar=document.getElementById('tokenBar'), runnerStatus=document.getElementById('runnerStatus'), runnerLabel=document.getElementById('runnerLabel'), newMessagesBtn=document.getElementById('newMessages'), killProcessBtn=document.getElementById('killProcess');
let selectedFiles=[], isRunning=false, phaseLabel='空闲', lastUpdated=0, messageCount=0, usageLoaded=false, usageLoading=false, unseenMessages=0, firstPaint=true, editInFlight=false;
let pollTimer=null, pollInFlight=false, pollQueued=false, pollGeneration=0;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeUrl=u=>{
  const s=String(u??'').trim();
  if(!s) return '';
  if(/^(https?:|mailto:|tel:|#|\/|\.\/|\.\.\/)/i.test(s)) return s;
  if(/^data:image\/[a-z0-9.+-]+;base64,/i.test(s)) return s;
  return '';
};
// marked + DOMPurify from /vendor. Fallback is escaped plain text.
const mdReady=(()=>{
  try{
    if(typeof marked==='undefined' || typeof DOMPurify==='undefined') return false;
    const lib=marked; // UMD build exposes {parse,setOptions,Renderer,...}
    const renderer=new lib.Renderer();
    // marked v11 classic signature: link(href, title, text) / image(href, title, text)
    renderer.link=function(href, title, text){
      if(href && typeof href==='object'){
        const tok=href;
        const body=(tok.tokens && this.parser?.parseInline)?this.parser.parseInline(tok.tokens):esc(String(tok.text||''));
        const safe=safeUrl(tok.href);
        if(!safe) return body;
        const t=tok.title?` title="${esc(tok.title)}"`:'';
        const ext=/^https?:/i.test(safe);
        return `<a href="${esc(safe)}"${t}${ext?' target="_blank" rel="noreferrer noopener"':''}>${body}</a>`;
      }
      const safe=safeUrl(href);
      if(!safe) return text||'';
      const t=title?` title="${esc(title)}"`:'';
      const ext=/^https?:/i.test(safe);
      return `<a href="${esc(safe)}"${t}${ext?' target="_blank" rel="noreferrer noopener"':''}>${text}</a>`;
    };
    renderer.image=function(href, title, text){
      if(href && typeof href==='object'){
        const tok=href; href=tok.href; title=tok.title; text=tok.text;
      }
      const safe=safeUrl(href);
      if(!safe) return esc(text||'');
      const t=title?` title="${esc(title)}"`:'';
      return `<img src="${esc(safe)}" alt="${esc(text||'')}"${t} loading="lazy">`;
    };
    lib.setOptions({
      gfm:true,
      breaks:false,
      pedantic:false,
      renderer,
      headerIds:false,
      mangle:false
    });
    return {lib, purify:DOMPurify};
  }catch(err){
    console.warn('markdown libs init failed', err);
    return false;
  }
})();
function markdown(text){
  const raw=String(text??'');
  if(!raw) return '';
  if(mdReady){
    try{
      const html=mdReady.lib.parse(raw);
      // Allow common markdown tags/attrs; strip scripts/handlers.
      return mdReady.purify.sanitize(html, {
        USE_PROFILES:{html:true},
        ADD_TAGS:['input'],
        ADD_ATTR:['target','rel','align','checked','disabled','type','start','colspan','rowspan','loading','class'],
      });
    }catch(err){
      console.warn('marked parse failed', err);
    }
  }
  return `<p>${esc(raw).replace(/\n\n+/g,'</p><p>').replace(/\n/g,'<br>')}</p>`;
}
function contentHtml(content){
  if(Array.isArray(content)) return content.map(part=>part.type==='image_url'?imageHtml(part.image_url?.url):`<div class="content">${markdown(part.text||JSON.stringify(part,null,2))}</div>`).join('');
  const text=String(content??'');
  const dataImgs=[...text.matchAll(/data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+/g)].map(m=>m[0].replace(/\s/g,''));
  const cleaned=text.replace(/data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+/g,'[图片]');
  let out=`<div class="content">${markdown(cleaned)}</div>`;
  if(dataImgs.length) out+=dataImgs.map(imageHtml).join('');
  return out;
}
function imageHtml(src){return `<img src="${esc(src)}" alt="attached image" loading="lazy">`;}
function hasVisibleContent(content){
  if(content==null) return false;
  if(Array.isArray(content)) return content.some(part=>part?.type==='image_url' || String(part?.text??'').trim());
  return String(content).trim().length>0;
}
function messageHtml(m){
  if(m.role==='tool' || !hasVisibleContent(m.content)) return '';
  const role=m.role||'message';
  const roleHtml=role==='assistant'?'':`<div class="role"><span>${esc(role)}</span></div>`;
  return `<article class="msg ${esc(role)}">${roleHtml}${contentHtml(m.content)}</article>`;
}
function newToolGroup(){
  messagesEl.insertAdjacentHTML('beforeend','<details class="tool-group"><summary><span class="tool-title">工具活动</span><span class="tool-meta"></span><span class="tool-duration"></span></summary><div class="tool-events"></div></details>');
  return messagesEl.lastElementChild;
}
function matchingToolEvent(group,id){
  if(!id)return null;
  return [...group.querySelectorAll('.tool-event')].find(el=>el.dataset.callId===String(id))||null;
}
function durationText(ms){return ms<1000?`${Math.max(0,Math.round(ms))}ms`:`${(ms/1000).toFixed(ms<10000?1:0)}s`}
function refreshToolGroup(group){
  const events=[...group.querySelectorAll('.tool-event')], done=events.filter(el=>el.classList.contains('done')).length, failed=events.filter(el=>el.classList.contains('failed')).length;
  const names={};
  events.forEach(el=>{const n=el.dataset.toolName||'tool';names[n]=(names[n]||0)+1});
  const nameText=Object.entries(names).slice(0,2).map(([n,c])=>c>1?`${n} ×${c}`:n).join('、')+(Object.keys(names).length>2?' 等':'');
  const complete=events.length>0&&events.length===done;
  group.classList.toggle('running',!complete);
  group.classList.toggle('has-failure',failed>0);
  if(!group.dataset.started)group.dataset.started=String(Date.now());
  if(complete&&!group.dataset.ended)group.dataset.ended=String(Date.now());
  if(!complete)delete group.dataset.ended;
  const status=failed?`${events.length} 次 · ${failed} 失败`:complete?`${events.length} 次 · 已完成`:`${events.length} 次 · 返回 ${done}/${events.length}`;
  const elapsed=Number(group.dataset.ended||Date.now())-Number(group.dataset.started);
  const dur=elapsed>=300?durationText(elapsed):'';
  const head=nameText?`工具活动 · ${nameText}`:'工具活动';
  group.querySelector('.tool-title').textContent=dur?`${head}  ${status}  ${dur}`:`${head}  ${status}`;
  group.querySelector('.tool-meta').textContent='';
  group.querySelector('.tool-duration').textContent='';
  if(failed&&!firstPaint)group.open=true;
}
function addToolActivity(group,m){
  const box=group.querySelector('.tool-events');
  if(m.tool_calls?.length){
    m.tool_calls.forEach(call=>{
      const id=String(call.id||''), name=String(call.function?.name||'python');
      if(matchingToolEvent(group,id))return;
      const row=document.createElement('div');
      row.className='tool-event';row.dataset.callId=id;row.dataset.toolName=name;
      row.innerHTML=`<span class="tool-name">${esc(name)}</span><span class="tool-state">等待结果</span><span class="tool-event-duration"></span><button class="tool-output-button hidden" type="button">查看输出</button>`;
      row.dataset.started=String(Date.now());
      box.appendChild(row);
    });
  }
  if(m.role==='tool'){
    const id=String(m.tool_call_id||'');
    let row=matchingToolEvent(group,id);
    if(!row){
      row=document.createElement('div');row.className='tool-event';row.dataset.callId=id;row.dataset.toolName='tool';
      row.innerHTML='<span class="tool-name">tool</span><span class="tool-state"></span><span class="tool-event-duration"></span><button class="tool-output-button hidden" type="button">查看输出</button>';
      row.dataset.started=String(Date.now());
      box.appendChild(row);
    }
    row.classList.add('done');
    row.classList.toggle('failed',!!m.tool_failed);
    if(!row.dataset.started)row.dataset.started=String(Date.now());
    if(!row.dataset.ended)row.dataset.ended=String(Date.now());
    const length=m.tool_content_length??String(m.content||'').length;
    row.querySelector('.tool-state').textContent=m.tool_failed?`执行失败 · ${length} 字符`:`已返回 · ${length} 字符`;
    row.querySelector('.tool-output-button').classList.remove('hidden');
    const oneElapsed=Number(row.dataset.ended)-Number(row.dataset.started);
    const oneDur=row.querySelector('.tool-event-duration');
    if(oneDur)oneDur.textContent=durationText(Math.max(0,oneElapsed));
  }
  refreshToolGroup(group);
}
function updateToolDurations(){
  document.querySelectorAll('.tool-group.running').forEach(refreshToolGroup);
  document.querySelectorAll('.tool-event:not(.done)').forEach(row=>{
    if(!row.dataset.started)row.dataset.started=String(Date.now());
    const elapsed=Date.now()-Number(row.dataset.started);
    const el=row.querySelector('.tool-event-duration');
    if(el)el.textContent=elapsed>=100?durationText(elapsed):'';
  });
}
setInterval(updateToolDurations,100);
function appendMessageBatch(msgs){
  for(const m of msgs){
    // A message may contain useful prose and tool calls; prose remains a normal card.
    const body=messageHtml(m);
    if(body){
      messagesEl.insertAdjacentHTML('beforeend',body);
      const last=messagesEl.lastElementChild;
      if(last && m.role==='user'){
        last.dataset.editText=plainTextFromContent(m.content);
      }
    }
    const isActivity=m.role==='tool' || !!m.tool_calls?.length;
    if(isActivity){
      let group=messagesEl.lastElementChild;
      if(!group?.classList.contains('tool-group'))group=newToolGroup();
      addToolActivity(group,m);
    }
  }
}
function updateRunLabel(){
  runBtn.textContent=isRunning?'停止':(msgInput.value.trim()||selectedFiles.length?'发送并运行':'继续运行');
}
function setRunningUi(){
  runnerStatus.classList.toggle('hidden',!isRunning);
  runnerLabel.textContent=phaseLabel||'正在运行…';
  runBtn.classList.toggle('stop',isRunning);
  // Heal stuck disabled state after stop; submit handler manages its own disable window.
  if(!isRunning && runBtn.textContent!=='结束中…') runBtn.disabled=false;
  updateRunLabel();
}
function nearBottom(){return window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-140}
function hideNewMessages(){unseenMessages=0;newMessagesBtn.classList.add('hidden')}
function showNewMessages(amount){unseenMessages+=Math.max(1,amount);newMessagesBtn.textContent=`↓ ${unseenMessages} 项新动态`;newMessagesBtn.classList.remove('hidden')}
function afterMessageUpdate(wasNear,added,reset=false){
  requestAnimationFrame(()=>{
    if(firstPaint||wasNear){window.scrollTo({top:document.documentElement.scrollHeight,behavior:firstPaint?'auto':'smooth'});hideNewMessages()}
    else if(added)showNewMessages(added);
    firstPaint=false;
    refreshEditableLastUser();
  });
}
function plainTextFromContent(content){
  if(content==null) return '';
  if(typeof content==='string') return content;
  if(Array.isArray(content)){
    return content.map(part=>{
      if(!part) return '';
      if(typeof part==='string') return part;
      if(part.type==='image_url') return '';
      return String(part.text??'');
    }).filter(Boolean).join('\n');
  }
  return String(content);
}
function refreshEditableLastUser(){
  messagesEl.querySelectorAll('.msg.user.editable-last').forEach(el=>{
    el.classList.remove('editable-last');
    el.removeAttribute('title');
  });
  // Only the true last DOM child may be edited. Tool groups sit after the user
  // card when tools are in flight without assistant prose — those must not look editable.
  const last=messagesEl.lastElementChild;
  if(!last || !last.classList.contains('msg') || !last.classList.contains('user')) return;
  last.classList.add('editable-last');
  last.title='双击以撤回并编辑这条消息';
  if(!last.dataset.editText){
    const content=last.querySelectorAll('.content');
    last.dataset.editText=[...content].map(node=>node.innerText||node.textContent||'').join('\n').trim();
  }
}
function requestFullResync(){
  // Invalidate in-flight incremental polls so a partial window cannot wipe history.
  pollGeneration++;
  messageCount=0;
  lastUpdated=0;
  schedulePoll(0);
}
async function editLastUserMessage(el){
  if(!el || !el.classList.contains('editable-last')) return;
  if(editInFlight) return;
  editInFlight=true;
  const fallback=el.dataset.editText||'';
  try{
    const r=await fetch('/api/retract-last-user',{method:'POST'});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json();
    const text=(data && typeof data.text==='string')?data.text:fallback;
    // Put the retracted user message back into the composer for editing.
    msgInput.value=text;
    localStorage.setItem('ae-draft', msgInput.value);
    resizeComposer();
    updateRunLabel();
    msgInput.focus();
    const len=msgInput.value.length;
    try{msgInput.setSelectionRange(len,len)}catch(e){}
    // Force full refresh so the retracted user bubble disappears
    requestFullResync();
  }catch(err){
    alert(err.message||'无法编辑该消息');
  }finally{
    editInFlight=false;
  }
}
function applyMessages(data){
  const msgs=data.messages||[], count=data.count??messageCount, offset=data.offset??0, wasNear=nearBottom();
  // Full replace only when the server sent a full window (reset / offset 0).
  // Do NOT treat local messageCount===0 as "this payload is complete" — a stale
  // incremental response could otherwise wipe earlier bubbles.
  if(data.reset || offset===0){
    messagesEl.innerHTML='';appendMessageBatch(msgs);messageCount=count;afterMessageUpdate(wasNear,msgs.length,true);return;
  }
  if(offset===messageCount){
    if(msgs.length)appendMessageBatch(msgs);messageCount=count;afterMessageUpdate(wasNear,msgs.length);return;
  }
  if(offset<messageCount && offset+msgs.length>=messageCount){
    const fresh=msgs.slice(messageCount-offset);
    if(fresh.length)appendMessageBatch(fresh);messageCount=count;afterMessageUpdate(wasNear,fresh.length);return;
  }
  requestFullResync();
}
function render(data){
  isRunning=!!data.running;
  phaseLabel=data.label||(isRunning?'运行中':'空闲');
  document.getElementById('model').textContent=data.model||'model';
  setRunningUi();
  applyMessages(data);
  emptyEl.classList.toggle('hidden', messageCount>0);
  if(!usageLoaded)loadUsage();
}
function schedulePoll(delay){
  // If a poll is already in flight, just ask for another pass after it finishes.
  // This avoids try{schedulePoll(0);return} being overwritten by finally's delayed schedule.
  if(pollInFlight){pollQueued=true;return;}
  if(pollTimer!=null) clearTimeout(pollTimer);
  pollTimer=setTimeout(poll, delay);
}
async function poll(){
  if(pollInFlight){pollQueued=true;return;}
  pollInFlight=true;
  pollTimer=null;
  const gen=pollGeneration;
  const reqAfter=messageCount;
  const reqSince=lastUpdated;
  try{
    const r=await fetch('/api/state?since='+encodeURIComponent(reqSince)+'&after='+encodeURIComponent(reqAfter));
    const data=await r.json();
    if(gen!==pollGeneration){
      // Stale response after requestFullResync / transcript rewrite — drop it.
      return;
    }
    if(data.unchanged){
      isRunning=!!data.running;
      phaseLabel=data.label||(isRunning?'运行中':'空闲');
      if(data.count!=null && data.count<messageCount){
        // Transcript shrank (compaction): full resync.
        requestFullResync();
        return;
      }
      if(data.count!=null) messageCount=data.count;
      setRunningUi();
    }else{
      lastUpdated=data.updated||0;
      usageLoaded=false;
      render(data);
    }
  }catch(e){
    if(gen===pollGeneration) usageText.textContent='Token：连接失败';
  }finally{
    pollInFlight=false;
    if(pollQueued || gen!==pollGeneration){
      pollQueued=false;
      schedulePoll(0);
    }else{
      schedulePoll(isRunning?500:1800);
    }
  }
}
function addFiles(files){const incoming=[...files].filter(Boolean);if(incoming.length){selectedFiles.push(...incoming);refreshFiles();updateRunLabel()}}
function refreshFiles(){filesEl.innerHTML=selectedFiles.map((f,i)=>`<span class="file">${esc(f.name)} <button type="button" data-i="${i}">x</button></span>`).join('');updateRunLabel()}
function resizeComposer(){msgInput.style.height='auto';msgInput.style.height=Math.max(72,msgInput.scrollHeight)+'px'}
drop.addEventListener('click',e=>{if(e.target===drop)fileInput.click()});
drop.addEventListener('paste',e=>{const files=[...e.clipboardData.files];if(files.length){e.preventDefault();addFiles(files)}});
fileInput.addEventListener('change',()=>{addFiles(fileInput.files);fileInput.value=''});
filesEl.addEventListener('click',e=>{if(e.target.dataset.i!==undefined){selectedFiles.splice(Number(e.target.dataset.i),1);refreshFiles()}});
for(const event of ['dragenter','dragover'])document.addEventListener(event,e=>{if([...e.dataTransfer.types].includes('Files')){e.preventDefault();drop.classList.add('drag')}});
for(const event of ['dragleave','drop'])document.addEventListener(event,e=>{if(event==='drop'&&e.dataTransfer?.files?.length){e.preventDefault();addFiles(e.dataTransfer.files)}drop.classList.remove('drag')});
msgInput.value=localStorage.getItem('ae-draft')||'';resizeComposer();updateRunLabel();
msgInput.addEventListener('input',()=>{localStorage.setItem('ae-draft',msgInput.value);resizeComposer();updateRunLabel()});
msgInput.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&!isRunning){e.preventDefault();composer.requestSubmit()}});
document.addEventListener('keydown',async e=>{if(e.key==='Escape'&&isRunning){e.preventDefault();await stopRunner(runBtn)}});
newMessagesBtn.addEventListener('click',()=>{window.scrollTo({top:document.documentElement.scrollHeight,behavior:'smooth'});hideNewMessages()});
window.addEventListener('scroll',()=>{if(nearBottom())hideNewMessages()},{passive:true});
messagesEl.addEventListener('dblclick',e=>{
  const card=e.target.closest('article.msg.user.editable-last');
  if(!card) return;
  // Ignore double-clicks on interactive children if any appear later
  if(e.target.closest('button,a,summary,input,textarea')) return;
  e.preventDefault();
  editLastUserMessage(card);
});
messagesEl.addEventListener('click',async e=>{
  const button=e.target.closest('.tool-output-button');if(!button)return;
  const row=button.closest('.tool-event');let out=row.querySelector('.tool-output');
  if(out){out.classList.toggle('hidden');button.textContent=out.classList.contains('hidden')?'查看输出':'收起输出';return}
  button.disabled=true;button.textContent='加载中…';
  try{const r=await fetch('/api/tool-output?id='+encodeURIComponent(row.dataset.callId));if(!r.ok)throw new Error();const data=await r.json();out=document.createElement('pre');out.className='tool-output';out.textContent=data.output||'(无输出)';row.appendChild(out);button.textContent='收起输出'}catch(err){button.textContent='加载失败'}finally{button.disabled=false}
});
async function loadUsage(){
  if(usageLoaded||usageLoading)return;usageLoading=true;let stale=false;usageText.textContent='Token：计算中…';
  try{const r=await fetch('/api/usage');if(!r.ok)throw new Error();const data=await r.json(), total=Number(data.usage?.estimated_total||0), limit=Number(data.context_limit||0), pct=limit?Math.round(total/limit*100):0;
    usageText.textContent=limit?`估算 Token：${total.toLocaleString()} / ${limit.toLocaleString()} · ${pct}%`:`估算 Token：${total.toLocaleString()}`;
    tokenBar.style.width=Math.min(100,pct)+'%';tokenBar.classList.toggle('warn',pct>=60&&pct<85);tokenBar.classList.toggle('danger',pct>=85);usageText.title=pct>=85?'上下文接近上限，建议压缩历史消息':'';stale=Number(data.updated||0)<lastUpdated;usageLoaded=!stale
  }catch(e){usageText.textContent='Token：获取失败'}finally{usageLoading=false;if(stale)setTimeout(loadUsage,0)}
}
async function stopRunner(btn){
  if(!isRunning)return;
  if(btn){btn.disabled=true;btn.textContent='结束中…'}
  try{
    await fetch('/api/stop',{method:'POST'});
  }finally{
    // Always re-enable: setRunningUi only updates labels, not disabled.
    if(btn) btn.disabled=false;
    isRunning=false;
    phaseLabel='空闲';
    setRunningUi();
    schedulePoll(0);
  }
}
async function shutdownViewer(){
  if(!killProcessBtn)return;
  killProcessBtn.disabled=true;
  killProcessBtn.textContent='关闭中…';
  try{
    await fetch('/api/shutdown',{method:'POST'});
  }catch(err){
    /* server may close connection mid-request */
  }
  killProcessBtn.textContent='已关闭';
  try{window.close()}catch(e){}
  document.body.innerHTML='<div style="display:grid;place-items:center;min-height:100vh;font:15px/1.5 system-ui,sans-serif;color:#6b7280">查看器已关闭</div>';
}
if(killProcessBtn)killProcessBtn.addEventListener('click',()=>shutdownViewer());
runBtn.addEventListener('click',async e=>{if(isRunning){e.preventDefault();await stopRunner(runBtn)}});
composer.addEventListener('submit',async e=>{
  e.preventDefault();if(isRunning)return;const submitted=msgInput.value,fd=new FormData();fd.append('message',submitted);selectedFiles.forEach(f=>fd.append('files',f,f.name));runBtn.disabled=true;
  try{const r=await fetch('/api/send',{method:'POST',body:fd});if(!r.ok)throw new Error(await r.text());if(msgInput.value===submitted){msgInput.value='';localStorage.removeItem('ae-draft');resizeComposer()}selectedFiles=[];refreshFiles();isRunning=true;phaseLabel='等待 AI';setRunningUi();schedulePoll(0)}catch(err){alert(err.message||'运行失败')}finally{runBtn.disabled=false}
});
schedulePoll(0);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if sys.stderr:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            qs = parse_qs(parsed.query)
            since = None
            after = None
            try:
                since = float(qs.get("since", [""])[0])
            except ValueError:
                pass
            try:
                after = int(qs.get("after", [""])[0])
            except ValueError:
                pass
            return self.send_json(state_payload(light_if_unchanged=True, since=since, after=after))
        if path == "/api/usage":
            return self.send_json(usage_payload())
        if path == "/api/tool-output":
            call_id = parse_qs(parsed.query).get("id", [""])[0]
            payload = tool_output_payload(call_id)
            return self.send_json(payload) if payload is not None else self.send_error(404)
        if path == "/api/blob":
            key = parse_qs(parsed.query).get("id", [""])[0]
            data_url = _blob_cache.get(key)
            if not data_url:
                return self.send_error(404)
            m = re.match(r"data:([^;]+);base64,(.*)", data_url, re.S)
            if not m:
                return self.send_error(400)
            raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
            self.send_response(200)
            self.send_header("Content-Type", m.group(1))
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in ("/", "/index.html"):
            return self.send_text(PAGE, content_type="text/html; charset=utf-8")
        if path.startswith("/vendor/"):
            name = path[len("/vendor/"):]
            fp = (VENDOR_DIR / name).resolve()
            try:
                fp.relative_to(VENDOR_DIR.resolve())
            except ValueError:
                return self.send_error(404)
            if not fp.is_file():
                return self.send_error(404)
            raw = fp.read_bytes()
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            if fp.suffix == ".js":
                ctype = "application/javascript; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/send":
                if running():
                    return self.send_text("process is already running", 409)
                text, files = parse_multipart(self)
                with _send_lock:
                    if running():
                        return self.send_text("process is already running", 409)
                    appended = bool(text.strip() or files)
                    if appended:
                        append_user_message(text, files)
                    if not start_process():
                        return self.send_text("process could not be started", 409)
                return self.send_json({"ok": True, "message_appended": appended})
            if path == "/api/stop":
                with _send_lock:
                    stopped = stop_process()
                return self.send_json({"stopped": stopped})
            if path == "/api/retract-last-user":
                with _send_lock:
                    if running():
                        stop_process()
                    try:
                        text = pop_last_user_message()
                    except ValueError as exc:
                        return self.send_text(str(exc), 409)
                return self.send_json({"ok": True, "text": text})
            if path == "/api/shutdown":
                with _send_lock:
                    shutdown_viewer()
                return self.send_json({"ok": True, "shutdown": True})
        except Exception as exc:
            return self.send_text(str(exc), 500)
        self.send_error(404)


if __name__ == "__main__":
    os.chdir(ROOT)

    def port_is_free(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex((HOST, port)) != 0

    port = PORT
    while not port_is_free(port):
        port += 1

    server = ThreadingHTTPServer((HOST, port), Handler)
    _server = server
    url = f"http://{HOST}:{port}"
    if sys.stdout:
        print(f"input.json viewer: {url}")
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_process()
    finally:
        _server = None
