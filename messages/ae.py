import json, queue, subprocess, threading, time
from pathlib import Path
from sys import argv, executable

import requests

_DRIVER = r"""
import sys, io, traceback
from contextlib import redirect_stdout, redirect_stderr

def _recv():
    h = sys.stdin.buffer.readline()
    if not h:
        return None
    n = int(h)
    buf = bytearray()
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return buf.decode("utf-8")

def _send(text):
    data = text.encode("utf-8")
    sys.stdout.buffer.write(f"{len(data)}\n".encode("ascii") + data)
    sys.stdout.buffer.flush()

NS = {"__name__": "__main__"}
while True:
    code = _recv()
    if code is None:
        break
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            exec(compile(code, "<tool>", "exec"), NS)
    except Exception:
        buf.write(traceback.format_exc())
    _send(buf.getvalue())
"""

if len(argv) < 2:
    raise SystemExit("usage: ae.py <request.json>")
f = Path(argv[1])

def _spawn():
    global _PROC
    old = _PROC
    _PROC = subprocess.Popen(
        [executable, "-u", "-c", _DRIVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    if old is None:
        return
    try:
        old.kill()
    except OSError:
        pass
    for pipe in (old.stdin, old.stdout):
        try:
            pipe.close()
        except OSError:
            pass

_PROC = None
_spawn()

def _read_frame(proc):
    h = proc.stdout.readline()
    if not h:
        return ""
    n = int(h)
    data = bytearray()
    while len(data) < n:
        chunk = proc.stdout.read(n - len(data))
        if not chunk:
            break
        data.extend(chunk)
    return data.decode("utf-8", errors="replace")

def tool_run(code):
    payload = code.encode("utf-8")
    frame = f"{len(payload)}\n".encode("ascii") + payload
    try:
        _PROC.stdin.write(frame)
        _PROC.stdin.flush()
    except OSError:
        _spawn()
        _PROC.stdin.write(frame)
        _PROC.stdin.flush()
    proc, done = _PROC, queue.Queue()

    def _read():
        try:
            done.put(_read_frame(proc))
        except Exception as e:
            done.put(f"\n{type(e).__name__}: {e}\n")

    threading.Thread(target=_read, daemon=True).start()
    try:
        out = done.get(timeout=_TOOL_TIMEOUT)
    except queue.Empty:
        proc.kill()
        _spawn()
        out = f"\nTimeoutError: tool execution exceeded {_TOOL_TIMEOUT}s\n"
    if _MAX_OUT and len(out) > _MAX_OUT:
        h = _MAX_OUT // 2
        out = out[:h] + f"\n...[truncated {len(out) - _MAX_OUT} chars]...\n" + out[-h:]
    return out

_cfg = json.loads(f.read_text(encoding="utf-8"))
_MAX_OUT = _cfg.get("max_tool_output", 0)
_TOOL_TIMEOUT = _cfg.get("timeout")
# 空闲时轮询 input.json 的间隔（秒），等新消息追加进来
_IDLE_POLL = float(_cfg.get("idle_poll", 1.0))

while True:
    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]

    response = requests.post(
        data["url"],
        headers=data["headers"],
        json=body,
    ).json()

    body["messages"].append({
        "role": "assistant",
        "content": response["content"],
    })
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    calls = [b for b in response["content"] if b["type"] == "tool_use"]
    if not calls:
        # 一轮对话结束：不退出，保持 driver 子进程（工具内存）存活，
        # 持续等待下一条用户消息被追加到 input.json 后再继续。
        processed = len(body["messages"])
        while True:
            time.sleep(_IDLE_POLL)
            try:
                items = json.loads(f.read_text(encoding="utf-8"))["json"]["messages"]
            except Exception:
                continue
            if len(items) > processed:
                break
        continue

    results = []
    for call in calls:
        out = tool_run(call["input"]["code"])
        results.append({
            "type": "tool_result",
            "tool_use_id": call["id"],
            "content": out or "(no output)",
        })

    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]
    body["messages"].append({
        "role": "user",
        "content": results,
    })
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
