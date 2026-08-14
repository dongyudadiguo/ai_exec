import json, queue, subprocess, threading
from pathlib import Path
from sys import argv, executable

import requests

_DRIVER = r"""
import sys, io, traceback, pickle
import os
from contextlib import redirect_stdout, redirect_stderr
STATE_FILE = sys.argv[1]
_SKIP = {"__name__", "__persist__", "__persisted__"}

def _load():
    try:
        with open(STATE_FILE, "rb") as f:
            data = pickle.load(f)
        if isinstance(data, dict) and data.get("__persist__") is True:
            data.pop("__persist__", None)
            data.pop("__persisted__", None)
            return data
    except (FileNotFoundError, EOFError, pickle.UnpicklingError):
        pass
    return {}

def _declared(ns):
    names = ns.get("__persist__")
    if isinstance(names, str):
        names = [names]
    if isinstance(names, (list, tuple, set, frozenset)):
        return {n for n in names if isinstance(n, str) and n not in _SKIP}
    return set()

def _save(ns):
    names = _declared(ns)
    if not names:
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return
    out = {"__persist__": True}
    for k in names:
        if k not in ns:
            continue
        v = ns[k]
        try:
            pickle.dumps(v)
        except Exception:
            continue
        out[k] = v
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(out, f)
    os.replace(tmp, STATE_FILE)

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

NS = _load()
NS["__name__"] = "__main__"
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
    _save(NS)
    _send(buf.getvalue())
"""

if len(argv) < 3:
    raise SystemExit("usage: ae.py <request.json> <state.pkl>")
f = Path(argv[1])
_STATE_FILE = str(Path(argv[2]).resolve())

def _spawn():
    global _PROC
    old = _PROC
    _PROC = subprocess.Popen(
        [executable, "-u", "-c", _DRIVER, _STATE_FILE],
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

while True:
    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]

    output = requests.post(
        data["url"],
        headers=data["headers"],
        json=body,
    ).json()["output"]

    body["input"].extend(output)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    calls = [item for item in output if item["type"] == "function_call"]
    if not calls:
        break

    for call in calls:
        out = tool_run(json.loads(call["arguments"])["code"])
        data = json.loads(f.read_text(encoding="utf-8"))
        data["json"]["input"].append({
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": out,
        })
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
