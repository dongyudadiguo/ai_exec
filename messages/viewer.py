# -*- coding: utf-8 -*-
"""input.json viewer — Flask + psutil, static assets out of this file."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import psutil
from flask import Flask, abort, jsonify, render_template, request, send_file

ROOT = Path(__file__).resolve().parent
INPUT_FILE = ROOT / "input.json"
AE_FILE = ROOT / "ae.py"
RUNNER_PID_DIR = ROOT / ".ae_runners"
ACTIVE_FILE = ROOT / ".ae_active_input"
HOST = "127.0.0.1"
PORT = int(os.environ.get("AE_VIEWER_PORT", "8765"))
TOOL_PREVIEW = int(os.environ.get("AE_TOOL_PREVIEW", "800"))
CONTEXT_LIMIT = int(os.environ.get("AE_CONTEXT_LIMIT", "128000"))

# chat_id -> {"proc": Popen|None, "pid": int|None, "ctime": float|None, "file": Path}
_runners: dict = {}
_process_lock = threading.Lock()
_send_lock = threading.Lock()
_input_lock = threading.Lock()
_state_cache: dict = {}
_state_cache_lock = threading.Lock()
_blob_cache: dict = {}


def read_input(path=None):
    return json.loads((INPUT_FILE if path is None else Path(path)).read_text(encoding="utf-8"))


def write_input(data, path=None):
    target = INPUT_FILE if path is None else Path(path)
    temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)


def _chat_id_from_filename(name: str):
    if name == "input.json":
        return "default"
    if name.startswith("input_") and name.endswith(".json"):
        return name[len("input_") : -len(".json")]
    return None


def _sanitize_chat_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip()).strip(" .")
    if not name:
        raise ValueError("对话名不能为空")
    if name.lower() in {"default", "input"}:
        raise ValueError("对话名不可用")
    if len(name) > 48:
        name = name[:48].rstrip(" .")
    if not name:
        raise ValueError("对话名不能为空")
    return name


def _filename_for_chat_id(chat_id: str) -> str:
    if chat_id is not None and not isinstance(chat_id, str):
        raise ValueError("无效的对话 ID")
    cid = (chat_id or "default").strip() or "default"
    if cid == "default":
        return "input.json"
    if _sanitize_chat_name(cid) != cid:
        raise ValueError("无效的对话 ID")
    return f"input_{cid}.json"


def _clear_transcript(data: dict) -> dict:
    body = data.setdefault("json", {})
    if isinstance(body, dict):
        if "input" in body:
            body["input"] = []
        if isinstance(body.get("messages"), list):
            body["messages"] = [
                m for m in body["messages"] if isinstance(m, dict) and m.get("role") == "system"
            ]
    return data


def _restore_active_input():
    global INPUT_FILE
    try:
        name = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    cid = _chat_id_from_filename(name) if name else None
    if cid:
        try:
            valid = _filename_for_chat_id(cid)
        except ValueError:
            valid = ""
        cand = ROOT / valid if valid == name else None
        if cand is not None and cand.is_file():
            INPUT_FILE = cand
            return
    INPUT_FILE = ROOT / "input.json"


def current_chat_id() -> str:
    return _chat_id_from_filename(INPUT_FILE.name) or "default"


def resolve_chat(chat_id=None):
    raw = (chat_id or "").strip()
    if not raw:
        return current_chat_id(), INPUT_FILE
    path = ROOT / _filename_for_chat_id(raw)
    if not path.is_file():
        raise FileNotFoundError(f"对话不存在: {path.name}")
    return raw, path


def list_chats():
    found = []
    default = ROOT / "input.json"
    if default.is_file():
        found.append(default)
    for path in sorted(ROOT.glob("input_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if _chat_id_from_filename(path.name):
            found.append(path)
    seen, chats, active = set(), [], current_chat_id()
    for path in found:
        if path.name in seen:
            continue
        seen.add(path.name)
        cid = _chat_id_from_filename(path.name)
        if not cid:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        is_running = running(cid)
        chats.append({
            "id": cid,
            "name": "默认" if cid == "default" else cid,
            "file": path.name,
            "mtime": mtime,
            "active": cid == active,
            "running": is_running,
            "idle": runner_idle(cid) if is_running else False,
        })
    chats.sort(key=lambda c: (0 if c["id"] == "default" else 1, -c["mtime"], c["name"]))
    return chats


def _invalidate_state_cache():
    with _state_cache_lock:
        _state_cache.clear()


def set_active_chat(chat_id: str):
    global INPUT_FILE
    filename = _filename_for_chat_id(chat_id)
    path = ROOT / filename
    if not path.is_file():
        raise FileNotFoundError(f"对话不存在: {filename}")
    with _input_lock, _send_lock:
        INPUT_FILE = path
        try:
            ACTIVE_FILE.write_text(filename + "\n", encoding="utf-8")
        except OSError:
            pass
        _invalidate_state_cache()
    return current_chat_id()


def _auto_chat_name() -> str:
    existing = {cid for p in ROOT.glob("input_*.json") if (cid := _chat_id_from_filename(p.name))}
    base = "新对话"
    if base not in existing:
        return base
    n = 2
    while f"{base}{n}" in existing:
        n += 1
    return f"{base}{n}"


def create_chat(name: str = ""):
    raw = (name or "").strip()
    cid = _sanitize_chat_name(raw) if raw else _auto_chat_name()
    path = ROOT / f"input_{cid}.json"
    if path.exists():
        if raw:
            raise ValueError("同名对话已存在")
        cid = _auto_chat_name()
        path = ROOT / f"input_{cid}.json"
    template_path = ROOT / "input.json"
    if not template_path.is_file():
        template_path = INPUT_FILE if INPUT_FILE.is_file() else None
    if template_path is None or not template_path.is_file():
        raise FileNotFoundError("找不到可用的 input 模板")
    data, last_exc = None, None
    for _ in range(8):
        try:
            loaded = json.loads(template_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("input 模板格式错误")
            data = loaded
            break
        except ValueError:
            raise
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            last_exc = exc
            time.sleep(0.02)
    if data is None:
        raise ValueError(f"读取对话模板失败: {last_exc}")
    data = _clear_transcript(data)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    set_active_chat(cid)
    return {"id": cid, "name": cid, "file": path.name, "active": True}


def rename_chat(chat_id: str, new_name: str):
    global INPUT_FILE
    cid = (chat_id or "").strip()
    if not cid or cid == "default":
        raise ValueError("默认对话不能重命名")
    new_cid = _sanitize_chat_name(new_name)
    src = ROOT / _filename_for_chat_id(cid)
    dst = ROOT / _filename_for_chat_id(new_cid)
    if not src.is_file():
        raise FileNotFoundError(f"对话不存在: {src.name}")
    if dst.exists():
        raise ValueError("同名对话已存在")
    with _input_lock, _send_lock:
        was_active = current_chat_id() == cid
        if running(cid):
            raise ValueError("对话运行中，请先停止再重命名")
        os.replace(src, dst)
        with _process_lock:
            entry = _runners.pop(cid, None)
            if entry is not None:
                entry["file"] = dst
                _runners[new_cid] = entry
            try:
                old_pid, new_pid = _runner_pid_file(cid), _runner_pid_file(new_cid)
                if old_pid.is_file():
                    os.replace(old_pid, new_pid)
            except OSError:
                pass
        if was_active:
            INPUT_FILE = dst
            try:
                ACTIVE_FILE.write_text(dst.name + "\n", encoding="utf-8")
            except OSError:
                pass
            _invalidate_state_cache()
    return {"id": new_cid, "name": new_cid, "file": dst.name, "active": current_chat_id() == new_cid}


def delete_chat(chat_id: str):
    global INPUT_FILE
    cid = (chat_id or "").strip()
    if not cid or cid == "default":
        raise ValueError("默认对话不能删除")
    path = ROOT / _filename_for_chat_id(cid)
    if not path.is_file():
        raise FileNotFoundError(f"对话不存在: {path.name}")
    with _input_lock, _send_lock:
        was_active = current_chat_id() == cid
        stop_process(cid)
        try:
            path.unlink()
        except OSError as exc:
            raise ValueError(f"删除失败: {exc}") from exc
        if was_active:
            INPUT_FILE = ROOT / "input.json"
            try:
                ACTIVE_FILE.write_text("input.json\n", encoding="utf-8")
            except OSError:
                pass
            _invalidate_state_cache()
    return {"deleted": cid, "active": current_chat_id()}


# ---------- runner / pid (psutil) ----------

def _runner_pid_file(chat_id=None):
    cid = (chat_id or current_chat_id() or "default").strip() or "default"
    try:
        filename = _filename_for_chat_id(cid)
    except ValueError:
        filename = "input.json"
    return RUNNER_PID_DIR / f"{filename}.pid"


def _ensure_runner_dir():
    try:
        RUNNER_PID_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _clear_runner_pid_file(chat_id):
    try:
        _runner_pid_file(chat_id).unlink()
    except OSError:
        pass


def _write_runner_pid(chat_id, pid):
    _ensure_runner_dir()
    try:
        ctime = psutil.Process(pid).create_time()
    except (psutil.Error, ValueError):
        ctime = None
    try:
        _runner_pid_file(chat_id).write_text(json.dumps({"pid": int(pid), "ctime": ctime}), encoding="utf-8")
    except OSError:
        pass


def _read_runner_pid(chat_id):
    path = _runner_pid_file(chat_id)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        info = None
    if isinstance(info, dict):
        try:
            return int(info.get("pid")), info.get("ctime"), mtime
        except (TypeError, ValueError):
            return None
    try:
        return int(raw), None, mtime
    except ValueError:
        return None


def _pid_matches(pid, ctime, pidfile_mtime=None):
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        started = proc.create_time()
    except (psutil.Error, ValueError):
        return False
    if ctime is not None:
        return abs(started - ctime) <= 2.0
    if pidfile_mtime is not None:
        return abs(started - pidfile_mtime) <= 120.0
    return True


def _runner_pid_unlocked(chat_id=None):
    cid = (chat_id or current_chat_id() or "default").strip() or "default"
    entry = _runners.get(cid)
    if entry is not None:
        proc = entry.get("proc")
        if proc is not None:
            if proc.poll() is None:
                return proc.pid
            _runners.pop(cid, None)
            _clear_runner_pid_file(cid)
            return None
        pid = entry.get("pid")
        if pid and _pid_matches(pid, entry.get("ctime")):
            return pid
        _runners.pop(cid, None)
        _clear_runner_pid_file(cid)
        return None
    info = _read_runner_pid(cid)
    if info is None:
        return None
    pid, ctime, mtime = info
    if _pid_matches(pid, ctime, mtime):
        try:
            target = ROOT / _filename_for_chat_id(cid)
        except ValueError:
            target = None
        _runners[cid] = {
            "proc": None,
            "pid": pid,
            "ctime": ctime if ctime is not None else (
                psutil.Process(pid).create_time() if psutil.pid_exists(pid) else None
            ),
            "file": target,
        }
        return pid
    _clear_runner_pid_file(cid)
    return None


def running(chat_id=None):
    with _process_lock:
        return _runner_pid_unlocked(chat_id) is not None


def runner_idle(chat_id=None):
    cid, target = resolve_chat(chat_id)
    if not running(cid):
        return False
    try:
        _, _, messages = load_cached(target)
    except Exception:
        return False
    if not messages:
        return False
    last = messages[-1]
    return last.get("role") == "assistant" and not last.get("tool_calls")


def pending_tool_progress(messages):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        call_ids = [c.get("id") for c in (message.get("tool_calls") or []) if c.get("id")]
        if not call_ids:
            return 0, 0
        done = set()
        for item in messages[index + 1 :]:
            if item.get("role") != "tool":
                return None
            cid = item.get("tool_call_id")
            if cid in call_ids:
                done.add(cid)
        return len(done), len(call_ids)
    return None


def runner_phase(messages, is_running):
    if not is_running:
        return {"phase": "idle", "label": "空闲", "tool_done": None, "tool_total": None}
    progress = pending_tool_progress(messages)
    if progress is not None:
        done, total = progress
        if total == 0 or done < total:
            return {
                "phase": "waiting_tool",
                "label": "等待工具" if total == 0 else f"等待工具 {done}/{total}",
                "tool_done": done,
                "tool_total": total,
            }
        return {"phase": "waiting_ai", "label": "等待 AI", "tool_done": done, "tool_total": total}
    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
        return {"phase": "idle_wait", "label": "等待消息", "tool_done": None, "tool_total": None}
    return {"phase": "waiting_ai", "label": "等待 AI", "tool_done": None, "tool_total": None}


def agent_python():
    exe = Path(sys.executable)
    if os.name == "nt":
        for candidate in (exe.with_name("pythonw.exe"), exe.parent / "pythonw.exe"):
            if candidate.exists():
                return str(candidate)
    return str(exe)


def noconsole_site_dir():
    return ROOT / "noconsole_site"


def prepend_pythonpath(env, path):
    path = str(path)
    current = env.get("PYTHONPATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    if path not in parts:
        env["PYTHONPATH"] = path + (os.pathsep + current if current else "")
    return env


def start_process(chat_id=None, input_file=None):
    cid = (chat_id or current_chat_id() or "default").strip() or "default"
    target = Path(input_file) if input_file is not None else (ROOT / _filename_for_chat_id(cid))
    with _process_lock:
        if _runner_pid_unlocked(cid) is not None or not target.is_file():
            return False
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
        env = os.environ.copy()
        env["AE_RUNNER"] = "1"
        env["AE_INPUT_FILE"] = str(target)
        env["AE_CONVERSATION_ID"] = cid
        prepend_pythonpath(env, noconsole_site_dir())
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        proc = subprocess.Popen(
            [agent_python(), str(AE_FILE), str(target)],
            cwd=str(ROOT),
            creationflags=creationflags,
            env=env,
            startupinfo=startupinfo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        _write_runner_pid(cid, proc.pid)
        try:
            ctime = psutil.Process(proc.pid).create_time()
        except (psutil.Error, ValueError):
            ctime = None
        _runners[cid] = {"proc": proc, "pid": proc.pid, "ctime": ctime, "file": target}
        return True


def _kill_tree(pid):
    try:
        parent = psutil.Process(pid)
    except (psutil.Error, ValueError):
        return False
    kids = parent.children(recursive=True)
    for child in kids:
        try:
            child.terminate()
        except psutil.Error:
            pass
    try:
        parent.terminate()
    except psutil.Error:
        pass
    gone, alive = psutil.wait_procs(kids + [parent], timeout=1.5)
    for leftover in alive:
        try:
            leftover.kill()
        except psutil.Error:
            pass
    return True


def stop_process(chat_id=None):
    cid = (chat_id or current_chat_id() or "default").strip() or "default"
    with _process_lock:
        pid = _runner_pid_unlocked(cid)
        if pid is None:
            _runners.pop(cid, None)
            _clear_runner_pid_file(cid)
            return False
        stopped = _kill_tree(pid)
        if not psutil.pid_exists(pid):
            stopped = True
        if stopped:
            _runners.pop(cid, None)
            _clear_runner_pid_file(cid)
        return stopped


def stop_all_processes():
    ids = set()
    with _process_lock:
        ids.update(_runners.keys())
    try:
        if (ROOT / "input.json").is_file():
            ids.add("default")
        for path in ROOT.glob("input_*.json"):
            cid = _chat_id_from_filename(path.name)
            if cid:
                ids.add(cid)
    except OSError:
        pass
    return any(stop_process(cid) for cid in sorted(ids))


def shutdown_viewer():
    stop_all_processes()

    def _close():
        time.sleep(0.05)
        os._exit(0)

    threading.Thread(target=_close, name="viewer-shutdown", daemon=True).start()
    return True


# ---------- transcript / display ----------

def simple_token_count(value):
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]", text))
    return cjk + latin


def usage_from_messages(messages):
    total, by_role = 0, {}
    for message in messages:
        count = simple_token_count(message)
        total += count
        role = message.get("role", "unknown")
        by_role[role] = by_role.get(role, 0) + count
    return {"estimated_total": total, "by_role": by_role}


ANTHROPIC_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def file_part_from_storage(storage):
    filename = storage.filename or "attachment"
    raw = storage.read() or b""
    mime = storage.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    encoded = base64.b64encode(raw).decode("ascii")
    if mime in ANTHROPIC_IMAGE_TYPES:
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}}
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime, "data": encoded}}
    try:
        text = raw.decode("utf-8")
        return {"type": "text", "text": f"附件 {filename}:\n{text}"}
    except UnicodeDecodeError:
        return {"type": "text", "text": f"附件 {filename} ({mime}, base64):\n{encoded}"}


def _is_human_user_message(message):
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if not isinstance(content, list) or not content:
        return False
    return all(not isinstance(block, dict) or block.get("type") != "tool_result" for block in content)


def _user_message_text(message):
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def pop_last_user_message(path=None):
    data = read_input(path)
    messages = data.setdefault("json", {}).setdefault("messages", [])
    if not messages:
        raise ValueError("no messages to edit")
    last = messages[-1]
    if not _is_human_user_message(last):
        raise ValueError("last message is not a human user message")
    text = _user_message_text(last)
    messages.pop()
    write_input(data, path)
    return text


def append_user_message(text, files, path=None):
    data = read_input(path)
    messages = data.setdefault("json", {}).setdefault("messages", [])
    blocks = []
    if text.strip():
        blocks.append({"type": "text", "text": text.strip()})
    blocks.extend(files)
    if not blocks:
        raise ValueError("message is empty")
    messages.append({"role": "user", "content": blocks})
    write_input(data, path)


def repair_unclosed_tool_calls(path=None):
    target = INPUT_FILE if path is None else Path(path)
    try:
        data = read_input(target)
    except (OSError, json.JSONDecodeError):
        return 0
    messages = data.setdefault("json", {}).setdefault("messages", [])
    answered = {
        b.get("tool_use_id")
        for m in messages
        for b in (m.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    calls = [
        b for m in messages
        for b in (m.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in answered
    ]
    if not calls:
        return 0
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": c["id"],
                "content": "（工具调用被中断，未返回结果，继续运行时自动补全）",
            }
            for c in calls
        ],
    })
    write_input(data, target)
    return len(calls)


def parse_send_form():
    text = request.form.get("message", "") or ""
    files = [file_part_from_storage(f) for f in request.files.getlist("files") if f and f.filename]
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


def _anthropic_block_for_display(block):
    if not isinstance(block, dict):
        return None
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": str(block.get("text", ""))}
    if kind == "image":
        source = block.get("source") or {}
        source_type = source.get("type")
        if source_type == "base64":
            url = f"data:{source.get('media_type', 'application/octet-stream')};base64,{source.get('data', '')}"
        elif source_type == "url":
            url = source.get("url", "")
        else:
            url = ""
        return {"type": "image_url", "image_url": {"url": url}}
    if kind == "document":
        return {"type": "text", "text": "[文档附件]"}
    return None


def _anthropic_content_for_display(content):
    if isinstance(content, str) or content is None:
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        display = _anthropic_block_for_display(block)
        if display is not None:
            parts.append(display)
    return _collapse_display_parts(parts)


def _anthropic_tool_result_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    pieces = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            pieces.append(str(block.get("text", "")))
        elif kind == "image":
            pieces.append("[图片工具结果]")
        elif kind == "document":
            pieces.append("[文档工具结果]")
    return "\n".join(piece for piece in pieces if piece)


def anthropic_transcript(body):
    result = []
    system = body.get("system")
    if system:
        result.append({"role": "system", "content": _anthropic_content_for_display(system)})

    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content", "")

        if role == "assistant":
            blocks = content if isinstance(content, list) else []
            calls = []
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                calls.append({
                    "id": block.get("id"),
                    "function": {
                        "name": block.get("name", "python"),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            item = {"role": "assistant", "content": _anthropic_content_for_display(content)}
            if calls:
                item["tool_calls"] = calls
            result.append(item)
            continue

        if role != "user":
            continue
        if isinstance(content, str):
            result.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            continue

        human_parts = []

        def flush_human_parts():
            nonlocal human_parts
            if human_parts:
                result.append({"role": "user", "content": _collapse_display_parts(human_parts)})
                human_parts = []

        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                flush_human_parts()
                result.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _anthropic_tool_result_text(block.get("content", "")),
                    "tool_failed": bool(block.get("is_error", False)),
                })
            else:
                display = _anthropic_block_for_display(block)
                if display is not None:
                    human_parts.append(display)
        flush_human_parts()

    return result


def display_content(content):
    if isinstance(content, list):
        out = []
        for part in content:
            if part.get("type") == "image_url":
                p, img = dict(part), dict((part.get("image_url") or {}))
                url = img.get("url", "")
                if url.startswith("data:image/"):
                    img["url"] = _blob_url(url)
                p["image_url"] = img
                out.append(p)
            else:
                out.append(part)
        return out
    if isinstance(content, str) and "data:image/" in content:
        return re.sub(
            r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+",
            lambda m: _blob_url(m.group(0).replace("\n", "").replace("\r", "")),
            content,
        )
    return content


_TOOL_FAIL_RE = re.compile(
    r"(?m)^\[tool_error\]|^Traceback \(most recent call last\):|"
    r"^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Error(?:Group)?:|"
    r"^(?:[A-Za-z_][\w]*\.)*[A-Za-z_]*Exception:"
)


def display_message(m):
    d = {"role": m.get("role", "message")}
    if "content" in m:
        d["content"] = display_content(m.get("content"))
    if m.get("role") == "tool":
        c = str(m.get("content", ""))
        d["content"] = c[:TOOL_PREVIEW] + (f"\n\n…… 已截断，完整长度 {len(c)} 字符" if len(c) > TOOL_PREVIEW else "")
        d["tool_content_length"] = len(c)
        d["tool_call_id"] = m.get("tool_call_id")
        d["tool_failed"] = bool(m.get("tool_failed") or _TOOL_FAIL_RE.search(c))
    if m.get("tool_calls"):
        d["tool_calls"] = [
            {"id": c.get("id"), "function": {"name": (c.get("function") or {}).get("name", "python")}}
            for c in m.get("tool_calls", [])
        ]
    return d


def load_cached(path=None):
    input_file = Path(path) if path is not None else INPUT_FILE
    st = input_file.stat()
    signature = (st.st_mtime_ns, st.st_size)
    with _state_cache_lock:
        entry = _state_cache.get(input_file)
        if entry is not None and entry["signature"] == signature:
            return entry["mtime"], entry["model"], entry["messages"]
    try:
        data = read_input(input_file)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError):
        with _state_cache_lock:
            entry = _state_cache.get(input_file)
        if entry is not None:
            return entry["mtime"], entry["model"], entry["messages"]
        raise
    body = data.get("json", {})
    items = body.get("messages", [])
    model = body.get("model", "")
    messages = None
    with _state_cache_lock:
        prev = _state_cache.get(input_file)
    if prev is not None and prev.get("items") is not None and prev.get("messages") is not None:
        prev_items, prev_msgs = prev["items"], prev["messages"]
        if len(items) >= len(prev_items) and items[: len(prev_items)] == prev_items:
            delta = items[len(prev_items) :]
            messages = prev_msgs if not delta else list(prev_msgs) + anthropic_transcript({"messages": delta})
    if messages is None:
        messages = anthropic_transcript(body)
    with _state_cache_lock:
        _state_cache[input_file] = {
            "signature": signature,
            "mtime": st.st_mtime,
            "messages": messages,
            "model": model,
            "usage": None,
            "items": items,
        }
        while len(_state_cache) > 12:
            _state_cache.pop(next(iter(_state_cache)), None)
    return st.st_mtime, model, messages


def state_payload(light_if_unchanged=False, since=None, after=None, chat_id=None):
    cid, target = resolve_chat(chat_id)
    mtime, model, messages = load_cached(target)
    is_running = running(cid)
    phase = runner_phase(messages, is_running)
    if light_if_unchanged and since is not None and mtime <= since:
        return {
            "unchanged": True,
            "running": is_running,
            "updated": mtime,
            "count": len(messages),
            "chat": cid,
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
        "chat": cid,
        **phase,
    }


def usage_payload(chat_id=None):
    _, target = resolve_chat(chat_id)
    mtime, _model, messages = load_cached(target)
    with _state_cache_lock:
        entry = _state_cache.get(target)
        usage = entry.get("usage") if entry else None
    if usage is None:
        usage = usage_from_messages(messages)
        with _state_cache_lock:
            entry = _state_cache.get(target)
            if entry is not None:
                entry["usage"] = usage
    return {"updated": mtime, "usage": usage, "context_limit": CONTEXT_LIMIT}


def tool_output_payload(call_id, chat_id=None):
    _, target = resolve_chat(chat_id)
    _, _, messages = load_cached(target)
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id:
            return {"call_id": call_id, "output": str(message.get("content", ""))}
    return None


# ---------- Flask app ----------

app = Flask(__name__, static_folder="static", template_folder="templates", root_path=str(ROOT))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024


@app.get("/")
@app.get("/index.html")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    try:
        since = float(request.args["since"]) if request.args.get("since") else None
    except ValueError:
        since = None
    try:
        after = int(request.args["after"]) if request.args.get("after") else None
    except ValueError:
        after = None
    try:
        return jsonify(state_payload(True, since, after, request.args.get("chat", "")))
    except (ValueError, OSError) as exc:
        return jsonify({"missing": True, "error": str(exc)}), 404


@app.get("/api/usage")
def api_usage():
    try:
        return jsonify(usage_payload(request.args.get("chat", "")))
    except (ValueError, OSError) as exc:
        return jsonify({"missing": True, "error": str(exc)}), 404


@app.get("/api/tool-output")
def api_tool_output():
    try:
        payload = tool_output_payload(request.args.get("id", ""), request.args.get("chat", ""))
    except (ValueError, OSError):
        payload = None
    return jsonify(payload) if payload is not None else abort(404)


@app.get("/api/chats")
def api_chats_get():
    return jsonify({"chats": list_chats(), "active": current_chat_id()})


@app.get("/api/blob")
def api_blob():
    data_url = _blob_cache.get(request.args.get("id", ""))
    if not data_url:
        abort(404)
    m = re.match(r"data:([^;]+);base64,(.*)", data_url, re.S)
    if not m:
        abort(400)
    raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
    from io import BytesIO

    return send_file(BytesIO(raw), mimetype=m.group(1), max_age=3600)


@app.post("/api/send")
def api_send():
    text, files = parse_send_form()
    chat_arg = request.args.get("chat", "")
    with _send_lock:
        try:
            cid, target = resolve_chat(chat_arg)
        except ValueError as exc:
            return str(exc), 400
        except FileNotFoundError as exc:
            return str(exc), 404
        if running(cid):
            if not runner_idle(cid):
                return "process is already running", 409
            if not (text.strip() or files):
                return "message is empty", 400
            append_user_message(text, files, target)
            return jsonify({"ok": True, "message_appended": True, "chat": cid, "resumed": True})
        repair_unclosed_tool_calls(target)
        appended = bool(text.strip() or files)
        if appended:
            append_user_message(text, files, target)
        if not start_process(cid, target):
            return "process could not be started", 409
    return jsonify({"ok": True, "message_appended": appended, "chat": cid})


@app.post("/api/stop")
def api_stop():
    with _send_lock:
        try:
            cid, _ = resolve_chat(request.args.get("chat", ""))
        except ValueError as exc:
            return str(exc), 400
        except FileNotFoundError as exc:
            return str(exc), 404
        stopped = stop_process(cid)
    return jsonify({"stopped": stopped, "chat": cid})


@app.post("/api/retract-last-user")
def api_retract():
    with _send_lock:
        try:
            cid, target = resolve_chat(request.args.get("chat", ""))
        except ValueError as exc:
            return str(exc), 400
        except FileNotFoundError as exc:
            return str(exc), 404
        if running(cid):
            stop_process(cid)
        try:
            text = pop_last_user_message(target)
        except ValueError as exc:
            return str(exc), 409
    return jsonify({"ok": True, "text": text, "chat": cid})


def _json_body():
    return request.get_json(silent=True) or {}


@app.post("/api/chats")
def api_chats_create():
    try:
        chat = create_chat(_json_body().get("name", ""))
    except ValueError as exc:
        return str(exc), 409
    except FileNotFoundError as exc:
        return str(exc), 404
    return jsonify({"ok": True, "chat": chat, "active": current_chat_id(), "chats": list_chats()})


@app.post("/api/chats/rename")
def api_chats_rename():
    body = _json_body()
    try:
        chat = rename_chat(body.get("id", ""), body.get("name", ""))
    except ValueError as exc:
        return str(exc), 409
    except FileNotFoundError as exc:
        return str(exc), 404
    return jsonify({"ok": True, "chat": chat, "active": current_chat_id(), "chats": list_chats()})


@app.post("/api/chats/delete")
def api_chats_delete():
    try:
        result = delete_chat(_json_body().get("id", ""))
    except ValueError as exc:
        return str(exc), 409
    except FileNotFoundError as exc:
        return str(exc), 404
    return jsonify({"ok": True, **result, "chats": list_chats()})


@app.post("/api/chats/select")
def api_chats_select():
    try:
        active = set_active_chat(_json_body().get("id", "default"))
    except ValueError as exc:
        return str(exc), 400
    except FileNotFoundError as exc:
        return str(exc), 404
    return jsonify({"ok": True, "active": active, "chats": list_chats()})


@app.post("/api/shutdown")
def api_shutdown():
    with _send_lock:
        shutdown_viewer()
    return jsonify({"ok": True, "shutdown": True})


def _port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((HOST, port)) != 0


def _pick_port():
    port = PORT
    while not _port_is_free(port):
        port += 1
    return port


if __name__ == "__main__":
    os.chdir(ROOT)
    _restore_active_input()
    port = _pick_port()
    url = f"http://{HOST}:{port}"
    if sys.stdout:
        print(f"input.json viewer: {url}")
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        app.run(host=HOST, port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        stop_all_processes()
