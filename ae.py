import json
import subprocess
from pathlib import Path
from sys import argv, executable

import requests

f = Path(argv[1])

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
        stdout = subprocess.run(
            [
                executable,
                "-c",
                json.loads(call["arguments"])["code"],
            ],
            text=True,
            errors="ignore",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout

        data = json.loads(f.read_text(encoding="utf-8"))
        data["json"]["input"].append({
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": stdout,
        })
        f.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )