import json
import subprocess
from pathlib import Path
from sys import argv, executable

import requests

f = Path(argv[1])

while True:
    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]

    response = requests.post(
        data["url"],
        headers=data["headers"],
        json=body,
    ).json()

    message = {
        "role": "assistant",
        "content": response["content"],
    }
    body["messages"].append(message)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    calls = [block for block in response["content"] if block["type"] == "tool_use"]
    if not calls:
        break

    for index, call in enumerate(calls):
        stdout = subprocess.run(
            [
                executable,
                "-c",
                call["input"]["code"],
            ],
            text=True,
            errors="ignore",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout

        result = {
            "type": "tool_result",
            "tool_use_id": call["id"],
            "content": stdout,
        }

        data = json.loads(f.read_text(encoding="utf-8"))
        body = data["json"]
        if index == 0:
            body["messages"].append({
                "role": "user",
                "content": [result],
            })
        else:
            body["messages"][-1]["content"].append(result)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
