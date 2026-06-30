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
        file.write(c)
        file.flush()
    p.wait()
    out = "".join(out)
    if p.returncode:
        out += f"\n[exit {p.returncode}]"
        file.write(f"\n[exit {p.returncode}]")
        file.flush()
    return out


def tool_result(file, call):
    code = call.function.parsed_arguments.code
    file.write(f"\n\n## Tool: {call.function.name}\n\n```python\n{code}\n```\n\n```text\n")
    file.flush()
    out = run_python(file, code)
    if len(out) > 20000:
        file.write("\n[context truncated]")
    file.write("\n```\n")
    file.flush()
    return out[:20000]


def chat(client, path):
    messages = markdown_messages(path.read_text(encoding="utf-8"))
    with path.open("a", encoding="utf-8") as file:
        while True:
            opened = False
            calls = []
            with client.chat.completions.stream(model=MODEL, messages=messages, tools=[TOOL], parallel_tool_calls=False) as stream:
                for event in stream:
                    if event.type == "content.delta":
                        if not opened and not event.delta.strip():
                            continue
                        if not opened:
                            file.write("\n\n## Assistant\n\n")
                            opened = True
                        file.write(event.delta)
                        file.flush()
                    if event.type == "tool_calls.function.arguments.done":
                        message = stream.current_completion_snapshot.choices[0].message
                        call = message.tool_calls[event.index]
                        calls.append(call)
                        messages.append({
                            "role": "assistant",
                            "content": message.content or "",
                            "tool_calls": [call.model_dump(exclude={"function": {"parsed_arguments"}})],
                        })
                        messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_result(file, call)})
                message = stream.get_final_completion().choices[0].message

            if not calls:
                messages.append({"role": "assistant", "content": message.content or ""})
                return


def main():
    chat(OpenAI(api_key=environ["OPENAI_API_KEY"], base_url=environ["OPENAI_BASE_URL"]), Path(argv[1]))


if __name__ == "__main__":
    main()
