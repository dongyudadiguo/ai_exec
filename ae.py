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

    body["input"].extend(response["output"])
    calls = [item for item in response["output"] if item["type"] == "function_call"]

    for call in calls:
        code = json.loads(call["arguments"])["code"]
        output = subprocess.run(
            [executable, "-c", code],
            text=True,
            errors="ignore",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout

        body["input"].append({
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": output,
        })

    f.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not calls:
        break