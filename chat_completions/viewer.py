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
    """Create one native Chat Completions user-content part."""
    filename = part.get_filename() or "attachment"
    raw = part.get_payload(decode=True) or b""
    mime = part.get_content_type() or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if mime.startswith("image/"):
        encoded = base64.b64encode(raw).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        }
    try:
        text = raw.decode("utf-8")
        return {"type": "text", "text": f"附件 {filename}:\n{text}"}
    except UnicodeDecodeError:
        encoded = base64.b64encode(raw).decode("ascii")
        return {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": f"data:{mime};base64,{encoded}",
            },
        }


def _user_message_text(message):
    """Extract editable text from a native Chat Completions user message."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            texts.append(str(part.get("text", "")))
    return "\n".join(text for text in texts if text)


def pop_last_user_message():
    data = read_input()
    body = data.setdefault("json", {})
    messages = body.setdefault("messages", [])
    if not messages:
        raise ValueError("no messages to edit")
    last = messages[-1]
    if last.get("role") != "user":
        raise ValueError("last message is not a user message")
    text = _user_message_text(last)
    messages.pop()
    write_input(data)
    return text


def append_user_message(text, files):
    """Append a native Chat Completions user message."""
    data = read_input()
    body = data.setdefault("json", {})
    messages = body.setdefault("messages", [])
    parts = []
    if text.strip():
        parts.append({"type": "text", "text": text.strip()})
    parts.extend(files)
    if not parts:
        raise ValueError("message is empty")
    messages.append({"role": "user", "content": parts})
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


def _collapse_display_parts(parts):
    if not parts:
        return None
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0].get("text", "")
    return parts


def _chat_content_for_display(content):
    if isinstance(content, str) or content is None:
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": str(part.get("text", ""))})
        elif kind == "image_url":
            image_url = part.get("image_url") or {}
            if isinstance(image_url, str):
                image_url = {"url": image_url}
            parts.append({"type": "image_url", "image_url": {"url": image_url.get("url", "")}})
        elif kind == "file":
            file_info = part.get("file") or {}
            parts.append({"type": "text", "text": f"[附件：{file_info.get('filename', 'file')}]"})
        elif kind == "refusal":
            parts.append({"type": "text", "text": str(part.get("refusal", ""))})
    return _collapse_display_parts(parts)


def chat_transcript(body):
    """Read only a native Chat Completions `messages` transcript."""
    result = []
    for message in body.get("messages", []):
        if not isinstance(message, dict) or not message.get("role"):
            continue
        item = {
            "role": message["role"],
            "content": _chat_content_for_display(message.get("content")),
        }
        if message.get("role") == "tool":
            item["tool_call_id"] = message.get("tool_call_id")
        if message.get("tool_calls"):
            item["tool_calls"] = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                item["tool_calls"].append({
                    "id": call.get("id"),
                    "function": {
                        "name": function.get("name", "python"),
                        "arguments": function.get("arguments", ""),
                    },
                })
        result.append(item)
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
            messages = chat_transcript(body)
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
<title>Chat Completions · input.json 查看器</title>
<script>
/* Apply theme before paint */
(function(){
  try{
    var t=localStorage.getItem('ae-theme');
    if(t!=='dark'&&t!=='light'){
      t=(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
    }
    document.documentElement.setAttribute('data-theme', t);
  }catch(e){
    document.documentElement.setAttribute('data-theme','light');
  }
})();
</script>
<style>
:root{color-scheme:light;--bg:#f7f7f4;--panel:#ffffff;--text:#202124;--muted:#62676f;--line:#d8dadd;--accent:#0f766e;--danger:#b42318;--tool:#eef2f6;--user:#e8f3ee;--assistant:#fff;--system:#f4efe6;--code-bg:#1f2328;--code-fg:#f6f8fa;--inline-code-bg:#eef2f6;--table-th:#f1f3f5;--table-stripe:#fafafa;--quote-bg:#fafafa;--shadow:rgba(0,0,0,.08);--topbar-btn:#eef2f6;--topbar-btn-hover:#e4e9ee;--toast-bg:#1f2328;--toast-fg:#f6f8fa;--spinner-track:#cbd2d9;--tool-state:#7a828c;--tool-state-done:#537066;--chip-bg:#eef2f6;--chip-border:#d8dadd;--track:#dde1e4;--btn-bg:#ffffff;--btn-border:#e5e7eb;--btn-text:#374151;--btn-hover-bg:#111827;--btn-hover-text:#ffffff;--hl-err:#b42318;--hl-err-bg:rgba(180,35,24,.08);--hl-tb:#b42318;--hl-file:#8a4b08;--hl-line:#9a6700;--hl-kw:#0550ae;--hl-str:#0a7b45;--hl-num:#0550ae;--hl-bool:#8250df;--hl-key:#953800;--hl-path:#3b6d11;--hl-url:#0969da;--hl-marker:#cf222e;--hl-warn:#9a6700;--hl-warn-bg:rgba(154,103,0,.1);--hl-ok:#1a7f37;--hl-dim:#8b949e;--hl-prompt:#6639ba;--hl-loading:#c8d0d8}html[data-theme="dark"]{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--text:#e6edf3;--muted:#9da7b3;--line:#30363d;--accent:#3db8a8;--danger:#f85149;--tool:#21262d;--user:#12261e;--assistant:transparent;--system:#2a2419;--code-bg:#0d1117;--code-fg:#e6edf3;--inline-code-bg:#21262d;--table-th:#21262d;--table-stripe:#12171f;--quote-bg:#21262d;--shadow:rgba(0,0,0,.35);--topbar-btn:#21262d;--topbar-btn-hover:#30363d;--toast-bg:#21262d;--toast-fg:#e6edf3;--spinner-track:#30363d;--tool-state:#8b949e;--tool-state-done:#7ee787;--chip-bg:#21262d;--chip-border:#30363d;--track:#30363d;--btn-bg:#21262d;--btn-border:#30363d;--btn-text:#e6edf3;--btn-hover-bg:#e6edf3;--btn-hover-text:#0d1117;--hl-err:#ff7b72;--hl-err-bg:rgba(248,81,73,.12);--hl-tb:#ff7b72;--hl-file:#e3b341;--hl-line:#d29922;--hl-kw:#79c0ff;--hl-str:#a5d6ff;--hl-num:#79c0ff;--hl-bool:#d2a8ff;--hl-key:#ffa657;--hl-path:#7ee787;--hl-url:#58a6ff;--hl-marker:#ff7b72;--hl-warn:#d29922;--hl-warn-bg:rgba(210,153,34,.12);--hl-ok:#3fb950;--hl-dim:#8b949e;--hl-prompt:#d2a8ff;--hl-loading:#8b949e}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;overflow-x:hidden}.app{min-height:100vh;padding:16px}.messages{max-width:980px;margin:0 auto;display:flex;flex-direction:column;gap:10px;width:100%;min-width:0}.message-list{display:flex;flex-direction:column;gap:10px;min-width:0}.model-message .content{font-weight:600}.model-message .role{text-transform:none}.runner-status{display:flex;align-items:center;gap:8px;padding:8px 4px;color:var(--muted);font-size:13px}.status-spinner{width:14px;height:14px;border:2px solid var(--spinner-track);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}.msg{border:0;background:var(--panel);border-radius:12px;padding:10px 12px;max-width:100%;overflow:hidden}.msg.user{background:var(--user)}.msg.system{background:var(--system)}.msg.assistant{background:transparent;padding-left:0;padding-right:0}.role{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--muted);font-size:12px;margin-bottom:6px;text-transform:uppercase;letter-spacing:0}.content{overflow-wrap:anywhere;word-break:break-word;max-width:100%;min-width:0;overflow-x:auto}.content p{margin:0 0 8px}.content p:last-child{margin-bottom:0}.content h1,.content h2,.content h3,.content h4,.content h5,.content h6{margin:10px 0 6px;line-height:1.25}.content h1{font-size:22px}.content h2{font-size:18px}.content h3{font-size:16px}.content h4{font-size:15px}.content h5{font-size:14px}.content h6{font-size:13px;color:var(--muted)}.content ul,.content ol{margin:6px 0 8px 22px;padding:0}.content li>ul,.content li>ol{margin-top:4px;margin-bottom:4px}.content table{border-collapse:collapse;margin:8px 0;width:max-content;max-width:100%;display:block;overflow:auto}.content th,.content td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}.content th{background:var(--table-th);font-weight:650}.content tr:nth-child(even) td{background:var(--table-stripe)}.content blockquote{margin:8px 0;padding:6px 10px;border-left:3px solid var(--line);background:var(--quote-bg);color:var(--muted)}.content pre{margin:8px 0;padding:10px;overflow:auto;background:var(--code-bg);color:var(--code-fg);border-radius:6px}.content code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}.content :not(pre)>code{background:var(--inline-code-bg);border-radius:4px;padding:1px 4px}.content img{display:block;max-width:100%;width:auto;height:auto;max-height:min(520px,70vh);border:1px solid var(--line);border-radius:6px;margin:8px 0;background:#fff}.content a{color:var(--accent);text-decoration:underline;text-underline-offset:2px}.content a:hover{opacity:.85}.content del{text-decoration:line-through;color:var(--muted)}.content hr{border:0;border-top:1px solid var(--line);margin:12px 0}.content .task-list-item,.content li.task-list-item,.content li:has(>input[type=checkbox]){list-style:none;margin-left:-1.2em}.content .task-list-item input[type=checkbox],.content li input[type=checkbox]{margin-right:6px;vertical-align:middle}.content pre code[class*=language-]{font-family:inherit}.content li>p{margin:0 0 4px}.content li>p:last-child{margin-bottom:0}.content ul.contains-task-list,.content ol.contains-task-list{margin-left:8px}.content th[align=center],.content td[align=center]{text-align:center}.content th[align=right],.content td[align=right]{text-align:right}.content th[align=left],.content td[align=left]{text-align:left}.tool-group{border:0;background:transparent;border-radius:0;color:var(--muted);animation:messageIn .2s ease-out}.tool-group.running{border:0}.tool-group.has-failure{border:0;background:transparent}.tool-group summary{display:flex;align-items:center;gap:6px;min-height:28px;padding:2px 0;cursor:pointer;user-select:none;list-style:none}.tool-group summary::-webkit-details-marker{display:none}.tool-group summary:before{content:"›";font-size:18px;line-height:1;color:var(--tool-state);transition:transform .18s}.tool-group[open] summary:before{transform:rotate(90deg)}.tool-title{font-weight:500;color:var(--muted)}.tool-meta{margin-left:0;color:var(--muted);font-size:12px}.tool-duration{color:var(--tool-state);font-size:12px;font-variant-numeric:tabular-nums}.tool-events{border-top:0;padding:2px 0 4px 14px}.tool-group[open] .tool-events{animation:reveal .18s ease-out}.tool-event{display:grid;grid-template-columns:minmax(80px,auto) minmax(0,1fr) auto auto;align-items:center;gap:10px;min-width:0;padding:5px 0;font-size:12px;border-bottom:1px solid rgba(0,0,0,.05)}.tool-event-duration{color:var(--tool-state);font-size:12px;font-variant-numeric:tabular-nums;min-width:3.5em;text-align:right}.tool-event:last-child{border-bottom:0}.tool-name{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--text)}.tool-state{text-align:right;color:var(--tool-state)}.tool-event.done .tool-state{color:var(--tool-state-done)}.tool-event.failed .tool-state{color:var(--tool-state);font-weight:400}.tool-output-button{border:0;background:transparent;color:var(--accent);font:inherit;cursor:pointer;padding:2px 4px}.tool-output{grid-column:1/-1;width:100%;max-width:100%;min-width:0;max-height:320px;overflow:auto;margin:3px 0 5px!important;font-size:12px!important;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:var(--code-bg);color:var(--code-fg);border:1px solid var(--line);border-radius:6px;padding:8px 10px}.tool-output.loading{color:var(--hl-loading)}.composer{max-width:980px;margin:18px auto 0;position:relative;padding:0 0 44px}.composer-inner{display:block}.usage-direct{position:absolute;right:0;bottom:0;margin:0;text-align:right;color:var(--muted);font-size:12px;min-height:0;width:auto;pointer-events:none;line-height:1.3}.token-track{width:120px;height:3px;margin:3px 0 0 auto;background:var(--track);border-radius:99px;overflow:hidden}.token-bar{width:0;height:100%;background:var(--accent);border-radius:inherit;transition:width .35s ease,background-color .25s}.token-bar.warn{background:var(--hl-warn)}.token-bar.danger{background:var(--danger)}.drop{border:0;background:transparent;border-radius:0;padding:0;min-height:72px}.drop.drag{outline:none;box-shadow:inset 0 0 0 2px var(--accent);border-radius:10px;padding:8px}textarea{width:100%;min-height:72px;max-height:none;height:auto;resize:none;border:0;outline:0;font:inherit;background:transparent;color:var(--text);padding:0 0 40px 0;line-height:1.55}textarea::placeholder{color:var(--muted)}.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.file{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 8px;background:var(--chip-bg)}.file button{border:0;background:transparent;cursor:pointer;color:var(--muted)}button.run{position:absolute;right:0;bottom:28px;height:34px;border:0;border-radius:10px;padding:0 14px;background:var(--accent);color:#fff;font-weight:650;cursor:pointer;min-width:84px;z-index:2}button.stop{background:var(--danger)}.msg.user.editable-last{cursor:pointer}.msg.user.editable-last:hover{box-shadow:inset 0 0 0 1px rgba(15,118,110,.28)}.msg.user.editable-last:active{transform:scale(.997)}.hidden{display:none}.empty{max-width:980px;margin:40px auto;color:var(--muted);text-align:center}.msg{animation:messageIn .2s ease-out}.new-messages{position:fixed;right:20px;bottom:20px;z-index:3;border:1px solid var(--line);border-radius:999px;padding:8px 13px;background:var(--panel);color:var(--text);box-shadow:0 6px 24px var(--shadow);cursor:pointer;animation:popIn .18s ease-out}.run{transition:transform .12s,background-color .2s,opacity .2s}.run:active{transform:scale(.97)}@keyframes spin{to{transform:rotate(360deg)}}@keyframes messageIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}@keyframes reveal{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}@keyframes popIn{from{opacity:0;transform:translateY(6px) scale(.96)}to{opacity:1;transform:none}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important;transition-duration:.01ms!important}}@media(max-width:700px){.app{padding-left:10px;padding-right:10px}.composer-inner{display:block}}
.model-message{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;background:transparent !important;box-shadow:none !important;padding:4px 2px !important}.model-message .model-meta{min-width:0;flex:1}.model-message .content{font-weight:600}.model-message .role{text-transform:none}.kill-process{flex:0 0 auto;border:1px solid var(--btn-border);background:var(--btn-bg);color:var(--btn-text);border-radius:999px;padding:5px 10px;font:12px/1.2 system-ui,-apple-system,"Segoe UI",sans-serif;font-weight:600;cursor:pointer;transition:background-color .15s ease,color .15s ease,border-color .15s ease,opacity .15s ease,transform .12s ease}.kill-process:hover{background:var(--btn-hover-bg);color:var(--btn-hover-text);border-color:var(--btn-hover-bg)}.kill-process:active{transform:scale(.97)}.kill-process:disabled{opacity:.65;cursor:wait}

.tool-output .hl{font-style:inherit;font-weight:inherit}
.tool-output .hl-err{color:var(--hl-err);background:var(--hl-err-bg);border-radius:3px}
.tool-output .hl-tb{color:var(--hl-tb);font-weight:600}
.tool-output .hl-file{color:var(--hl-file)}
.tool-output .hl-line{color:var(--hl-line)}
.tool-output .hl-kw{color:var(--hl-kw)}
.tool-output .hl-str{color:var(--hl-str)}
.tool-output .hl-num{color:var(--hl-num)}
.tool-output .hl-bool{color:var(--hl-bool)}
.tool-output .hl-key{color:var(--hl-key)}
.tool-output .hl-path{color:var(--hl-path)}
.tool-output .hl-url{color:var(--hl-url);text-decoration:underline;text-underline-offset:2px}
.tool-output .hl-marker{color:var(--hl-marker);font-weight:600}
.tool-output .hl-warn{color:var(--hl-warn);background:var(--hl-warn-bg);border-radius:3px}
.tool-output .hl-ok{color:var(--hl-ok)}
.tool-output .hl-dim{color:var(--hl-dim)}
.tool-output .hl-prompt{color:var(--hl-prompt);font-weight:600}
.model-actions{display:flex;align-items:center;gap:8px;flex:0 0 auto}
.theme-toggle{border:0;background:var(--topbar-btn);color:var(--text);border-radius:8px;width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;cursor:pointer;line-height:1;padding:0}
.theme-toggle:hover{background:var(--topbar-btn-hover)}
.theme-toggle svg{width:18px;height:18px;display:block}
.theme-toggle .theme-icon-dark{display:none}
html[data-theme="dark"] .theme-toggle .theme-icon-light{display:none}
html[data-theme="dark"] .theme-toggle .theme-icon-dark{display:block}

</style>
</head>
<body>
<main class="app">
  <section class="messages">
    <article class="msg model-message"><div class="model-meta"><div class="role">模型</div><div id="model" class="content">model</div></div><div class="model-actions"><button type="button" class="theme-toggle" id="themeToggle" title="切换亮/暗主题" aria-label="切换亮/暗主题"><span class="theme-icon-light" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg></span><span class="theme-icon-dark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg></span></button><button id="killProcess" class="kill-process" type="button" title="关闭当前端口的查看器进程">关闭查看器</button></div></article>
    <div id="messages" class="message-list"></div>
    <div id="runnerStatus" class="runner-status hidden"><span class="status-spinner"></span><span id="runnerLabel">正在运行…</span></div>
    <div id="empty" class="empty hidden">暂无消息</div>
  </section>
  <form id="composer" class="composer"><div class="composer-inner"><div id="drop" class="drop"><textarea id="message" name="message" placeholder="输入消息，或拖入/粘贴文件、图片；留空可直接运行"></textarea><div id="files" class="files"></div><input id="fileInput" name="files" type="file" multiple class="hidden"></div><button id="run" class="run" type="submit">运行</button></div><div class="usage-direct"><div id="usageText">Token：计算中…</div><div class="token-track"><div id="tokenBar" class="token-bar"></div></div></div></form>
</main>
<button id="newMessages" class="new-messages hidden" type="button">↓ 新消息</button>
<script>
/**
 * marked v11.1.1 - a markdown parser
 * Copyright (c) 2011-2023, Christopher Jeffrey. (MIT Licensed)
 * https://github.com/markedjs/marked
 */
!function(e,t){"object"==typeof exports&&"undefined"!=typeof module?t(exports):"function"==typeof define&&define.amd?define(["exports"],t):t((e="undefined"!=typeof globalThis?globalThis:e||self).marked={})}(this,(function(e){"use strict";function t(){return{async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null}}function n(t){e.defaults=t}e.defaults={async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null};const s=/[&<>"']/,r=new RegExp(s.source,"g"),i=/[<>"']|&(?!(#\d{1,7}|#[Xx][a-fA-F0-9]{1,6}|\w+);)/,l=new RegExp(i.source,"g"),o={"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"},a=e=>o[e];function c(e,t){if(t){if(s.test(e))return e.replace(r,a)}else if(i.test(e))return e.replace(l,a);return e}const h=/&(#(?:\d+)|(?:#x[0-9A-Fa-f]+)|(?:\w+));?/gi;function p(e){return e.replace(h,((e,t)=>"colon"===(t=t.toLowerCase())?":":"#"===t.charAt(0)?"x"===t.charAt(1)?String.fromCharCode(parseInt(t.substring(2),16)):String.fromCharCode(+t.substring(1)):""))}const u=/(^|[^\[])\^/g;function k(e,t){let n="string"==typeof e?e:e.source;t=t||"";const s={replace:(e,t)=>{let r="string"==typeof t?t:t.source;return r=r.replace(u,"$1"),n=n.replace(e,r),s},getRegex:()=>new RegExp(n,t)};return s}function g(e){try{e=encodeURI(e).replace(/%25/g,"%")}catch(e){return null}return e}const f={exec:()=>null};function d(e,t){const n=e.replace(/\|/g,((e,t,n)=>{let s=!1,r=t;for(;--r>=0&&"\\"===n[r];)s=!s;return s?"|":" |"})).split(/ \|/);let s=0;if(n[0].trim()||n.shift(),n.length>0&&!n[n.length-1].trim()&&n.pop(),t)if(n.length>t)n.splice(t);else for(;n.length<t;)n.push("");for(;s<n.length;s++)n[s]=n[s].trim().replace(/\\\|/g,"|");return n}function x(e,t,n){const s=e.length;if(0===s)return"";let r=0;for(;r<s;){const i=e.charAt(s-r-1);if(i!==t||n){if(i===t||!n)break;r++}else r++}return e.slice(0,s-r)}function b(e,t,n,s){const r=t.href,i=t.title?c(t.title):null,l=e[1].replace(/\\([\[\]])/g,"$1");if("!"!==e[0].charAt(0)){s.state.inLink=!0;const e={type:"link",raw:n,href:r,title:i,text:l,tokens:s.inlineTokens(l)};return s.state.inLink=!1,e}return{type:"image",raw:n,href:r,title:i,text:c(l)}}class w{options;rules;lexer;constructor(t){this.options=t||e.defaults}space(e){const t=this.rules.block.newline.exec(e);if(t&&t[0].length>0)return{type:"space",raw:t[0]}}code(e){const t=this.rules.block.code.exec(e);if(t){const e=t[0].replace(/^ {1,4}/gm,"");return{type:"code",raw:t[0],codeBlockStyle:"indented",text:this.options.pedantic?e:x(e,"\n")}}}fences(e){const t=this.rules.block.fences.exec(e);if(t){const e=t[0],n=function(e,t){const n=e.match(/^(\s+)(?:```)/);if(null===n)return t;const s=n[1];return t.split("\n").map((e=>{const t=e.match(/^\s+/);if(null===t)return e;const[n]=t;return n.length>=s.length?e.slice(s.length):e})).join("\n")}(e,t[3]||"");return{type:"code",raw:e,lang:t[2]?t[2].trim().replace(this.rules.inline.anyPunctuation,"$1"):t[2],text:n}}}heading(e){const t=this.rules.block.heading.exec(e);if(t){let e=t[2].trim();if(/#$/.test(e)){const t=x(e,"#");this.options.pedantic?e=t.trim():t&&!/ $/.test(t)||(e=t.trim())}return{type:"heading",raw:t[0],depth:t[1].length,text:e,tokens:this.lexer.inline(e)}}}hr(e){const t=this.rules.block.hr.exec(e);if(t)return{type:"hr",raw:t[0]}}blockquote(e){const t=this.rules.block.blockquote.exec(e);if(t){const e=x(t[0].replace(/^ *>[ \t]?/gm,""),"\n"),n=this.lexer.state.top;this.lexer.state.top=!0;const s=this.lexer.blockTokens(e);return this.lexer.state.top=n,{type:"blockquote",raw:t[0],tokens:s,text:e}}}list(e){let t=this.rules.block.list.exec(e);if(t){let n=t[1].trim();const s=n.length>1,r={type:"list",raw:"",ordered:s,start:s?+n.slice(0,-1):"",loose:!1,items:[]};n=s?`\\d{1,9}\\${n.slice(-1)}`:`\\${n}`,this.options.pedantic&&(n=s?n:"[*+-]");const i=new RegExp(`^( {0,3}${n})((?:[\t ][^\\n]*)?(?:\\n|$))`);let l="",o="",a=!1;for(;e;){let n=!1;if(!(t=i.exec(e)))break;if(this.rules.block.hr.test(e))break;l=t[0],e=e.substring(l.length);let s=t[2].split("\n",1)[0].replace(/^\t+/,(e=>" ".repeat(3*e.length))),c=e.split("\n",1)[0],h=0;this.options.pedantic?(h=2,o=s.trimStart()):(h=t[2].search(/[^ ]/),h=h>4?1:h,o=s.slice(h),h+=t[1].length);let p=!1;if(!s&&/^ *$/.test(c)&&(l+=c+"\n",e=e.substring(c.length+1),n=!0),!n){const t=new RegExp(`^ {0,${Math.min(3,h-1)}}(?:[*+-]|\\d{1,9}[.)])((?:[ \t][^\\n]*)?(?:\\n|$))`),n=new RegExp(`^ {0,${Math.min(3,h-1)}}((?:- *){3,}|(?:_ *){3,}|(?:\\* *){3,})(?:\\n+|$)`),r=new RegExp(`^ {0,${Math.min(3,h-1)}}(?:\`\`\`|~~~)`),i=new RegExp(`^ {0,${Math.min(3,h-1)}}#`);for(;e;){const a=e.split("\n",1)[0];if(c=a,this.options.pedantic&&(c=c.replace(/^ {1,4}(?=( {4})*[^ ])/g,"  ")),r.test(c))break;if(i.test(c))break;if(t.test(c))break;if(n.test(e))break;if(c.search(/[^ ]/)>=h||!c.trim())o+="\n"+c.slice(h);else{if(p)break;if(s.search(/[^ ]/)>=4)break;if(r.test(s))break;if(i.test(s))break;if(n.test(s))break;o+="\n"+c}p||c.trim()||(p=!0),l+=a+"\n",e=e.substring(a.length+1),s=c.slice(h)}}r.loose||(a?r.loose=!0:/\n *\n *$/.test(l)&&(a=!0));let u,k=null;this.options.gfm&&(k=/^\[[ xX]\] /.exec(o),k&&(u="[ ] "!==k[0],o=o.replace(/^\[[ xX]\] +/,""))),r.items.push({type:"list_item",raw:l,task:!!k,checked:u,loose:!1,text:o,tokens:[]}),r.raw+=l}r.items[r.items.length-1].raw=l.trimEnd(),r.items[r.items.length-1].text=o.trimEnd(),r.raw=r.raw.trimEnd();for(let e=0;e<r.items.length;e++)if(this.lexer.state.top=!1,r.items[e].tokens=this.lexer.blockTokens(r.items[e].text,[]),!r.loose){const t=r.items[e].tokens.filter((e=>"space"===e.type)),n=t.length>0&&t.some((e=>/\n.*\n/.test(e.raw)));r.loose=n}if(r.loose)for(let e=0;e<r.items.length;e++)r.items[e].loose=!0;return r}}html(e){const t=this.rules.block.html.exec(e);if(t){return{type:"html",block:!0,raw:t[0],pre:"pre"===t[1]||"script"===t[1]||"style"===t[1],text:t[0]}}}def(e){const t=this.rules.block.def.exec(e);if(t){const e=t[1].toLowerCase().replace(/\s+/g," "),n=t[2]?t[2].replace(/^<(.*)>$/,"$1").replace(this.rules.inline.anyPunctuation,"$1"):"",s=t[3]?t[3].substring(1,t[3].length-1).replace(this.rules.inline.anyPunctuation,"$1"):t[3];return{type:"def",tag:e,raw:t[0],href:n,title:s}}}table(e){const t=this.rules.block.table.exec(e);if(!t)return;if(!/[:|]/.test(t[2]))return;const n=d(t[1]),s=t[2].replace(/^\||\| *$/g,"").split("|"),r=t[3]&&t[3].trim()?t[3].replace(/\n[ \t]*$/,"").split("\n"):[],i={type:"table",raw:t[0],header:[],align:[],rows:[]};if(n.length===s.length){for(const e of s)/^ *-+: *$/.test(e)?i.align.push("right"):/^ *:-+: *$/.test(e)?i.align.push("center"):/^ *:-+ *$/.test(e)?i.align.push("left"):i.align.push(null);for(const e of n)i.header.push({text:e,tokens:this.lexer.inline(e)});for(const e of r)i.rows.push(d(e,i.header.length).map((e=>({text:e,tokens:this.lexer.inline(e)}))));return i}}lheading(e){const t=this.rules.block.lheading.exec(e);if(t)return{type:"heading",raw:t[0],depth:"="===t[2].charAt(0)?1:2,text:t[1],tokens:this.lexer.inline(t[1])}}paragraph(e){const t=this.rules.block.paragraph.exec(e);if(t){const e="\n"===t[1].charAt(t[1].length-1)?t[1].slice(0,-1):t[1];return{type:"paragraph",raw:t[0],text:e,tokens:this.lexer.inline(e)}}}text(e){const t=this.rules.block.text.exec(e);if(t)return{type:"text",raw:t[0],text:t[0],tokens:this.lexer.inline(t[0])}}escape(e){const t=this.rules.inline.escape.exec(e);if(t)return{type:"escape",raw:t[0],text:c(t[1])}}tag(e){const t=this.rules.inline.tag.exec(e);if(t)return!this.lexer.state.inLink&&/^<a /i.test(t[0])?this.lexer.state.inLink=!0:this.lexer.state.inLink&&/^<\/a>/i.test(t[0])&&(this.lexer.state.inLink=!1),!this.lexer.state.inRawBlock&&/^<(pre|code|kbd|script)(\s|>)/i.test(t[0])?this.lexer.state.inRawBlock=!0:this.lexer.state.inRawBlock&&/^<\/(pre|code|kbd|script)(\s|>)/i.test(t[0])&&(this.lexer.state.inRawBlock=!1),{type:"html",raw:t[0],inLink:this.lexer.state.inLink,inRawBlock:this.lexer.state.inRawBlock,block:!1,text:t[0]}}link(e){const t=this.rules.inline.link.exec(e);if(t){const e=t[2].trim();if(!this.options.pedantic&&/^</.test(e)){if(!/>$/.test(e))return;const t=x(e.slice(0,-1),"\\");if((e.length-t.length)%2==0)return}else{const e=function(e,t){if(-1===e.indexOf(t[1]))return-1;let n=0;for(let s=0;s<e.length;s++)if("\\"===e[s])s++;else if(e[s]===t[0])n++;else if(e[s]===t[1]&&(n--,n<0))return s;return-1}(t[2],"()");if(e>-1){const n=(0===t[0].indexOf("!")?5:4)+t[1].length+e;t[2]=t[2].substring(0,e),t[0]=t[0].substring(0,n).trim(),t[3]=""}}let n=t[2],s="";if(this.options.pedantic){const e=/^([^'"]*[^\s])\s+(['"])(.*)\2/.exec(n);e&&(n=e[1],s=e[3])}else s=t[3]?t[3].slice(1,-1):"";return n=n.trim(),/^</.test(n)&&(n=this.options.pedantic&&!/>$/.test(e)?n.slice(1):n.slice(1,-1)),b(t,{href:n?n.replace(this.rules.inline.anyPunctuation,"$1"):n,title:s?s.replace(this.rules.inline.anyPunctuation,"$1"):s},t[0],this.lexer)}}reflink(e,t){let n;if((n=this.rules.inline.reflink.exec(e))||(n=this.rules.inline.nolink.exec(e))){const e=t[(n[2]||n[1]).replace(/\s+/g," ").toLowerCase()];if(!e){const e=n[0].charAt(0);return{type:"text",raw:e,text:e}}return b(n,e,n[0],this.lexer)}}emStrong(e,t,n=""){let s=this.rules.inline.emStrongLDelim.exec(e);if(!s)return;if(s[3]&&n.match(/[\p{L}\p{N}]/u))return;if(!(s[1]||s[2]||"")||!n||this.rules.inline.punctuation.exec(n)){const n=[...s[0]].length-1;let r,i,l=n,o=0;const a="*"===s[0][0]?this.rules.inline.emStrongRDelimAst:this.rules.inline.emStrongRDelimUnd;for(a.lastIndex=0,t=t.slice(-1*e.length+n);null!=(s=a.exec(t));){if(r=s[1]||s[2]||s[3]||s[4]||s[5]||s[6],!r)continue;if(i=[...r].length,s[3]||s[4]){l+=i;continue}if((s[5]||s[6])&&n%3&&!((n+i)%3)){o+=i;continue}if(l-=i,l>0)continue;i=Math.min(i,i+l+o);const t=[...s[0]][0].length,a=e.slice(0,n+s.index+t+i);if(Math.min(n,i)%2){const e=a.slice(1,-1);return{type:"em",raw:a,text:e,tokens:this.lexer.inlineTokens(e)}}const c=a.slice(2,-2);return{type:"strong",raw:a,text:c,tokens:this.lexer.inlineTokens(c)}}}}codespan(e){const t=this.rules.inline.code.exec(e);if(t){let e=t[2].replace(/\n/g," ");const n=/[^ ]/.test(e),s=/^ /.test(e)&&/ $/.test(e);return n&&s&&(e=e.substring(1,e.length-1)),e=c(e,!0),{type:"codespan",raw:t[0],text:e}}}br(e){const t=this.rules.inline.br.exec(e);if(t)return{type:"br",raw:t[0]}}del(e){const t=this.rules.inline.del.exec(e);if(t)return{type:"del",raw:t[0],text:t[2],tokens:this.lexer.inlineTokens(t[2])}}autolink(e){const t=this.rules.inline.autolink.exec(e);if(t){let e,n;return"@"===t[2]?(e=c(t[1]),n="mailto:"+e):(e=c(t[1]),n=e),{type:"link",raw:t[0],text:e,href:n,tokens:[{type:"text",raw:e,text:e}]}}}url(e){let t;if(t=this.rules.inline.url.exec(e)){let e,n;if("@"===t[2])e=c(t[0]),n="mailto:"+e;else{let s;do{s=t[0],t[0]=this.rules.inline._backpedal.exec(t[0])?.[0]??""}while(s!==t[0]);e=c(t[0]),n="www."===t[1]?"http://"+t[0]:t[0]}return{type:"link",raw:t[0],text:e,href:n,tokens:[{type:"text",raw:e,text:e}]}}}inlineText(e){const t=this.rules.inline.text.exec(e);if(t){let e;return e=this.lexer.state.inRawBlock?t[0]:c(t[0]),{type:"text",raw:t[0],text:e}}}}const m=/^ {0,3}((?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|$)/,y=/(?:[*+-]|\d{1,9}[.)])/,$=k(/^(?!bull )((?:.|\n(?!\s*?\n|bull ))+?)\n {0,3}(=+|-+) *(?:\n+|$)/).replace(/bull/g,y).getRegex(),z=/^([^\n]+(?:\n(?!hr|heading|lheading|blockquote|fences|list|html|table| +\n)[^\n]+)*)/,T=/(?!\s*\])(?:\\.|[^\[\]\\])+/,R=k(/^ {0,3}\[(label)\]: *(?:\n *)?([^<\s][^\s]*|<.*?>)(?:(?: +(?:\n *)?| *\n *)(title))? *(?:\n+|$)/).replace("label",T).replace("title",/(?:"(?:\\"?|[^"\\])*"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\))/).getRegex(),_=k(/^( {0,3}bull)([ \t][^\n]+?)?(?:\n|$)/).replace(/bull/g,y).getRegex(),A="address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|ol|optgroup|option|p|param|section|source|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul",S=/<!--(?!-?>)[\s\S]*?(?:-->|$)/,I=k("^ {0,3}(?:<(script|pre|style|textarea)[\\s>][\\s\\S]*?(?:</\\1>[^\\n]*\\n+|$)|comment[^\\n]*(\\n+|$)|<\\?[\\s\\S]*?(?:\\?>\\n*|$)|<![A-Z][\\s\\S]*?(?:>\\n*|$)|<!\\[CDATA\\[[\\s\\S]*?(?:\\]\\]>\\n*|$)|</?(tag)(?: +|\\n|/?>)[\\s\\S]*?(?:(?:\\n *)+\\n|$)|<(?!script|pre|style|textarea)([a-z][\\w-]*)(?:attribute)*? */?>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n *)+\\n|$)|</(?!script|pre|style|textarea)[a-z][\\w-]*\\s*>(?=[ \\t]*(?:\\n|$))[\\s\\S]*?(?:(?:\\n *)+\\n|$))","i").replace("comment",S).replace("tag",A).replace("attribute",/ +[a-zA-Z:_][\w.:-]*(?: *= *"[^"\n]*"| *= *'[^'\n]*'| *= *[^\s"'=<>`]+)?/).getRegex(),E=k(z).replace("hr",m).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("|lheading","").replace("|table","").replace("blockquote"," {0,3}>").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",A).getRegex(),Z={blockquote:k(/^( {0,3}> ?(paragraph|[^\n]*)(?:\n|$))+/).replace("paragraph",E).getRegex(),code:/^( {4}[^\n]+(?:\n(?: *(?:\n|$))*)?)+/,def:R,fences:/^ {0,3}(`{3,}(?=[^`\n]*(?:\n|$))|~{3,})([^\n]*)(?:\n|$)(?:|([\s\S]*?)(?:\n|$))(?: {0,3}\1[~`]* *(?=\n|$)|$)/,heading:/^ {0,3}(#{1,6})(?=\s|$)(.*)(?:\n+|$)/,hr:m,html:I,lheading:$,list:_,newline:/^(?: *(?:\n|$))+/,paragraph:E,table:f,text:/^[^\n]+/},q=k("^ *([^\\n ].*)\\n {0,3}((?:\\| *)?:?-+:? *(?:\\| *:?-+:? *)*(?:\\| *)?)(?:\\n((?:(?! *\\n|hr|heading|blockquote|code|fences|list|html).*(?:\\n|$))*)\\n*|$)").replace("hr",m).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("blockquote"," {0,3}>").replace("code"," {4}[^\\n]").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",A).getRegex(),L={...Z,table:q,paragraph:k(z).replace("hr",m).replace("heading"," {0,3}#{1,6}(?:\\s|$)").replace("|lheading","").replace("table",q).replace("blockquote"," {0,3}>").replace("fences"," {0,3}(?:`{3,}(?=[^`\\n]*\\n)|~{3,})[^\\n]*\\n").replace("list"," {0,3}(?:[*+-]|1[.)]) ").replace("html","</?(?:tag)(?: +|\\n|/?>)|<(?:script|pre|style|textarea|!--)").replace("tag",A).getRegex()},P={...Z,html:k("^ *(?:comment *(?:\\n|\\s*$)|<(tag)[\\s\\S]+?</\\1> *(?:\\n{2,}|\\s*$)|<tag(?:\"[^\"]*\"|'[^']*'|\\s[^'\"/>\\s]*)*?/?> *(?:\\n{2,}|\\s*$))").replace("comment",S).replace(/tag/g,"(?!(?:a|em|strong|small|s|cite|q|dfn|abbr|data|time|code|var|samp|kbd|sub|sup|i|b|u|mark|ruby|rt|rp|bdi|bdo|span|br|wbr|ins|del|img)\\b)\\w+(?!:|[^\\w\\s@]*@)\\b").getRegex(),def:/^ *\[([^\]]+)\]: *<?([^\s>]+)>?(?: +(["(][^\n]+[")]))? *(?:\n+|$)/,heading:/^(#{1,6})(.*)(?:\n+|$)/,fences:f,lheading:/^(.+?)\n {0,3}(=+|-+) *(?:\n+|$)/,paragraph:k(z).replace("hr",m).replace("heading"," *#{1,6} *[^\n]").replace("lheading",$).replace("|table","").replace("blockquote"," {0,3}>").replace("|fences","").replace("|list","").replace("|html","").replace("|tag","").getRegex()},Q=/^\\([!"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])/,v=/^( {2,}|\\)\n(?!\s*$)/,B="\\p{P}$+<=>`^|~",M=k(/^((?![*_])[\spunctuation])/,"u").replace(/punctuation/g,B).getRegex(),O=k(/^(?:\*+(?:((?!\*)[punct])|[^\s*]))|^_+(?:((?!_)[punct])|([^\s_]))/,"u").replace(/punct/g,B).getRegex(),C=k("^[^_*]*?__[^_*]*?\\*[^_*]*?(?=__)|[^*]+(?=[^*])|(?!\\*)[punct](\\*+)(?=[\\s]|$)|[^punct\\s](\\*+)(?!\\*)(?=[punct\\s]|$)|(?!\\*)[punct\\s](\\*+)(?=[^punct\\s])|[\\s](\\*+)(?!\\*)(?=[punct])|(?!\\*)[punct](\\*+)(?!\\*)(?=[punct])|[^punct\\s](\\*+)(?=[^punct\\s])","gu").replace(/punct/g,B).getRegex(),D=k("^[^_*]*?\\*\\*[^_*]*?_[^_*]*?(?=\\*\\*)|[^_]+(?=[^_])|(?!_)[punct](_+)(?=[\\s]|$)|[^punct\\s](_+)(?!_)(?=[punct\\s]|$)|(?!_)[punct\\s](_+)(?=[^punct\\s])|[\\s](_+)(?!_)(?=[punct])|(?!_)[punct](_+)(?!_)(?=[punct])","gu").replace(/punct/g,B).getRegex(),j=k(/\\([punct])/,"gu").replace(/punct/g,B).getRegex(),H=k(/^<(scheme:[^\s\x00-\x1f<>]*|email)>/).replace("scheme",/[a-zA-Z][a-zA-Z0-9+.-]{1,31}/).replace("email",/[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+(@)[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+(?![-_])/).getRegex(),U=k(S).replace("(?:--\x3e|$)","--\x3e").getRegex(),X=k("^comment|^</[a-zA-Z][\\w:-]*\\s*>|^<[a-zA-Z][\\w-]*(?:attribute)*?\\s*/?>|^<\\?[\\s\\S]*?\\?>|^<![a-zA-Z]+\\s[\\s\\S]*?>|^<!\\[CDATA\\[[\\s\\S]*?\\]\\]>").replace("comment",U).replace("attribute",/\s+[a-zA-Z:_][\w.:-]*(?:\s*=\s*"[^"]*"|\s*=\s*'[^']*'|\s*=\s*[^\s"'=<>`]+)?/).getRegex(),F=/(?:\[(?:\\.|[^\[\]\\])*\]|\\.|`[^`]*`|[^\[\]\\`])*?/,N=k(/^!?\[(label)\]\(\s*(href)(?:\s+(title))?\s*\)/).replace("label",F).replace("href",/<(?:\\.|[^\n<>\\])+>|[^\s\x00-\x1f]*/).replace("title",/"(?:\\"?|[^"\\])*"|'(?:\\'?|[^'\\])*'|\((?:\\\)?|[^)\\])*\)/).getRegex(),G=k(/^!?\[(label)\]\[(ref)\]/).replace("label",F).replace("ref",T).getRegex(),J=k(/^!?\[(ref)\](?:\[\])?/).replace("ref",T).getRegex(),K={_backpedal:f,anyPunctuation:j,autolink:H,blockSkip:/\[[^[\]]*?\]\([^\(\)]*?\)|`[^`]*?`|<[^<>]*?>/g,br:v,code:/^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/,del:f,emStrongLDelim:O,emStrongRDelimAst:C,emStrongRDelimUnd:D,escape:Q,link:N,nolink:J,punctuation:M,reflink:G,reflinkSearch:k("reflink|nolink(?!\\()","g").replace("reflink",G).replace("nolink",J).getRegex(),tag:X,text:/^(`+|[^`])(?:(?= {2,}\n)|[\s\S]*?(?:(?=[\\<!\[`*_]|\b_|$)|[^ ](?= {2,}\n)))/,url:f},V={...K,link:k(/^!?\[(label)\]\((.*?)\)/).replace("label",F).getRegex(),reflink:k(/^!?\[(label)\]\s*\[([^\]]*)\]/).replace("label",F).getRegex()},W={...K,escape:k(Q).replace("])","~|])").getRegex(),url:k(/^((?:ftp|https?):\/\/|www\.)(?:[a-zA-Z0-9\-]+\.?)+[^\s<]*|^email/,"i").replace("email",/[A-Za-z0-9._+-]+(@)[a-zA-Z0-9-_]+(?:\.[a-zA-Z0-9-_]*[a-zA-Z0-9])+(?![-_])/).getRegex(),_backpedal:/(?:[^?!.,:;*_'"~()&]+|\([^)]*\)|&(?![a-zA-Z0-9]+;$)|[?!.,:;*_'"~)]+(?!$))+/,del:/^(~~?)(?=[^\s~])([\s\S]*?[^\s~])\1(?=[^~]|$)/,text:/^([`~]+|[^`~])(?:(?= {2,}\n)|(?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)|[\s\S]*?(?:(?=[\\<!\[`*~_]|\b_|https?:\/\/|ftp:\/\/|www\.|$)|[^ ](?= {2,}\n)|[^a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-](?=[a-zA-Z0-9.!#$%&'*+\/=?_`{\|}~-]+@)))/},Y={...W,br:k(v).replace("{2,}","*").getRegex(),text:k(W.text).replace("\\b_","\\b_| {2,}\\n").replace(/\{2,\}/g,"*").getRegex()},ee={normal:Z,gfm:L,pedantic:P},te={normal:K,gfm:W,breaks:Y,pedantic:V};class ne{tokens;options;state;tokenizer;inlineQueue;constructor(t){this.tokens=[],this.tokens.links=Object.create(null),this.options=t||e.defaults,this.options.tokenizer=this.options.tokenizer||new w,this.tokenizer=this.options.tokenizer,this.tokenizer.options=this.options,this.tokenizer.lexer=this,this.inlineQueue=[],this.state={inLink:!1,inRawBlock:!1,top:!0};const n={block:ee.normal,inline:te.normal};this.options.pedantic?(n.block=ee.pedantic,n.inline=te.pedantic):this.options.gfm&&(n.block=ee.gfm,this.options.breaks?n.inline=te.breaks:n.inline=te.gfm),this.tokenizer.rules=n}static get rules(){return{block:ee,inline:te}}static lex(e,t){return new ne(t).lex(e)}static lexInline(e,t){return new ne(t).inlineTokens(e)}lex(e){e=e.replace(/\r\n|\r/g,"\n"),this.blockTokens(e,this.tokens);for(let e=0;e<this.inlineQueue.length;e++){const t=this.inlineQueue[e];this.inlineTokens(t.src,t.tokens)}return this.inlineQueue=[],this.tokens}blockTokens(e,t=[]){let n,s,r,i;for(e=this.options.pedantic?e.replace(/\t/g,"    ").replace(/^ +$/gm,""):e.replace(/^( *)(\t+)/gm,((e,t,n)=>t+"    ".repeat(n.length)));e;)if(!(this.options.extensions&&this.options.extensions.block&&this.options.extensions.block.some((s=>!!(n=s.call({lexer:this},e,t))&&(e=e.substring(n.raw.length),t.push(n),!0)))))if(n=this.tokenizer.space(e))e=e.substring(n.raw.length),1===n.raw.length&&t.length>0?t[t.length-1].raw+="\n":t.push(n);else if(n=this.tokenizer.code(e))e=e.substring(n.raw.length),s=t[t.length-1],!s||"paragraph"!==s.type&&"text"!==s.type?t.push(n):(s.raw+="\n"+n.raw,s.text+="\n"+n.text,this.inlineQueue[this.inlineQueue.length-1].src=s.text);else if(n=this.tokenizer.fences(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.heading(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.hr(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.blockquote(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.list(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.html(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.def(e))e=e.substring(n.raw.length),s=t[t.length-1],!s||"paragraph"!==s.type&&"text"!==s.type?this.tokens.links[n.tag]||(this.tokens.links[n.tag]={href:n.href,title:n.title}):(s.raw+="\n"+n.raw,s.text+="\n"+n.raw,this.inlineQueue[this.inlineQueue.length-1].src=s.text);else if(n=this.tokenizer.table(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.lheading(e))e=e.substring(n.raw.length),t.push(n);else{if(r=e,this.options.extensions&&this.options.extensions.startBlock){let t=1/0;const n=e.slice(1);let s;this.options.extensions.startBlock.forEach((e=>{s=e.call({lexer:this},n),"number"==typeof s&&s>=0&&(t=Math.min(t,s))})),t<1/0&&t>=0&&(r=e.substring(0,t+1))}if(this.state.top&&(n=this.tokenizer.paragraph(r)))s=t[t.length-1],i&&"paragraph"===s.type?(s.raw+="\n"+n.raw,s.text+="\n"+n.text,this.inlineQueue.pop(),this.inlineQueue[this.inlineQueue.length-1].src=s.text):t.push(n),i=r.length!==e.length,e=e.substring(n.raw.length);else if(n=this.tokenizer.text(e))e=e.substring(n.raw.length),s=t[t.length-1],s&&"text"===s.type?(s.raw+="\n"+n.raw,s.text+="\n"+n.text,this.inlineQueue.pop(),this.inlineQueue[this.inlineQueue.length-1].src=s.text):t.push(n);else if(e){const t="Infinite loop on byte: "+e.charCodeAt(0);if(this.options.silent){console.error(t);break}throw new Error(t)}}return this.state.top=!0,t}inline(e,t=[]){return this.inlineQueue.push({src:e,tokens:t}),t}inlineTokens(e,t=[]){let n,s,r,i,l,o,a=e;if(this.tokens.links){const e=Object.keys(this.tokens.links);if(e.length>0)for(;null!=(i=this.tokenizer.rules.inline.reflinkSearch.exec(a));)e.includes(i[0].slice(i[0].lastIndexOf("[")+1,-1))&&(a=a.slice(0,i.index)+"["+"a".repeat(i[0].length-2)+"]"+a.slice(this.tokenizer.rules.inline.reflinkSearch.lastIndex))}for(;null!=(i=this.tokenizer.rules.inline.blockSkip.exec(a));)a=a.slice(0,i.index)+"["+"a".repeat(i[0].length-2)+"]"+a.slice(this.tokenizer.rules.inline.blockSkip.lastIndex);for(;null!=(i=this.tokenizer.rules.inline.anyPunctuation.exec(a));)a=a.slice(0,i.index)+"++"+a.slice(this.tokenizer.rules.inline.anyPunctuation.lastIndex);for(;e;)if(l||(o=""),l=!1,!(this.options.extensions&&this.options.extensions.inline&&this.options.extensions.inline.some((s=>!!(n=s.call({lexer:this},e,t))&&(e=e.substring(n.raw.length),t.push(n),!0)))))if(n=this.tokenizer.escape(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.tag(e))e=e.substring(n.raw.length),s=t[t.length-1],s&&"text"===n.type&&"text"===s.type?(s.raw+=n.raw,s.text+=n.text):t.push(n);else if(n=this.tokenizer.link(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.reflink(e,this.tokens.links))e=e.substring(n.raw.length),s=t[t.length-1],s&&"text"===n.type&&"text"===s.type?(s.raw+=n.raw,s.text+=n.text):t.push(n);else if(n=this.tokenizer.emStrong(e,a,o))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.codespan(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.br(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.del(e))e=e.substring(n.raw.length),t.push(n);else if(n=this.tokenizer.autolink(e))e=e.substring(n.raw.length),t.push(n);else if(this.state.inLink||!(n=this.tokenizer.url(e))){if(r=e,this.options.extensions&&this.options.extensions.startInline){let t=1/0;const n=e.slice(1);let s;this.options.extensions.startInline.forEach((e=>{s=e.call({lexer:this},n),"number"==typeof s&&s>=0&&(t=Math.min(t,s))})),t<1/0&&t>=0&&(r=e.substring(0,t+1))}if(n=this.tokenizer.inlineText(r))e=e.substring(n.raw.length),"_"!==n.raw.slice(-1)&&(o=n.raw.slice(-1)),l=!0,s=t[t.length-1],s&&"text"===s.type?(s.raw+=n.raw,s.text+=n.text):t.push(n);else if(e){const t="Infinite loop on byte: "+e.charCodeAt(0);if(this.options.silent){console.error(t);break}throw new Error(t)}}else e=e.substring(n.raw.length),t.push(n);return t}}class se{options;constructor(t){this.options=t||e.defaults}code(e,t,n){const s=(t||"").match(/^\S*/)?.[0];return e=e.replace(/\n$/,"")+"\n",s?'<pre><code class="language-'+c(s)+'">'+(n?e:c(e,!0))+"</code></pre>\n":"<pre><code>"+(n?e:c(e,!0))+"</code></pre>\n"}blockquote(e){return`<blockquote>\n${e}</blockquote>\n`}html(e,t){return e}heading(e,t,n){return`<h${t}>${e}</h${t}>\n`}hr(){return"<hr>\n"}list(e,t,n){const s=t?"ol":"ul";return"<"+s+(t&&1!==n?' start="'+n+'"':"")+">\n"+e+"</"+s+">\n"}listitem(e,t,n){return`<li>${e}</li>\n`}checkbox(e){return"<input "+(e?'checked="" ':"")+'disabled="" type="checkbox">'}paragraph(e){return`<p>${e}</p>\n`}table(e,t){return t&&(t=`<tbody>${t}</tbody>`),"<table>\n<thead>\n"+e+"</thead>\n"+t+"</table>\n"}tablerow(e){return`<tr>\n${e}</tr>\n`}tablecell(e,t){const n=t.header?"th":"td";return(t.align?`<${n} align="${t.align}">`:`<${n}>`)+e+`</${n}>\n`}strong(e){return`<strong>${e}</strong>`}em(e){return`<em>${e}</em>`}codespan(e){return`<code>${e}</code>`}br(){return"<br>"}del(e){return`<del>${e}</del>`}link(e,t,n){const s=g(e);if(null===s)return n;let r='<a href="'+(e=s)+'"';return t&&(r+=' title="'+t+'"'),r+=">"+n+"</a>",r}image(e,t,n){const s=g(e);if(null===s)return n;let r=`<img src="${e=s}" alt="${n}"`;return t&&(r+=` title="${t}"`),r+=">",r}text(e){return e}}class re{strong(e){return e}em(e){return e}codespan(e){return e}del(e){return e}html(e){return e}text(e){return e}link(e,t,n){return""+n}image(e,t,n){return""+n}br(){return""}}class ie{options;renderer;textRenderer;constructor(t){this.options=t||e.defaults,this.options.renderer=this.options.renderer||new se,this.renderer=this.options.renderer,this.renderer.options=this.options,this.textRenderer=new re}static parse(e,t){return new ie(t).parse(e)}static parseInline(e,t){return new ie(t).parseInline(e)}parse(e,t=!0){let n="";for(let s=0;s<e.length;s++){const r=e[s];if(this.options.extensions&&this.options.extensions.renderers&&this.options.extensions.renderers[r.type]){const e=r,t=this.options.extensions.renderers[e.type].call({parser:this},e);if(!1!==t||!["space","hr","heading","code","table","blockquote","list","html","paragraph","text"].includes(e.type)){n+=t||"";continue}}switch(r.type){case"space":continue;case"hr":n+=this.renderer.hr();continue;case"heading":{const e=r;n+=this.renderer.heading(this.parseInline(e.tokens),e.depth,p(this.parseInline(e.tokens,this.textRenderer)));continue}case"code":{const e=r;n+=this.renderer.code(e.text,e.lang,!!e.escaped);continue}case"table":{const e=r;let t="",s="";for(let t=0;t<e.header.length;t++)s+=this.renderer.tablecell(this.parseInline(e.header[t].tokens),{header:!0,align:e.align[t]});t+=this.renderer.tablerow(s);let i="";for(let t=0;t<e.rows.length;t++){const n=e.rows[t];s="";for(let t=0;t<n.length;t++)s+=this.renderer.tablecell(this.parseInline(n[t].tokens),{header:!1,align:e.align[t]});i+=this.renderer.tablerow(s)}n+=this.renderer.table(t,i);continue}case"blockquote":{const e=r,t=this.parse(e.tokens);n+=this.renderer.blockquote(t);continue}case"list":{const e=r,t=e.ordered,s=e.start,i=e.loose;let l="";for(let t=0;t<e.items.length;t++){const n=e.items[t],s=n.checked,r=n.task;let o="";if(n.task){const e=this.renderer.checkbox(!!s);i?n.tokens.length>0&&"paragraph"===n.tokens[0].type?(n.tokens[0].text=e+" "+n.tokens[0].text,n.tokens[0].tokens&&n.tokens[0].tokens.length>0&&"text"===n.tokens[0].tokens[0].type&&(n.tokens[0].tokens[0].text=e+" "+n.tokens[0].tokens[0].text)):n.tokens.unshift({type:"text",text:e+" "}):o+=e+" "}o+=this.parse(n.tokens,i),l+=this.renderer.listitem(o,r,!!s)}n+=this.renderer.list(l,t,s);continue}case"html":{const e=r;n+=this.renderer.html(e.text,e.block);continue}case"paragraph":{const e=r;n+=this.renderer.paragraph(this.parseInline(e.tokens));continue}case"text":{let i=r,l=i.tokens?this.parseInline(i.tokens):i.text;for(;s+1<e.length&&"text"===e[s+1].type;)i=e[++s],l+="\n"+(i.tokens?this.parseInline(i.tokens):i.text);n+=t?this.renderer.paragraph(l):l;continue}default:{const e='Token with "'+r.type+'" type was not found.';if(this.options.silent)return console.error(e),"";throw new Error(e)}}}return n}parseInline(e,t){t=t||this.renderer;let n="";for(let s=0;s<e.length;s++){const r=e[s];if(this.options.extensions&&this.options.extensions.renderers&&this.options.extensions.renderers[r.type]){const e=this.options.extensions.renderers[r.type].call({parser:this},r);if(!1!==e||!["escape","html","link","image","strong","em","codespan","br","del","text"].includes(r.type)){n+=e||"";continue}}switch(r.type){case"escape":{const e=r;n+=t.text(e.text);break}case"html":{const e=r;n+=t.html(e.text);break}case"link":{const e=r;n+=t.link(e.href,e.title,this.parseInline(e.tokens,t));break}case"image":{const e=r;n+=t.image(e.href,e.title,e.text);break}case"strong":{const e=r;n+=t.strong(this.parseInline(e.tokens,t));break}case"em":{const e=r;n+=t.em(this.parseInline(e.tokens,t));break}case"codespan":{const e=r;n+=t.codespan(e.text);break}case"br":n+=t.br();break;case"del":{const e=r;n+=t.del(this.parseInline(e.tokens,t));break}case"text":{const e=r;n+=t.text(e.text);break}default:{const e='Token with "'+r.type+'" type was not found.';if(this.options.silent)return console.error(e),"";throw new Error(e)}}}return n}}class le{options;constructor(t){this.options=t||e.defaults}static passThroughHooks=new Set(["preprocess","postprocess","processAllTokens"]);preprocess(e){return e}postprocess(e){return e}processAllTokens(e){return e}}class oe{defaults={async:!1,breaks:!1,extensions:null,gfm:!0,hooks:null,pedantic:!1,renderer:null,silent:!1,tokenizer:null,walkTokens:null};options=this.setOptions;parse=this.#e(ne.lex,ie.parse);parseInline=this.#e(ne.lexInline,ie.parseInline);Parser=ie;Renderer=se;TextRenderer=re;Lexer=ne;Tokenizer=w;Hooks=le;constructor(...e){this.use(...e)}walkTokens(e,t){let n=[];for(const s of e)switch(n=n.concat(t.call(this,s)),s.type){case"table":{const e=s;for(const s of e.header)n=n.concat(this.walkTokens(s.tokens,t));for(const s of e.rows)for(const e of s)n=n.concat(this.walkTokens(e.tokens,t));break}case"list":{const e=s;n=n.concat(this.walkTokens(e.items,t));break}default:{const e=s;this.defaults.extensions?.childTokens?.[e.type]?this.defaults.extensions.childTokens[e.type].forEach((s=>{n=n.concat(this.walkTokens(e[s],t))})):e.tokens&&(n=n.concat(this.walkTokens(e.tokens,t)))}}return n}use(...e){const t=this.defaults.extensions||{renderers:{},childTokens:{}};return e.forEach((e=>{const n={...e};if(n.async=this.defaults.async||n.async||!1,e.extensions&&(e.extensions.forEach((e=>{if(!e.name)throw new Error("extension name required");if("renderer"in e){const n=t.renderers[e.name];t.renderers[e.name]=n?function(...t){let s=e.renderer.apply(this,t);return!1===s&&(s=n.apply(this,t)),s}:e.renderer}if("tokenizer"in e){if(!e.level||"block"!==e.level&&"inline"!==e.level)throw new Error("extension level must be 'block' or 'inline'");const n=t[e.level];n?n.unshift(e.tokenizer):t[e.level]=[e.tokenizer],e.start&&("block"===e.level?t.startBlock?t.startBlock.push(e.start):t.startBlock=[e.start]:"inline"===e.level&&(t.startInline?t.startInline.push(e.start):t.startInline=[e.start]))}"childTokens"in e&&e.childTokens&&(t.childTokens[e.name]=e.childTokens)})),n.extensions=t),e.renderer){const t=this.defaults.renderer||new se(this.defaults);for(const n in e.renderer){if(!(n in t))throw new Error(`renderer '${n}' does not exist`);if("options"===n)continue;const s=n,r=e.renderer[s],i=t[s];t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n||""}}n.renderer=t}if(e.tokenizer){const t=this.defaults.tokenizer||new w(this.defaults);for(const n in e.tokenizer){if(!(n in t))throw new Error(`tokenizer '${n}' does not exist`);if(["options","rules","lexer"].includes(n))continue;const s=n,r=e.tokenizer[s],i=t[s];t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n}}n.tokenizer=t}if(e.hooks){const t=this.defaults.hooks||new le;for(const n in e.hooks){if(!(n in t))throw new Error(`hook '${n}' does not exist`);if("options"===n)continue;const s=n,r=e.hooks[s],i=t[s];le.passThroughHooks.has(n)?t[s]=e=>{if(this.defaults.async)return Promise.resolve(r.call(t,e)).then((e=>i.call(t,e)));const n=r.call(t,e);return i.call(t,n)}:t[s]=(...e)=>{let n=r.apply(t,e);return!1===n&&(n=i.apply(t,e)),n}}n.hooks=t}if(e.walkTokens){const t=this.defaults.walkTokens,s=e.walkTokens;n.walkTokens=function(e){let n=[];return n.push(s.call(this,e)),t&&(n=n.concat(t.call(this,e))),n}}this.defaults={...this.defaults,...n}})),this}setOptions(e){return this.defaults={...this.defaults,...e},this}lexer(e,t){return ne.lex(e,t??this.defaults)}parser(e,t){return ie.parse(e,t??this.defaults)}#e(e,t){return(n,s)=>{const r={...s},i={...this.defaults,...r};!0===this.defaults.async&&!1===r.async&&(i.silent||console.warn("marked(): The async option was set to true by an extension. The async: false option sent to parse will be ignored."),i.async=!0);const l=this.#t(!!i.silent,!!i.async);if(null==n)return l(new Error("marked(): input parameter is undefined or null"));if("string"!=typeof n)return l(new Error("marked(): input parameter is of type "+Object.prototype.toString.call(n)+", string expected"));if(i.hooks&&(i.hooks.options=i),i.async)return Promise.resolve(i.hooks?i.hooks.preprocess(n):n).then((t=>e(t,i))).then((e=>i.hooks?i.hooks.processAllTokens(e):e)).then((e=>i.walkTokens?Promise.all(this.walkTokens(e,i.walkTokens)).then((()=>e)):e)).then((e=>t(e,i))).then((e=>i.hooks?i.hooks.postprocess(e):e)).catch(l);try{i.hooks&&(n=i.hooks.preprocess(n));let s=e(n,i);i.hooks&&(s=i.hooks.processAllTokens(s)),i.walkTokens&&this.walkTokens(s,i.walkTokens);let r=t(s,i);return i.hooks&&(r=i.hooks.postprocess(r)),r}catch(e){return l(e)}}}#t(e,t){return n=>{if(n.message+="\nPlease report this to https://github.com/markedjs/marked.",e){const e="<p>An error occurred:</p><pre>"+c(n.message+"",!0)+"</pre>";return t?Promise.resolve(e):e}if(t)return Promise.reject(n);throw n}}}const ae=new oe;function ce(e,t){return ae.parse(e,t)}ce.options=ce.setOptions=function(e){return ae.setOptions(e),ce.defaults=ae.defaults,n(ce.defaults),ce},ce.getDefaults=t,ce.defaults=e.defaults,ce.use=function(...e){return ae.use(...e),ce.defaults=ae.defaults,n(ce.defaults),ce},ce.walkTokens=function(e,t){return ae.walkTokens(e,t)},ce.parseInline=ae.parseInline,ce.Parser=ie,ce.parser=ie.parse,ce.Renderer=se,ce.TextRenderer=re,ce.Lexer=ne,ce.lexer=ne.lex,ce.Tokenizer=w,ce.Hooks=le,ce.parse=ce;const he=ce.options,pe=ce.setOptions,ue=ce.use,ke=ce.walkTokens,ge=ce.parseInline,fe=ce,de=ie.parse,xe=ne.lex;e.Hooks=le,e.Lexer=ne,e.Marked=oe,e.Parser=ie,e.Renderer=se,e.TextRenderer=re,e.Tokenizer=w,e.getDefaults=t,e.lexer=xe,e.marked=ce,e.options=he,e.parse=fe,e.parseInline=ge,e.parser=de,e.setOptions=pe,e.use=ue,e.walkTokens=ke}));

</script>
<script>
/*! @license DOMPurify 3.1.6 | (c) Cure53 and other contributors | Released under the Apache license 2.0 and Mozilla Public License 2.0 | github.com/cure53/DOMPurify/blob/3.1.6/LICENSE */
!function(e,t){"object"==typeof exports&&"undefined"!=typeof module?module.exports=t():"function"==typeof define&&define.amd?define(t):(e="undefined"!=typeof globalThis?globalThis:e||self).DOMPurify=t()}(this,(function(){"use strict";const{entries:e,setPrototypeOf:t,isFrozen:n,getPrototypeOf:o,getOwnPropertyDescriptor:r}=Object;let{freeze:i,seal:a,create:l}=Object,{apply:c,construct:s}="undefined"!=typeof Reflect&&Reflect;i||(i=function(e){return e}),a||(a=function(e){return e}),c||(c=function(e,t,n){return e.apply(t,n)}),s||(s=function(e,t){return new e(...t)});const u=b(Array.prototype.forEach),m=b(Array.prototype.pop),p=b(Array.prototype.push),f=b(String.prototype.toLowerCase),d=b(String.prototype.toString),h=b(String.prototype.match),g=b(String.prototype.replace),T=b(String.prototype.indexOf),y=b(String.prototype.trim),E=b(Object.prototype.hasOwnProperty),_=b(RegExp.prototype.test),A=(N=TypeError,function(){for(var e=arguments.length,t=new Array(e),n=0;n<e;n++)t[n]=arguments[n];return s(N,t)});var N;function b(e){return function(t){for(var n=arguments.length,o=new Array(n>1?n-1:0),r=1;r<n;r++)o[r-1]=arguments[r];return c(e,t,o)}}function S(e,o){let r=arguments.length>2&&void 0!==arguments[2]?arguments[2]:f;t&&t(e,null);let i=o.length;for(;i--;){let t=o[i];if("string"==typeof t){const e=r(t);e!==t&&(n(o)||(o[i]=e),t=e)}e[t]=!0}return e}function R(e){for(let t=0;t<e.length;t++){E(e,t)||(e[t]=null)}return e}function w(t){const n=l(null);for(const[o,r]of e(t)){E(t,o)&&(Array.isArray(r)?n[o]=R(r):r&&"object"==typeof r&&r.constructor===Object?n[o]=w(r):n[o]=r)}return n}function C(e,t){for(;null!==e;){const n=r(e,t);if(n){if(n.get)return b(n.get);if("function"==typeof n.value)return b(n.value)}e=o(e)}return function(){return null}}const L=i(["a","abbr","acronym","address","area","article","aside","audio","b","bdi","bdo","big","blink","blockquote","body","br","button","canvas","caption","center","cite","code","col","colgroup","content","data","datalist","dd","decorator","del","details","dfn","dialog","dir","div","dl","dt","element","em","fieldset","figcaption","figure","font","footer","form","h1","h2","h3","h4","h5","h6","head","header","hgroup","hr","html","i","img","input","ins","kbd","label","legend","li","main","map","mark","marquee","menu","menuitem","meter","nav","nobr","ol","optgroup","option","output","p","picture","pre","progress","q","rp","rt","ruby","s","samp","section","select","shadow","small","source","spacer","span","strike","strong","style","sub","summary","sup","table","tbody","td","template","textarea","tfoot","th","thead","time","tr","track","tt","u","ul","var","video","wbr"]),D=i(["svg","a","altglyph","altglyphdef","altglyphitem","animatecolor","animatemotion","animatetransform","circle","clippath","defs","desc","ellipse","filter","font","g","glyph","glyphref","hkern","image","line","lineargradient","marker","mask","metadata","mpath","path","pattern","polygon","polyline","radialgradient","rect","stop","style","switch","symbol","text","textpath","title","tref","tspan","view","vkern"]),v=i(["feBlend","feColorMatrix","feComponentTransfer","feComposite","feConvolveMatrix","feDiffuseLighting","feDisplacementMap","feDistantLight","feDropShadow","feFlood","feFuncA","feFuncB","feFuncG","feFuncR","feGaussianBlur","feImage","feMerge","feMergeNode","feMorphology","feOffset","fePointLight","feSpecularLighting","feSpotLight","feTile","feTurbulence"]),O=i(["animate","color-profile","cursor","discard","font-face","font-face-format","font-face-name","font-face-src","font-face-uri","foreignobject","hatch","hatchpath","mesh","meshgradient","meshpatch","meshrow","missing-glyph","script","set","solidcolor","unknown","use"]),x=i(["math","menclose","merror","mfenced","mfrac","mglyph","mi","mlabeledtr","mmultiscripts","mn","mo","mover","mpadded","mphantom","mroot","mrow","ms","mspace","msqrt","mstyle","msub","msup","msubsup","mtable","mtd","mtext","mtr","munder","munderover","mprescripts"]),k=i(["maction","maligngroup","malignmark","mlongdiv","mscarries","mscarry","msgroup","mstack","msline","msrow","semantics","annotation","annotation-xml","mprescripts","none"]),M=i(["#text"]),I=i(["accept","action","align","alt","autocapitalize","autocomplete","autopictureinpicture","autoplay","background","bgcolor","border","capture","cellpadding","cellspacing","checked","cite","class","clear","color","cols","colspan","controls","controlslist","coords","crossorigin","datetime","decoding","default","dir","disabled","disablepictureinpicture","disableremoteplayback","download","draggable","enctype","enterkeyhint","face","for","headers","height","hidden","high","href","hreflang","id","inputmode","integrity","ismap","kind","label","lang","list","loading","loop","low","max","maxlength","media","method","min","minlength","multiple","muted","name","nonce","noshade","novalidate","nowrap","open","optimum","pattern","placeholder","playsinline","popover","popovertarget","popovertargetaction","poster","preload","pubdate","radiogroup","readonly","rel","required","rev","reversed","role","rows","rowspan","spellcheck","scope","selected","shape","size","sizes","span","srclang","start","src","srcset","step","style","summary","tabindex","title","translate","type","usemap","valign","value","width","wrap","xmlns","slot"]),U=i(["accent-height","accumulate","additive","alignment-baseline","ascent","attributename","attributetype","azimuth","basefrequency","baseline-shift","begin","bias","by","class","clip","clippathunits","clip-path","clip-rule","color","color-interpolation","color-interpolation-filters","color-profile","color-rendering","cx","cy","d","dx","dy","diffuseconstant","direction","display","divisor","dur","edgemode","elevation","end","fill","fill-opacity","fill-rule","filter","filterunits","flood-color","flood-opacity","font-family","font-size","font-size-adjust","font-stretch","font-style","font-variant","font-weight","fx","fy","g1","g2","glyph-name","glyphref","gradientunits","gradienttransform","height","href","id","image-rendering","in","in2","k","k1","k2","k3","k4","kerning","keypoints","keysplines","keytimes","lang","lengthadjust","letter-spacing","kernelmatrix","kernelunitlength","lighting-color","local","marker-end","marker-mid","marker-start","markerheight","markerunits","markerwidth","maskcontentunits","maskunits","max","mask","media","method","mode","min","name","numoctaves","offset","operator","opacity","order","orient","orientation","origin","overflow","paint-order","path","pathlength","patterncontentunits","patterntransform","patternunits","points","preservealpha","preserveaspectratio","primitiveunits","r","rx","ry","radius","refx","refy","repeatcount","repeatdur","restart","result","rotate","scale","seed","shape-rendering","specularconstant","specularexponent","spreadmethod","startoffset","stddeviation","stitchtiles","stop-color","stop-opacity","stroke-dasharray","stroke-dashoffset","stroke-linecap","stroke-linejoin","stroke-miterlimit","stroke-opacity","stroke","stroke-width","style","surfacescale","systemlanguage","tabindex","targetx","targety","transform","transform-origin","text-anchor","text-decoration","text-rendering","textlength","type","u1","u2","unicode","values","viewbox","visibility","version","vert-adv-y","vert-origin-x","vert-origin-y","width","word-spacing","wrap","writing-mode","xchannelselector","ychannelselector","x","x1","x2","xmlns","y","y1","y2","z","zoomandpan"]),P=i(["accent","accentunder","align","bevelled","close","columnsalign","columnlines","columnspan","denomalign","depth","dir","display","displaystyle","encoding","fence","frame","height","href","id","largeop","length","linethickness","lspace","lquote","mathbackground","mathcolor","mathsize","mathvariant","maxsize","minsize","movablelimits","notation","numalign","open","rowalign","rowlines","rowspacing","rowspan","rspace","rquote","scriptlevel","scriptminsize","scriptsizemultiplier","selection","separator","separators","stretchy","subscriptshift","supscriptshift","symmetric","voffset","width","xmlns"]),F=i(["xlink:href","xml:id","xlink:title","xml:space","xmlns:xlink"]),H=a(/\{\{[\w\W]*|[\w\W]*\}\}/gm),z=a(/<%[\w\W]*|[\w\W]*%>/gm),B=a(/\${[\w\W]*}/gm),W=a(/^data-[\-\w.\u00B7-\uFFFF]/),G=a(/^aria-[\-\w]+$/),Y=a(/^(?:(?:(?:f|ht)tps?|mailto|tel|callto|sms|cid|xmpp):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i),j=a(/^(?:\w+script|data):/i),X=a(/[\u0000-\u0020\u00A0\u1680\u180E\u2000-\u2029\u205F\u3000]/g),q=a(/^html$/i),$=a(/^[a-z][.\w]*(-[.\w]+)+$/i);var K=Object.freeze({__proto__:null,MUSTACHE_EXPR:H,ERB_EXPR:z,TMPLIT_EXPR:B,DATA_ATTR:W,ARIA_ATTR:G,IS_ALLOWED_URI:Y,IS_SCRIPT_OR_DATA:j,ATTR_WHITESPACE:X,DOCTYPE_NAME:q,CUSTOM_ELEMENT:$});const V=1,Z=3,J=7,Q=8,ee=9,te=function(){return"undefined"==typeof window?null:window};var ne=function t(){let n=arguments.length>0&&void 0!==arguments[0]?arguments[0]:te();const o=e=>t(e);if(o.version="3.1.6",o.removed=[],!n||!n.document||n.document.nodeType!==ee)return o.isSupported=!1,o;let{document:r}=n;const a=r,c=a.currentScript,{DocumentFragment:s,HTMLTemplateElement:N,Node:b,Element:R,NodeFilter:H,NamedNodeMap:z=n.NamedNodeMap||n.MozNamedAttrMap,HTMLFormElement:B,DOMParser:W,trustedTypes:G}=n,j=R.prototype,X=C(j,"cloneNode"),$=C(j,"remove"),ne=C(j,"nextSibling"),oe=C(j,"childNodes"),re=C(j,"parentNode");if("function"==typeof N){const e=r.createElement("template");e.content&&e.content.ownerDocument&&(r=e.content.ownerDocument)}let ie,ae="";const{implementation:le,createNodeIterator:ce,createDocumentFragment:se,getElementsByTagName:ue}=r,{importNode:me}=a;let pe={};o.isSupported="function"==typeof e&&"function"==typeof re&&le&&void 0!==le.createHTMLDocument;const{MUSTACHE_EXPR:fe,ERB_EXPR:de,TMPLIT_EXPR:he,DATA_ATTR:ge,ARIA_ATTR:Te,IS_SCRIPT_OR_DATA:ye,ATTR_WHITESPACE:Ee,CUSTOM_ELEMENT:_e}=K;let{IS_ALLOWED_URI:Ae}=K,Ne=null;const be=S({},[...L,...D,...v,...x,...M]);let Se=null;const Re=S({},[...I,...U,...P,...F]);let we=Object.seal(l(null,{tagNameCheck:{writable:!0,configurable:!1,enumerable:!0,value:null},attributeNameCheck:{writable:!0,configurable:!1,enumerable:!0,value:null},allowCustomizedBuiltInElements:{writable:!0,configurable:!1,enumerable:!0,value:!1}})),Ce=null,Le=null,De=!0,ve=!0,Oe=!1,xe=!0,ke=!1,Me=!0,Ie=!1,Ue=!1,Pe=!1,Fe=!1,He=!1,ze=!1,Be=!0,We=!1,Ge=!0,Ye=!1,je={},Xe=null;const qe=S({},["annotation-xml","audio","colgroup","desc","foreignobject","head","iframe","math","mi","mn","mo","ms","mtext","noembed","noframes","noscript","plaintext","script","style","svg","template","thead","title","video","xmp"]);let $e=null;const Ke=S({},["audio","video","img","source","image","track"]);let Ve=null;const Ze=S({},["alt","class","for","id","label","name","pattern","placeholder","role","summary","title","value","style","xmlns"]),Je="http://www.w3.org/1998/Math/MathML",Qe="http://www.w3.org/2000/svg",et="http://www.w3.org/1999/xhtml";let tt=et,nt=!1,ot=null;const rt=S({},[Je,Qe,et],d);let it=null;const at=["application/xhtml+xml","text/html"];let lt=null,ct=null;const st=r.createElement("form"),ut=function(e){return e instanceof RegExp||e instanceof Function},mt=function(){let e=arguments.length>0&&void 0!==arguments[0]?arguments[0]:{};if(!ct||ct!==e){if(e&&"object"==typeof e||(e={}),e=w(e),it=-1===at.indexOf(e.PARSER_MEDIA_TYPE)?"text/html":e.PARSER_MEDIA_TYPE,lt="application/xhtml+xml"===it?d:f,Ne=E(e,"ALLOWED_TAGS")?S({},e.ALLOWED_TAGS,lt):be,Se=E(e,"ALLOWED_ATTR")?S({},e.ALLOWED_ATTR,lt):Re,ot=E(e,"ALLOWED_NAMESPACES")?S({},e.ALLOWED_NAMESPACES,d):rt,Ve=E(e,"ADD_URI_SAFE_ATTR")?S(w(Ze),e.ADD_URI_SAFE_ATTR,lt):Ze,$e=E(e,"ADD_DATA_URI_TAGS")?S(w(Ke),e.ADD_DATA_URI_TAGS,lt):Ke,Xe=E(e,"FORBID_CONTENTS")?S({},e.FORBID_CONTENTS,lt):qe,Ce=E(e,"FORBID_TAGS")?S({},e.FORBID_TAGS,lt):{},Le=E(e,"FORBID_ATTR")?S({},e.FORBID_ATTR,lt):{},je=!!E(e,"USE_PROFILES")&&e.USE_PROFILES,De=!1!==e.ALLOW_ARIA_ATTR,ve=!1!==e.ALLOW_DATA_ATTR,Oe=e.ALLOW_UNKNOWN_PROTOCOLS||!1,xe=!1!==e.ALLOW_SELF_CLOSE_IN_ATTR,ke=e.SAFE_FOR_TEMPLATES||!1,Me=!1!==e.SAFE_FOR_XML,Ie=e.WHOLE_DOCUMENT||!1,Fe=e.RETURN_DOM||!1,He=e.RETURN_DOM_FRAGMENT||!1,ze=e.RETURN_TRUSTED_TYPE||!1,Pe=e.FORCE_BODY||!1,Be=!1!==e.SANITIZE_DOM,We=e.SANITIZE_NAMED_PROPS||!1,Ge=!1!==e.KEEP_CONTENT,Ye=e.IN_PLACE||!1,Ae=e.ALLOWED_URI_REGEXP||Y,tt=e.NAMESPACE||et,we=e.CUSTOM_ELEMENT_HANDLING||{},e.CUSTOM_ELEMENT_HANDLING&&ut(e.CUSTOM_ELEMENT_HANDLING.tagNameCheck)&&(we.tagNameCheck=e.CUSTOM_ELEMENT_HANDLING.tagNameCheck),e.CUSTOM_ELEMENT_HANDLING&&ut(e.CUSTOM_ELEMENT_HANDLING.attributeNameCheck)&&(we.attributeNameCheck=e.CUSTOM_ELEMENT_HANDLING.attributeNameCheck),e.CUSTOM_ELEMENT_HANDLING&&"boolean"==typeof e.CUSTOM_ELEMENT_HANDLING.allowCustomizedBuiltInElements&&(we.allowCustomizedBuiltInElements=e.CUSTOM_ELEMENT_HANDLING.allowCustomizedBuiltInElements),ke&&(ve=!1),He&&(Fe=!0),je&&(Ne=S({},M),Se=[],!0===je.html&&(S(Ne,L),S(Se,I)),!0===je.svg&&(S(Ne,D),S(Se,U),S(Se,F)),!0===je.svgFilters&&(S(Ne,v),S(Se,U),S(Se,F)),!0===je.mathMl&&(S(Ne,x),S(Se,P),S(Se,F))),e.ADD_TAGS&&(Ne===be&&(Ne=w(Ne)),S(Ne,e.ADD_TAGS,lt)),e.ADD_ATTR&&(Se===Re&&(Se=w(Se)),S(Se,e.ADD_ATTR,lt)),e.ADD_URI_SAFE_ATTR&&S(Ve,e.ADD_URI_SAFE_ATTR,lt),e.FORBID_CONTENTS&&(Xe===qe&&(Xe=w(Xe)),S(Xe,e.FORBID_CONTENTS,lt)),Ge&&(Ne["#text"]=!0),Ie&&S(Ne,["html","head","body"]),Ne.table&&(S(Ne,["tbody"]),delete Ce.tbody),e.TRUSTED_TYPES_POLICY){if("function"!=typeof e.TRUSTED_TYPES_POLICY.createHTML)throw A('TRUSTED_TYPES_POLICY configuration option must provide a "createHTML" hook.');if("function"!=typeof e.TRUSTED_TYPES_POLICY.createScriptURL)throw A('TRUSTED_TYPES_POLICY configuration option must provide a "createScriptURL" hook.');ie=e.TRUSTED_TYPES_POLICY,ae=ie.createHTML("")}else void 0===ie&&(ie=function(e,t){if("object"!=typeof e||"function"!=typeof e.createPolicy)return null;let n=null;const o="data-tt-policy-suffix";t&&t.hasAttribute(o)&&(n=t.getAttribute(o));const r="dompurify"+(n?"#"+n:"");try{return e.createPolicy(r,{createHTML:e=>e,createScriptURL:e=>e})}catch(e){return console.warn("TrustedTypes policy "+r+" could not be created."),null}}(G,c)),null!==ie&&"string"==typeof ae&&(ae=ie.createHTML(""));i&&i(e),ct=e}},pt=S({},["mi","mo","mn","ms","mtext"]),ft=S({},["foreignobject","annotation-xml"]),dt=S({},["title","style","font","a","script"]),ht=S({},[...D,...v,...O]),gt=S({},[...x,...k]),Tt=function(e){p(o.removed,{element:e});try{re(e).removeChild(e)}catch(t){$(e)}},yt=function(e,t){try{p(o.removed,{attribute:t.getAttributeNode(e),from:t})}catch(e){p(o.removed,{attribute:null,from:t})}if(t.removeAttribute(e),"is"===e&&!Se[e])if(Fe||He)try{Tt(t)}catch(e){}else try{t.setAttribute(e,"")}catch(e){}},Et=function(e){let t=null,n=null;if(Pe)e="<remove></remove>"+e;else{const t=h(e,/^[\r\n\t ]+/);n=t&&t[0]}"application/xhtml+xml"===it&&tt===et&&(e='<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>'+e+"</body></html>");const o=ie?ie.createHTML(e):e;if(tt===et)try{t=(new W).parseFromString(o,it)}catch(e){}if(!t||!t.documentElement){t=le.createDocument(tt,"template",null);try{t.documentElement.innerHTML=nt?ae:o}catch(e){}}const i=t.body||t.documentElement;return e&&n&&i.insertBefore(r.createTextNode(n),i.childNodes[0]||null),tt===et?ue.call(t,Ie?"html":"body")[0]:Ie?t.documentElement:i},_t=function(e){return ce.call(e.ownerDocument||e,e,H.SHOW_ELEMENT|H.SHOW_COMMENT|H.SHOW_TEXT|H.SHOW_PROCESSING_INSTRUCTION|H.SHOW_CDATA_SECTION,null)},At=function(e){return e instanceof B&&("string"!=typeof e.nodeName||"string"!=typeof e.textContent||"function"!=typeof e.removeChild||!(e.attributes instanceof z)||"function"!=typeof e.removeAttribute||"function"!=typeof e.setAttribute||"string"!=typeof e.namespaceURI||"function"!=typeof e.insertBefore||"function"!=typeof e.hasChildNodes)},Nt=function(e){return"function"==typeof b&&e instanceof b},bt=function(e,t,n){pe[e]&&u(pe[e],(e=>{e.call(o,t,n,ct)}))},St=function(e){let t=null;if(bt("beforeSanitizeElements",e,null),At(e))return Tt(e),!0;const n=lt(e.nodeName);if(bt("uponSanitizeElement",e,{tagName:n,allowedTags:Ne}),e.hasChildNodes()&&!Nt(e.firstElementChild)&&_(/<[/\w]/g,e.innerHTML)&&_(/<[/\w]/g,e.textContent))return Tt(e),!0;if(e.nodeType===J)return Tt(e),!0;if(Me&&e.nodeType===Q&&_(/<[/\w]/g,e.data))return Tt(e),!0;if(!Ne[n]||Ce[n]){if(!Ce[n]&&wt(n)){if(we.tagNameCheck instanceof RegExp&&_(we.tagNameCheck,n))return!1;if(we.tagNameCheck instanceof Function&&we.tagNameCheck(n))return!1}if(Ge&&!Xe[n]){const t=re(e)||e.parentNode,n=oe(e)||e.childNodes;if(n&&t){for(let o=n.length-1;o>=0;--o){const r=X(n[o],!0);r.__removalCount=(e.__removalCount||0)+1,t.insertBefore(r,ne(e))}}}return Tt(e),!0}return e instanceof R&&!function(e){let t=re(e);t&&t.tagName||(t={namespaceURI:tt,tagName:"template"});const n=f(e.tagName),o=f(t.tagName);return!!ot[e.namespaceURI]&&(e.namespaceURI===Qe?t.namespaceURI===et?"svg"===n:t.namespaceURI===Je?"svg"===n&&("annotation-xml"===o||pt[o]):Boolean(ht[n]):e.namespaceURI===Je?t.namespaceURI===et?"math"===n:t.namespaceURI===Qe?"math"===n&&ft[o]:Boolean(gt[n]):e.namespaceURI===et?!(t.namespaceURI===Qe&&!ft[o])&&!(t.namespaceURI===Je&&!pt[o])&&!gt[n]&&(dt[n]||!ht[n]):!("application/xhtml+xml"!==it||!ot[e.namespaceURI]))}(e)?(Tt(e),!0):"noscript"!==n&&"noembed"!==n&&"noframes"!==n||!_(/<\/no(script|embed|frames)/i,e.innerHTML)?(ke&&e.nodeType===Z&&(t=e.textContent,u([fe,de,he],(e=>{t=g(t,e," ")})),e.textContent!==t&&(p(o.removed,{element:e.cloneNode()}),e.textContent=t)),bt("afterSanitizeElements",e,null),!1):(Tt(e),!0)},Rt=function(e,t,n){if(Be&&("id"===t||"name"===t)&&(n in r||n in st))return!1;if(ve&&!Le[t]&&_(ge,t));else if(De&&_(Te,t));else if(!Se[t]||Le[t]){if(!(wt(e)&&(we.tagNameCheck instanceof RegExp&&_(we.tagNameCheck,e)||we.tagNameCheck instanceof Function&&we.tagNameCheck(e))&&(we.attributeNameCheck instanceof RegExp&&_(we.attributeNameCheck,t)||we.attributeNameCheck instanceof Function&&we.attributeNameCheck(t))||"is"===t&&we.allowCustomizedBuiltInElements&&(we.tagNameCheck instanceof RegExp&&_(we.tagNameCheck,n)||we.tagNameCheck instanceof Function&&we.tagNameCheck(n))))return!1}else if(Ve[t]);else if(_(Ae,g(n,Ee,"")));else if("src"!==t&&"xlink:href"!==t&&"href"!==t||"script"===e||0!==T(n,"data:")||!$e[e]){if(Oe&&!_(ye,g(n,Ee,"")));else if(n)return!1}else;return!0},wt=function(e){return"annotation-xml"!==e&&h(e,_e)},Ct=function(e){bt("beforeSanitizeAttributes",e,null);const{attributes:t}=e;if(!t)return;const n={attrName:"",attrValue:"",keepAttr:!0,allowedAttributes:Se};let r=t.length;for(;r--;){const i=t[r],{name:a,namespaceURI:l,value:c}=i,s=lt(a);let p="value"===a?c:y(c);if(n.attrName=s,n.attrValue=p,n.keepAttr=!0,n.forceKeepAttr=void 0,bt("uponSanitizeAttribute",e,n),p=n.attrValue,Me&&_(/((--!?|])>)|<\/(style|title)/i,p)){yt(a,e);continue}if(n.forceKeepAttr)continue;if(yt(a,e),!n.keepAttr)continue;if(!xe&&_(/\/>/i,p)){yt(a,e);continue}ke&&u([fe,de,he],(e=>{p=g(p,e," ")}));const f=lt(e.nodeName);if(Rt(f,s,p)){if(!We||"id"!==s&&"name"!==s||(yt(a,e),p="user-content-"+p),ie&&"object"==typeof G&&"function"==typeof G.getAttributeType)if(l);else switch(G.getAttributeType(f,s)){case"TrustedHTML":p=ie.createHTML(p);break;case"TrustedScriptURL":p=ie.createScriptURL(p)}try{l?e.setAttributeNS(l,a,p):e.setAttribute(a,p),At(e)?Tt(e):m(o.removed)}catch(e){}}}bt("afterSanitizeAttributes",e,null)},Lt=function e(t){let n=null;const o=_t(t);for(bt("beforeSanitizeShadowDOM",t,null);n=o.nextNode();)bt("uponSanitizeShadowNode",n,null),St(n)||(n.content instanceof s&&e(n.content),Ct(n));bt("afterSanitizeShadowDOM",t,null)};return o.sanitize=function(e){let t=arguments.length>1&&void 0!==arguments[1]?arguments[1]:{},n=null,r=null,i=null,l=null;if(nt=!e,nt&&(e="\x3c!--\x3e"),"string"!=typeof e&&!Nt(e)){if("function"!=typeof e.toString)throw A("toString is not a function");if("string"!=typeof(e=e.toString()))throw A("dirty is not a string, aborting")}if(!o.isSupported)return e;if(Ue||mt(t),o.removed=[],"string"==typeof e&&(Ye=!1),Ye){if(e.nodeName){const t=lt(e.nodeName);if(!Ne[t]||Ce[t])throw A("root node is forbidden and cannot be sanitized in-place")}}else if(e instanceof b)n=Et("\x3c!----\x3e"),r=n.ownerDocument.importNode(e,!0),r.nodeType===V&&"BODY"===r.nodeName||"HTML"===r.nodeName?n=r:n.appendChild(r);else{if(!Fe&&!ke&&!Ie&&-1===e.indexOf("<"))return ie&&ze?ie.createHTML(e):e;if(n=Et(e),!n)return Fe?null:ze?ae:""}n&&Pe&&Tt(n.firstChild);const c=_t(Ye?e:n);for(;i=c.nextNode();)St(i)||(i.content instanceof s&&Lt(i.content),Ct(i));if(Ye)return e;if(Fe){if(He)for(l=se.call(n.ownerDocument);n.firstChild;)l.appendChild(n.firstChild);else l=n;return(Se.shadowroot||Se.shadowrootmode)&&(l=me.call(a,l,!0)),l}let m=Ie?n.outerHTML:n.innerHTML;return Ie&&Ne["!doctype"]&&n.ownerDocument&&n.ownerDocument.doctype&&n.ownerDocument.doctype.name&&_(q,n.ownerDocument.doctype.name)&&(m="<!DOCTYPE "+n.ownerDocument.doctype.name+">\n"+m),ke&&u([fe,de,he],(e=>{m=g(m,e," ")})),ie&&ze?ie.createHTML(m):m},o.setConfig=function(){mt(arguments.length>0&&void 0!==arguments[0]?arguments[0]:{}),Ue=!0},o.clearConfig=function(){ct=null,Ue=!1},o.isValidAttribute=function(e,t,n){ct||mt({});const o=lt(e),r=lt(t);return Rt(o,r,n)},o.addHook=function(e,t){"function"==typeof t&&(pe[e]=pe[e]||[],p(pe[e],t))},o.removeHook=function(e){if(pe[e])return m(pe[e])},o.removeHooks=function(e){pe[e]&&(pe[e]=[])},o.removeAllHooks=function(){pe={}},o}();return ne}));
//# sourceMappingURL=purify.min.js.map

</script>
<script>
const messagesEl=document.getElementById('messages'), emptyEl=document.getElementById('empty'), composer=document.getElementById('composer'), runBtn=document.getElementById('run'), msgInput=document.getElementById('message'), fileInput=document.getElementById('fileInput'), drop=document.getElementById('drop'), filesEl=document.getElementById('files'), usageText=document.getElementById('usageText'), tokenBar=document.getElementById('tokenBar'), runnerStatus=document.getElementById('runnerStatus'), runnerLabel=document.getElementById('runnerLabel'), newMessagesBtn=document.getElementById('newMessages'), killProcessBtn=document.getElementById('killProcess'), themeToggleBtn=document.getElementById('themeToggle');
let selectedFiles=[], isRunning=false, phaseLabel='空闲', lastUpdated=0, messageCount=0, usageLoaded=false, usageLoading=false, unseenMessages=0, firstPaint=true, editInFlight=false;
let pollTimer=null, pollInFlight=false, pollQueued=false, pollGeneration=0;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function currentTheme(){
  return document.documentElement.getAttribute('data-theme')==='dark'?'dark':'light';
}
function applyTheme(theme, persist){
  const t=theme==='dark'?'dark':'light';
  document.documentElement.setAttribute('data-theme', t);
  if(persist!==false){
    try{localStorage.setItem('ae-theme', t)}catch(e){}
  }
  const btn=document.getElementById('themeToggle');
  if(btn){
    btn.title=t==='dark'?'切换为亮色主题':'切换为暗色主题';
    btn.setAttribute('aria-label', btn.title);
  }
}
function toggleTheme(){
  applyTheme(currentTheme()==='dark'?'light':'dark');
}
function highlightToolOutput(raw){
  const text=String(raw??'');
  if(!text) return esc('(无输出)');
  const lines=text.split('\n');
  const MAX_LINES=4000;
  const head=lines.length>MAX_LINES?lines.slice(0,MAX_LINES):lines;
  const rest=lines.length>MAX_LINES?lines.slice(MAX_LINES):null;
  const reTbHeader=/^Traceback \(most recent call last\):\s*$/;
  const reTbFile=/^\s*File "([^"]+)", line (\d+)(?:, in (.+))?\s*$/;
  const reTbCaret=/^\s*[\^~]+\s*$/;
  const reExc=/^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Error(?:Group)?(?:\s*:|\s*$)|^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Exception(?:\s*:|\s*$)/;
  const reToolErr=/^\[tool_error\]/;
  const reWarn=/^\s*(?:WARNING|WARN|Warning)\b/;
  const reOk=/^\s*(?:OK|SUCCESS|PASS(?:ED)?)\b/i;
  const rePrompt=/^(?:>>> |\.\.\. |\$ |# |> )/;
  const reSection=/^={3,}|^-{3,}\s*$|^#{1,6}\s+\S/;
  const span=(cls,s)=>`<span class="hl ${cls}">${s}</span>`;

  function highlightJsonish(line){
    let out='', i=0; const s=line;
    while(i<s.length){
      const ch=s[i];
      if(ch==='"' || ch==="'"){
        let j=i+1, escb=false;
        while(j<s.length){
          const c=s[j];
          if(escb){escb=false;j++;continue}
          if(c==='\\'){escb=true;j++;continue}
          if(c===ch){j++;break}
          j++;
        }
        const body=s.slice(i,j);
        let k=j; while(k<s.length && /\s/.test(s[k])) k++;
        out+=span(s[k]===':'?'hl-key':'hl-str', esc(body));
        i=j; continue;
      }
      const nm=/^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(s.slice(i));
      if(nm){ out+=span('hl-num', esc(nm[0])); i+=nm[0].length; continue; }
      const bw=/^(?:true|false|null|True|False|None)\b/.exec(s.slice(i));
      if(bw){ out+=span('hl-bool', esc(bw[0])); i+=bw[0].length; continue; }
      out+=esc(ch); i++;
    }
    return out;
  }

  function matchAt(s, i, re){
    re.lastIndex=0;
    const m=re.exec(s.slice(i));
    return m && m.index===0 ? m[0] : null;
  }

  function inlineHighlight(line){
    if(!line) return '';
    if((line.includes('{')||line.includes('[')) && line.includes(':') && line.length<2000){
      return highlightJsonish(line);
    }
    const rules=[
      {cls:'hl-url', re:/^https?:\/\/[^\s<>"']+/},
      {cls:'hl-path', re:/^(?:[A-Za-z]:\\|\/|\.\/|\.\.\/)[^\s:*,;"'<>|]+/},
      {cls:'hl-str', re:/^(['"`])(?:\\.|(?!\1)[\s\S])*\1/},
      {cls:'hl-num', re:/^-?\d+(?:\.\d+)?/},
      {cls:'hl-bool', re:/^(?:true|false|null|True|False|None)\b/},
      {cls:'hl-kw', re:/^(?:def|class|return|import|from|raise|except|try|with|async|await|print|len|if|else|elif|for|while|in|not|and|or)\b/},
      {cls:'hl-marker', re:/^(?:ERROR|FAIL(?:ED)?|Traceback|Exception|Error)\b/},
      {cls:'hl-ok', re:/^(?:OK|SUCCESS|PASS(?:ED)?)\b/},
      {cls:'hl-warn', re:/^(?:WARNING|WARN)\b/},
    ];
    let out='', i=0;
    while(i<line.length){
      let hit=null, cls='';
      const atWord = i===0 || !/[A-Za-z0-9_]/.test(line[i-1]);
      for(const r of rules){
        if((r.cls==='hl-num'||r.cls==='hl-bool'||r.cls==='hl-kw'||r.cls==='hl-marker'||r.cls==='hl-ok'||r.cls==='hl-warn') && !atWord) continue;
        const t=matchAt(line, i, r.re);
        if(t){ hit=t; cls=r.cls; break; }
      }
      if(hit){ out+=span(cls, esc(hit)); i+=hit.length; }
      else { out+=esc(line[i]); i++; }
    }
    return out;
  }

  function highlightTbFile(line){
    const m=line.match(/^\s*File "([^"]+)", line (\d+)(?:, in (.+))?\s*$/);
    if(!m) return inlineHighlight(line);
    const indent=line.match(/^\s*/)[0];
    let html=esc(indent)+span('hl-kw', esc('File'))+' '+span('hl-str', esc('"'+m[1]+'"'))+', ';
    html+=span('hl-kw', esc('line'))+' '+span('hl-line', esc(m[2]));
    if(m[3]) html+=', '+span('hl-kw', esc('in'))+' '+span('hl-file', esc(m[3]));
    return html;
  }

  function highlightExc(line){
    const em=line.match(/^((?:[A-Za-z_][\w]*\.)*[A-Za-z_]*(?:Error(?:Group)?|Exception))(\s*:?\s*)(.*)$/);
    if(em) return span('hl-err', esc(em[1]))+esc(em[2])+inlineHighlight(em[3]||'');
    return span('hl-err', esc(line));
  }

  const joined=head.join('\n');
  const looksTb=/Traceback \(most recent call last\):/.test(joined) || /^\[tool_error\]/m.test(joined);
  const looksJson=(()=>{
    const t=joined.trim();
    if(!(t.startsWith('{')||t.startsWith('[')) || t.length>200000) return false;
    try{ JSON.parse(t); return true; }catch(e){ return false; }
  })();

  const outLines=[];
  let inTb=false;
  for(const line of head){
    if(reToolErr.test(line)){ outLines.push(span('hl-err', esc(line))); inTb=true; continue; }
    if(reTbHeader.test(line)){ outLines.push(span('hl-tb', esc(line))); inTb=true; continue; }
    if(inTb || looksTb){
      if(reTbFile.test(line)){ outLines.push(highlightTbFile(line)); continue; }
      if(reExc.test(line)){ outLines.push(highlightExc(line)); continue; }
      if(reTbCaret.test(line)){ outLines.push(span('hl-err', esc(line))); continue; }
      if(!line.trim()){ outLines.push(''); continue; }
      if(/^\s{2,}/.test(line)){ outLines.push(inlineHighlight(line)); continue; }
    }
    if(looksJson){ outLines.push(highlightJsonish(line)); continue; }
    if(rePrompt.test(line)){
      const pm=line.match(/^(>>> |\.\.\. |\$ |# |> )([\s\S]*)$/);
      outLines.push(span('hl-prompt', esc(pm[1]))+inlineHighlight(pm[2]||''));
      continue;
    }
    if(reSection.test(line) && line.trim().length>=3){ outLines.push(span('hl-marker', esc(line))); continue; }
    if(reWarn.test(line)){ outLines.push('<span class="hl hl-warn">'+inlineHighlight(line)+'</span>'); continue; }
    if(reOk.test(line) && line.trim().length<80){ outLines.push('<span class="hl hl-ok">'+inlineHighlight(line)+'</span>'); continue; }
    if(reExc.test(line)){ outLines.push(highlightExc(line)); continue; }
    outLines.push(inlineHighlight(line));
  }
  let html=outLines.join('\n');
  if(rest){
    html+='\n'+span('hl-dim', esc('… 已截断高亮，剩余 '+rest.length+' 行未着色'))+'\n'+esc(rest.join('\n'));
  }
  return html;
}
function fillToolOutputEl(el, raw){
  if(!el) return;
  el.classList.add('tool-output');
  el.innerHTML=highlightToolOutput(raw);
}
applyTheme(currentTheme(), false);

const safeUrl=u=>{
  const s=String(u??'').trim();
  if(!s) return '';
  if(/^(https?:|mailto:|tel:|#|\/|\.\/|\.\.\/)/i.test(s)) return s;
  if(/^data:image\/[a-z0-9.+-]+;base64,/i.test(s)) return s;
  return '';
};
// marked + DOMPurify (inlined). Fallback is escaped plain text.
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
  // 仅多种工具时在名称后显示 ×c，避免「python ×n」与「n 次」重复
  const nameEntries=Object.entries(names);
  const multiKinds=nameEntries.length>1;
  const nameText=nameEntries.slice(0,2).map(([n,c])=>(multiKinds&&c>1)?`${n} ×${c}`:n).join('、')+(nameEntries.length>2?' 等':'');
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
  try{const r=await fetch('/api/tool-output?id='+encodeURIComponent(row.dataset.callId));if(!r.ok)throw new Error();const data=await r.json();out=document.createElement('pre');fillToolOutputEl(out, data.output||'(无输出)');row.appendChild(out);button.textContent='收起输出'}catch(err){button.textContent='加载失败'}finally{button.disabled=false}
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
if(themeToggleBtn) themeToggleBtn.addEventListener('click',()=>toggleTheme());
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
