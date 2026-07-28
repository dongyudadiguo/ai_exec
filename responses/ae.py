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
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            exec(json.loads(call["arguments"])["code"], _ns)
        data = json.loads(f.read_text(encoding="utf-8"))
        data["json"]["input"].append({
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": out.getvalue(),
        })
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
