import re
import subprocess
from itertools import batched
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


def markdown_messages(text):
    return [
        {"role": role.strip().lower(), "content": content.strip()}
        for role, content in batched(re.split(r"(?m)^##\s+(.+?)\s*$", text)[1:], 2)
    ]


def save(path, messages):
    path.write_text("\n\n".join(f"## {m['role']}\n\n{m['content']}" for m in messages), encoding="utf-8")


def run_python(code):
    return subprocess.check_output([executable, "-c", code], text=True, stderr=subprocess.STDOUT)[:20000]


def chat(client, path):
    messages = markdown_messages(path.read_text(encoding="utf-8"))
    context = []
    while True:
        message = client.beta.chat.completions.parse(
            model=MODEL,
            messages=messages + context,
            tools=[TOOL],
            parallel_tool_calls=False,
        ).choices[0].message
        item = {"role": "assistant", "content": message.content}
        if not message.tool_calls:
            messages.append(item)
            save(path, messages)
            return

        item["tool_calls"] = [call.model_dump(exclude={"function": {"parsed_arguments"}}) for call in message.tool_calls]
        context.append(item)
        for call in message.tool_calls:
            context.append({"role": "tool", "tool_call_id": call.id, "content": run_python(call.function.parsed_arguments.code)})


def main():
    chat(OpenAI(api_key=environ["OPENAI_API_KEY"], base_url=environ["OPENAI_BASE_URL"]), Path(argv[1]))


if __name__ == "__main__":
    main()
