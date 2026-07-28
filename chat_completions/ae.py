import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from sys import argv

import requests

f = Path(argv[1])
_ns = {}

while True:
    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]
    message = requests.post(data["url"], headers=data["headers"], json=body).json()["choices"][0]["message"]
    body["messages"].append(message)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if not message.get("tool_calls"):
        break
    for call in message["tool_calls"]:
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            exec(json.loads(call["function"]["arguments"])["code"], _ns)
        data = json.loads(f.read_text(encoding="utf-8")); body = data["json"]
        body["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": out.getvalue()})
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
