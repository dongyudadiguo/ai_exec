import json
import subprocess
from sys import argv, executable

import requests


while True:
    data = json.load(open(argv[1], encoding="utf-8"))
    body = data["json"]
    message = requests.post(data["url"], headers=data["headers"], json=body).json()["choices"][0]["message"]
    body["messages"].append(message)
    open(argv[1], "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
    if not message.get("tool_calls"):
        break
    for call in message["tool_calls"]:
        body["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": subprocess.run([executable, "-c", json.loads(call["function"]["arguments"])["code"]], text=True, errors="ignore", stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout})
        open(argv[1], "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False, indent=2))
