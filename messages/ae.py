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
        out = subprocess.run(
            [executable, "-u", "-c", call["input"]["code"]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            errors="ignore",
        ).stdout
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