import re
import subprocess
from os import environ
from pathlib import Path
from sys import argv, executable

from openai import OpenAI, pydantic_function_tool
from pydantic import BaseModel


MODEL = environ["OPENAI_MODEL"]


class PythonArgs(BaseModel):
    code: str


TOOL = pydantic_function_tool(
    PythonArgs,
    name="python",
    description="Execute Python code and return stdout/stderr.",
)

MESSAGE_RE = re.compile(r"(?ms)^##\s+((?:system|user|assistant)|tool\s+\S+)\s*\n(.*?)(?=^##\s+(?:(?:system|user|assistant)|tool\s+\S+)\s*\n|\Z)")
ASSISTANT_TOOL_RE = re.compile(r"(?ms)^###\s+tool\s+(\S+)\s+(\S+)\s*\n+```[^\n]*\n(.*?)\n```")
FENCE_RE = re.compile(r"(?ms)^```[^\n]*\n(.*?)\n```$")


def fence(lang, text):
    return f"```{lang}\n{text or ''}\n```"


def unfence(text):
    match = FENCE_RE.fullmatch(text.strip("\n"))
    return match.group(1) if match else text.strip()


def tool_call(name, call_id, code):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": PythonArgs(code=code).model_dump_json()},
    }


def tool_code(call):
    return PythonArgs.model_validate_json(call["function"]["arguments"]).code


def assistant_parts(body):
    matches = list(ASSISTANT_TOOL_RE.finditer(body))
    if not matches:
        return body.strip(), []
    return ASSISTANT_TOOL_RE.sub("", body).strip() or None, [
        tool_call(match.group(1), match.group(2), match.group(3))
        for match in matches
    ]


def markdown_messages(text):
    messages = []
    for heading, body in MESSAGE_RE.findall(text):
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
    return messages


def markdown_message(message):
    role = message["role"]
    if role == "tool":
        return f"## tool {message['tool_call_id']}\n\n{fence('text', message['content'])}"

    body = message.get("content") or ""
    if role == "assistant" and message.get("tool_calls"):
        body = "\n\n".join(
            [body]
            + [
                f"### tool {call['function']['name']} {call['id']}\n\n{fence('', tool_code(call))}"
                for call in message["tool_calls"]
            ]
        ).strip()
    return f"## {role}\n\n{body}"


def append(path, message):
    with path.open("a", encoding="utf-8") as file:
        if path.stat().st_size:
            file.write("\n\n")
        file.write(markdown_message(message))
    return markdown_messages(path.read_text(encoding="utf-8"))


def run_python(code):
    return subprocess.run([executable, "-c", code], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout[:20000]


def chat(client, path):
    messages = markdown_messages(path.read_text(encoding="utf-8"))
    while True:
        message = client.beta.chat.completions.parse(
            model=MODEL,
            messages=messages,
            tools=[TOOL],
            parallel_tool_calls=False,
        ).choices[0].message
        item = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            item["tool_calls"] = [call.model_dump(exclude={"function": {"parsed_arguments"}}) for call in message.tool_calls]
        messages = append(path, item)
        if not message.tool_calls:
            return

        for call in message.tool_calls:
            messages = append(path, {"role": "tool", "tool_call_id": call.id, "content": run_python(call.function.parsed_arguments.code)})


def main():
    chat(OpenAI(api_key=environ["OPENAI_API_KEY"], base_url=environ["OPENAI_BASE_URL"]), Path(argv[1]))


if __name__ == "__main__":
    main()
