import json, queue, subprocess, secrets, threading
from pathlib import Path
from sys import argv, executable

import requests

_DRIVER = """
import sys, io, traceback, pickle
from contextlib import redirect_stdout, redirect_stderr
SENTINEL = sys.argv[1]
STATE_FILE = sys.argv[2]

def _load():
    try:
        with open(STATE_FILE, "rb") as f:
            return pickle.load(f)
    except (FileNotFoundError, EOFError, pickle.UnpicklingError):
        return {}

def _save(ns):
    out = {}
    for k, v in ns.items():
        if k.startswith("__"): continue
        try:
            pickle.dumps(v)
            out[k] = v
        except Exception:
            pass
    with open(STATE_FILE, "wb") as f:
        pickle.dump(out, f)

NS = _load()
NS["__name__"] = "__main__"
while True:
    h = sys.stdin.readline()
    if not h: break
    code = sys.stdin.read(int(h))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            exec(compile(code, "<tool>", "exec"), NS)
    except Exception:
        buf.write(traceback.format_exc())
    _save(NS)
    sys.stdout.write(buf.getvalue() + SENTINEL + "\\n")
    sys.stdout.flush()
"""

_STATE_FILE = str(Path(__file__).resolve().parent / "_state.pkl")

def _spawn():
    global _PROC, _SENTINEL
    _SENTINEL = secrets.token_hex(16)
    _PROC = subprocess.Popen(
        [executable, "-u", "-c", _DRIVER, _SENTINEL, _STATE_FILE],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, errors="ignore", bufsize=1,
    )

_PROC = _SENTINEL = None
_spawn()

def tool_run(code):
    try:
        _PROC.stdin.write(f"{len(code)}\n{code}")
        _PROC.stdin.flush()
    except OSError:
        _spawn()
        _PROC.stdin.write(f"{len(code)}\n{code}")
        _PROC.stdin.flush()
    lines = []
    proc, sentinel, done = _PROC, _SENTINEL, queue.Queue()

    def _read():
        for line in proc.stdout:
            if line.rstrip("\n") == sentinel:
                break
            lines.append(line)
        done.put(True)

    threading.Thread(target=_read, daemon=True).start()
    try:
        done.get(timeout=_TOOL_TIMEOUT)
        out = "".join(lines)
    except queue.Empty:
        proc.kill()
        _spawn()
        out = "".join(lines) + f"\nTimeoutError: tool execution exceeded {_TOOL_TIMEOUT}s\n"
    if _MAX_OUT and len(out) > _MAX_OUT:
        h = _MAX_OUT // 2
        out = out[:h] + f"\n...[truncated {len(out) - _MAX_OUT} chars]...\n" + out[-h:]
    return out

f = Path(argv[1])
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
