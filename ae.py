import json
import re
import subprocess
from os import environ
from pathlib import Path
from sys import argv, executable

from openai import OpenAI


MODEL = environ["OPENAI_MODEL"]
TOOL = {"type": "function", "function": {"name": "python", "description": "Execute Python code and return stdout/stderr.", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}}

MESSAGE_RE = re.compile(r"(?ms)^##\s+((?:system|user|assistant)|tool\s+\S+)\s+(-+)[^\S\n]*\n(.*?)\n^\2[^\S\n]*$")
ASSISTANT_TOOL_RE = re.compile(r"(?ms)^###\s+tool\s+(\S+)\s+(\S+)\s*\n+(`{3,})[^\n]*\n(.*?)\n\3")
FENCE_RE = re.compile(r"(?ms)^(`{3,})[^\n]*\n(.*?)\n\1$")


def fence(lang, text):
    text = text or ""
    ticks = "`" * max(3, max(map(len, re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def block(heading, body):
    end = "-" * max(3, max(map(len, re.findall(r"(?m)^(-+)[^\S\n]*$", body)), default=0) + 1)
    return f"## {heading} {end}\n\n{body}\n{end}"


def unfence(text):
    match = FENCE_RE.fullmatch(text.strip("\n"))
    return match.group(2) if match else text.strip()


def tool_call(name, call_id, code):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({"code": code})},
    }


def tool_code(call):
    return json.loads(call["function"]["arguments"])["code"]


def assistant_parts(body):
    matches = list(ASSISTANT_TOOL_RE.finditer(body))
    if not matches:
        return body.strip(), []
    return ASSISTANT_TOOL_RE.sub("", body).strip() or None, [tool_call(match.group(1), match.group(2), match.group(4)) for match in matches]


def markdown_messages(text):
    messages = []
    pos = 0
    for match in MESSAGE_RE.finditer(text):
        if text[pos:match.start()].strip():
            messages.append({"role": "user", "content": text[pos:match.start()].strip()})
        heading, _, body = match.groups()
        parts = heading.split()
        role = parts[0].lower()
        if role == "tool":
            messages.append({"role": role, "tool_call_id": parts[1], "content": unfence(body)})
        elif role == "assistant":
            content, tool_calls = assistant_parts(body)
            item = {"role": role, "content": content}
            if tool_calls:
                item["tool_calls"] = tool_calls
            messages.append(item)
        else:
            messages.append({"role": role, "content": body.strip()})
        pos = match.end()
    if text[pos:].strip():
        messages.append({"role": "user", "content": text[pos:].strip()})
    return messages


def markdown_message(message):
    role = message["role"]
    if role == "tool":
        return block(f"tool {message['tool_call_id']}", message["content"] or "")

    body = message.get("content") or ""
    if role == "assistant" and message.get("tool_calls"):
        body = "\n\n".join(
            [body]
            + [
                f"### tool {call['function']['name']} {call['id']}\n\n{fence('', tool_code(call))}"
                for call in message["tool_calls"]
            ]
        ).strip()
    return block(role, body)


def append(path, message):
    with path.open("a", encoding="utf-8") as file:
        if path.stat().st_size:
            file.write("\n\n")
        file.write(markdown_message(message))
    return markdown_messages(path.read_text(encoding="utf-8"))


def run_python(code):
    return subprocess.run([executable, "-X", "utf8", "-c", code], text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout[:20000]


def chat(client, path):
    messages = markdown_messages(path.read_text(encoding="utf-8"))
    path.write_text("\n\n".join(map(markdown_message, messages)), encoding="utf-8")
    while True:
        message = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=[TOOL],
            parallel_tool_calls=False,
        ).choices[0].message
        messages = append(path, message.model_dump(exclude_none=True))
        if not message.tool_calls:
            return

        for call in message.tool_calls:
            messages = append(path, {"role": "tool", "tool_call_id": call.id, "content": run_python(json.loads(call.function.arguments)["code"])})


def main():
    chat(OpenAI(api_key=environ["OPENAI_API_KEY"], base_url=environ["OPENAI_BASE_URL"]), Path(argv[1]))


if __name__ == "__main__":
    main()
