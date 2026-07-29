import json
import subprocess
from pathlib import Path
from sys import argv, executable

import requests

f = Path(argv[1])

while True:
    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]
    message = requests.post(data["url"], headers=data["headers"], json=body).json()["choices"][0]["message"]
    body["messages"].append(message)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if not message.get("tool_calls"):
        break
    for call in message["tool_calls"]:
        out = subprocess.run([executable, "-u", "-c", json.loads(call["function"]["arguments"])["code"]],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, errors="ignore").stdout
        data = json.loads(f.read_text(encoding="utf-8")); body = data["json"]
        body["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": out})
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")