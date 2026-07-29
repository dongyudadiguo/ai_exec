import json, subprocess, secrets
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
        _PROC.stdin.write(f"{len(code.encode())}\n{code}")
        _PROC.stdin.flush()
    except OSError:
        _spawn()
        _PROC.stdin.write(f"{len(code.encode())}\n{code}")
        _PROC.stdin.flush()
    lines = []
    for line in _PROC.stdout:
        if line.rstrip("\n") == _SENTINEL:
            break
        lines.append(line)
    return "".join(lines)

f = Path(argv[1])

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
        break

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
