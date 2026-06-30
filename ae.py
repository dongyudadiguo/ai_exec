import subprocess
import re
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

ROLE_HEADING = re.compile(r"^##\s+(System|Developer|User|Assistant)\s*$", re.IGNORECASE)
ROLE_ALIASES = {
    "system": "system",
    "developer": "developer",
    "user": "user",
    "assistant": "assistant",
}


def markdown_messages(text):
    messages = []
    role = "user"
    lines = []

    def flush():
        nonlocal lines
        content = "\n".join(lines).strip()
        if content:
            messages.append({"role": role, "content": content})
        lines = []

    for line in text.splitlines():
        match = ROLE_HEADING.match(line.strip())
        if match:
            flush()
            role = ROLE_ALIASES[match.group(1).lower()]
            continue
        lines.append(line)

    flush()
    return messages or [{"role": "user", "content": text}]


def write_md(file, text):
    for c in text:
        file.write(c)
        file.flush()


def run_python(file, code):
    p = subprocess.Popen(
        [executable, "-u", "-c", code],
        env={**environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    out = []
    for c in iter(lambda: p.stdout.read(1), ""):
        out.append(c)
        write_md(file, c)
    p.wait()
    out = "".join(out)
    if p.returncode:
        out += f"\n[exit {p.returncode}]"
        write_md(file, f"\n[exit {p.returncode}]")
    return out


def tool_result(file, call):
    code = call.function.parsed_arguments.code
    write_md(file, f"\n\n## Tool: {call.function.name}\n\n```python\n{code}\n```\n\n```text\n")
    out = run_python(file, code)
    if len(out) > 20000:
        write_md(file, "\n[context truncated]")
    write_md(file, "\n```\n")
    return out[:20000]


def chat(client, path):
    messages = markdown_messages(path.read_text(encoding="utf-8"))
    with path.open("a", encoding="utf-8") as file:
        while True:
            calls = []
            message = client.beta.chat.completions.parse(
                model=MODEL,
                messages=messages,
                tools=[TOOL],
                parallel_tool_calls=False,
            ).choices[0].message
            if message.content and message.content.strip():
                write_md(file, f"\n\n## Assistant\n\n{message.content}")

            for call in message.tool_calls or []:
                calls.append(call)
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [call.model_dump(exclude={"function": {"parsed_arguments"}})],
                })
                messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_result(file, call)})

            if not calls:
                messages.append({"role": "assistant", "content": message.content or ""})
                return


def main():
    chat(OpenAI(api_key=environ["OPENAI_API_KEY"], base_url=environ["OPENAI_BASE_URL"]), Path(argv[1]))


if __name__ == "__main__":
    main()
