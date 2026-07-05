import json
import subprocess
from pathlib import Path
from sys import argv, executable

import requests


TOOL = {"type": "function", "function": {"name": "python", "description": "Execute Python code and return stdout/stderr.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}}


def run_python(code):
    return subprocess.run([executable, "-X", "utf8", "-c", code], text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout[:20000]


def chat(path):
    while True:
        data = json.loads(path.read_text(encoding="utf-8"))
        body = data["json"]
        body["tools"] = [TOOL]
        body["parallel_tool_calls"] = False
        message = requests.post(data["url"], headers=data["headers"], json=body).json()["choices"][0]["message"]
        body["messages"].append(message)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        if not message.get("tool_calls"):
            return
        for call in message["tool_calls"]:
            body["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": run_python(json.loads(call["function"]["arguments"])["code"])})
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    chat(Path(argv[1]))
