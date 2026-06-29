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


def run_python(code):
    p = subprocess.run(
        [executable, "-c", code],
        env={**environ, "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    out = (p.stdout or "") + (p.stderr or "")
    return out + (f"\n[exit {p.returncode}]" if p.returncode else "")


def tool_result(file, call):
    code = call.function.parsed_arguments.code
    out = run_python(code)
    file.write(f"\n\n## Tool: {call.function.name}\n\n```python\n{code}\n```\n\n```text\n{out[:20000]}\n```\n")
    file.flush()
    return out[:20000]


def chat(client, path):
    messages = [{"role": "user", "content": path.read_text(encoding="utf-8")}]
    with path.open("a", encoding="utf-8") as file:
        while True:
            opened = False
            with client.chat.completions.stream(model=MODEL, messages=messages, tools=[TOOL]) as stream:
                for event in stream:
                    if event.type == "content.delta":
                        if not opened:
                            file.write("\n\n## Assistant\n\n")
                            opened = True
                        file.write(event.delta)
                        file.flush()
                message = stream.get_final_completion().choices[0].message

            calls = message.tool_calls or []
            messages.append({
                "role": "assistant",
                "content": message.content or "",
                **({"tool_calls": [c.model_dump(exclude={"function": {"parsed_arguments"}}) for c in calls]} if calls else {}),
            })
            if not calls:
                return
            for call in calls:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_result(file, call)})


def main():
    chat(OpenAI(api_key=environ["OPENAI_API_KEY"], base_url=environ["OPENAI_BASE_URL"]), Path(argv[1]))


if __name__ == "__main__":
    main()
