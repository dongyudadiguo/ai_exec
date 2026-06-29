import json
import os
import subprocess
import sys
from pathlib import Path


MODEL = os.environ["OPENAI_MODEL"]

TOOLS = [{
    "type": "function",
    "function": {
        "name": "python",
        "description": "Execute Python code and return stdout/stderr.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
}]


def append(path, text):
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def run_python(code):
    p = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        capture_output=True,
    )
    out = p.stdout + p.stderr
    return out + (f"\n[exit {p.returncode}]" if p.returncode else "")


def merge_call(calls, delta):
    while len(calls) <= delta.index:
        calls.append({})
    call = calls[delta.index]
    for key in ("id", "type"):
        value = getattr(delta, key)
        if value is not None:
            call[key] = value
    if delta.function is not None:
        fn = call.setdefault("function", {})
        for key in ("name", "arguments"):
            value = getattr(delta.function, key)
            if value is not None:
                fn[key] = fn.get(key, "") + value


def tool_result(path, call):
    fn = call["function"]
    args = json.loads(fn["arguments"])
    out = run_python(args["code"])
    append(path, f"\n\n## Tool: {fn['name']}\n\n```python\n{args['code']}\n```\n\n```text\n{out[:20000]}\n```\n")
    print(f"\n[tool:{fn['name']}]\n{out}\n", flush=True)
    return out[:20000]


def chat(client, path, md):
    messages = [{"role": "user", "content": md}]
    while True:
        content, calls, opened = "", [], False
        for chunk in client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, stream=True):
            delta = chunk.choices[0].delta
            if delta.content:
                if not opened:
                    append(path, "\n\n## Assistant\n\n")
                    opened = True
                print(delta.content, end="", flush=True)
                append(path, delta.content)
                content += delta.content
            for d in delta.tool_calls or []:
                merge_call(calls, d)
        print(flush=True)
        messages.append({"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})})
        if not calls:
            return
        for call in calls:
            result = tool_result(path, call)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})


def main():
    path = Path(sys.argv[1])
    from openai import OpenAI
    chat(OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"]), path, path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
