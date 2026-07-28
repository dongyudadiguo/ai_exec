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
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            exec(call["input"]["code"], _ns)
        results.append({
            "type": "tool_result",
            "tool_use_id": call["id"],
            "content": out.getvalue(),
        })

    data = json.loads(f.read_text(encoding="utf-8"))
    body = data["json"]
    body["messages"].append({
        "role": "user",
        "content": results,
    })
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
