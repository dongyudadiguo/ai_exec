import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

SUMMARY_PREFIX = "[Compacted context summary; archived messages were replaced locally]"


def resolve_input_path(input_path=None):
    """Resolve the conversation transcript path.

    Order:
    1. explicit input_path argument
    2. AE_INPUT_FILE (optional; multi-chat runners / future viewer)
    3. conversations/<AE_CONVERSATION_ID>.json
    4. ./input.json (current single-file viewer: ae.py argv[1] = input.json)
    5. parent of skills package → input.json (tool child cwd may be agent/)
    """
    if input_path:
        return str(Path(input_path).resolve())
    env_path = (os.environ.get("AE_INPUT_FILE") or "").strip()
    if env_path:
        return str(Path(env_path).resolve())
    cid = (os.environ.get("AE_CONVERSATION_ID") or "").strip()
    if cid:
        cand = Path("conversations") / f"{cid}.json"
        if cand.is_file():
            return str(cand.resolve())
    # Current JUST agent layout: single root transcript
    for cand in (Path("input.json"), Path(__file__).resolve().parents[2] / "input.json"):
        if cand.is_file():
            return str(cand.resolve())
    raise FileNotFoundError(
        "no transcript path: pass a file, set AE_INPUT_FILE / AE_CONVERSATION_ID, "
        "or run from agent/ with input.json present"
    )


def _get_transcript(data):
    """Return (container_dict, key, items_list) for either Responses or Chat formats.

    Current ae.py / viewer use Responses API shape:
      data["json"]["input"] = [items...]
      data["json"]["instructions"] = system text (optional)

    Legacy chat shape:
      data["json"]["messages"] = [{role, content, ...}, ...]
    """
    body = data.get("json")
    if not isinstance(body, dict):
        raise ValueError("input JSON has no json object")

    if isinstance(body.get("input"), list):
        return body, "input", body["input"]
    if isinstance(body.get("messages"), list):
        return body, "messages", body["messages"]
    raise ValueError("input JSON has neither json.input nor json.messages list")


def _is_system_item(item, transcript_key):
    if not isinstance(item, dict):
        return False
    if item.get("role") == "system":
        return True
    # Responses API sometimes uses type=message with role=system
    if transcript_key == "input" and item.get("type") == "message" and item.get("role") == "system":
        return True
    return False


def _leading_system_count(items, transcript_key):
    count = 0
    for item in items:
        if not _is_system_item(item, transcript_key):
            break
        count += 1
    return count


def _is_user_item(item):
    if not isinstance(item, dict):
        return False
    if item.get("role") == "user":
        return True
    return item.get("type") == "message" and item.get("role") == "user"


def _boundary_from_user_turns(items, keep_user_turns):
    if keep_user_turns < 0:
        raise ValueError("keep_user_turns must be non-negative")
    if keep_user_turns == 0:
        return len(items)
    users = [i for i, item in enumerate(items) if _is_user_item(item)]
    if not users:
        return len(items)
    return users[max(0, len(users) - keep_user_turns)]


def _call_id(item):
    if not isinstance(item, dict):
        return None
    return item.get("call_id") or item.get("id")


def _iter_tool_call_ids(item):
    """Yield tool/function call ids declared by an assistant/function_call item."""
    if not isinstance(item, dict):
        return
    # Responses API function_call item
    if item.get("type") == "function_call":
        cid = item.get("call_id") or item.get("id")
        if cid:
            yield cid
        return
    # Chat-completions style assistant.tool_calls
    for call in item.get("tool_calls") or []:
        cid = call.get("id") or call.get("call_id")
        if cid:
            yield cid


def _is_tool_result(item):
    if not isinstance(item, dict):
        return False
    if item.get("type") == "function_call_output":
        return True
    return item.get("role") == "tool"


def _tool_result_call_id(item):
    if not isinstance(item, dict):
        return None
    return item.get("call_id") or item.get("tool_call_id")


def _validate_retained_tools(items):
    calls = set()
    for item in items:
        for cid in _iter_tool_call_ids(item):
            calls.add(cid)
        if _is_tool_result(item):
            cid = _tool_result_call_id(item)
            if cid not in calls:
                raise ValueError(
                    f"retained tool result {cid!r} has no retained assistant/function call"
                )


def _active_tool_boundary(items):
    """Return index of the active function_call / tool_calls group.

    Compatible with:
    - Responses API: function_call items (+ optional reasoning) followed only by
      function_call_output items for those call_ids.
    - Chat style: assistant message with tool_calls, followed only by tool results.
    """
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        call_ids = set(_iter_tool_call_ids(item))
        if not call_ids:
            continue

        # Include immediately preceding Responses function_calls in the same group
        # (parallel calls are separate items). Also allow reasoning between them.
        start = index
        while start > 0:
            prev = items[start - 1]
            prev_ids = set(_iter_tool_call_ids(prev))
            if prev_ids:
                call_ids |= prev_ids
                start -= 1
                continue
            if prev.get("type") == "reasoning":
                start -= 1
                continue
            break

        trailing = items[index + 1 :]
        if not trailing:
            return start
        if all(
            _is_tool_result(x) and _tool_result_call_id(x) in call_ids
            for x in trailing
        ):
            return start

        # A later non-result message means this group is completed history.
        break
    raise ValueError("no active assistant tool-call group found")


def _summary_item(summary, transcript_key, summary_role="user"):
    text = SUMMARY_PREFIX + "\n\n" + summary.strip()
    if transcript_key == "input":
        # Prefer the same bare user shape ae.py / existing history already uses.
        return {
            "role": summary_role,
            "content": [
                {"type": "input_text" if summary_role == "user" else "output_text", "text": text}
            ],
        }
    return {"role": summary_role, "content": text}


def compact_file(
    input_path,
    summary,
    keep_from_index=None,
    keep_user_turns=0,
    summary_role="user",
):
    path = Path(input_path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    body, transcript_key, items = _get_transcript(data)
    if not summary.strip():
        raise ValueError("summary is empty")

    system_count = _leading_system_count(items, transcript_key)
    if keep_from_index is None:
        boundary = _boundary_from_user_turns(items, keep_user_turns)
    else:
        boundary = keep_from_index
    if boundary < system_count or boundary > len(items):
        raise ValueError(
            f"boundary {boundary} must be between {system_count} and {len(items)}"
        )

    retained = items[boundary:]
    _validate_retained_tools(retained)
    summary_message = _summary_item(summary, transcript_key, summary_role=summary_role)
    compacted = items[:system_count] + [summary_message] + retained

    before_messages = len(items)
    before_bytes = path.stat().st_size
    body[transcript_key] = compacted
    # instructions (Responses system) already live outside input; leave untouched.
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + f".precompact-{stamp}.bak")
    shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".compact.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()

    return {
        "backup": str(backup),
        "format": transcript_key,
        "boundary": boundary,
        "messages_before": before_messages,
        "messages_after": len(compacted),
        "bytes_before": before_bytes,
        "bytes_after": len(encoded),
        "instructions_preserved": bool(
            isinstance(body.get("instructions"), str) and body.get("instructions")
        ),
    }


def _stop_runner():
    """Stop the ae.py runner so it cannot start another API turn after compaction.

    ae.py executes tool code in-process (``exec(code, _ns)``), so this function
    runs *inside* the runner: ``os.getppid()`` would point at viewer.py, not at
    ae.py. Killing that parent would take down the viewer while leaving ae.py
    free to POST the freshly compacted transcript. Ending the current process is
    therefore the correct way to honor "compact then stop" without editing ae.py.

    Exiting here also means ae.py never appends a tool_result for this call,
    which is what we want: the compacted transcript has no matching tool_use.
    """
    if os.environ.get("AE_RUNNER") != "1":
        return {"attempted": False, "reason": "AE_RUNNER not set"}

    pid = os.getpid()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    # os._exit skips atexit/buffer flushing on purpose: the transcript is already
    # committed to disk and any further ae.py work would corrupt it.
    os._exit(0)
    return {"attempted": True, "pid": pid, "ok": True}  # unreachable


def compact_active_file(input_path=None, summary=""):
    """Replace non-system history with summary and stop the active ae.py runner.

    Retaining the in-flight tool group would leave the old runner free to make
    another model call after this child exits. For compaction-as-stop we write a
    clean summary-only transcript, then terminate the parent runner when this
    process was launched under AE_RUNNER=1.

    If input_path is omitted, uses AE_INPUT_FILE when the viewer launched this
    runner for a specific conversation.
    """
    path = resolve_input_path(input_path)
    result = compact_file(path, summary, keep_user_turns=0)
    result["stopped_runner"] = _stop_runner()
    return result


def compact_active_file_keep_tools(input_path=None, summary=""):
    """Legacy active compact: keep the current tool group and do not stop ae.py."""
    path = resolve_input_path(input_path)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    _, _, items = _get_transcript(data)
    boundary = _active_tool_boundary(items)
    return compact_file(path, summary, keep_from_index=boundary)


def _load_summary(args, parser):
    has_text = args.summary is not None
    has_file = args.summary_file is not None
    if has_text == has_file:
        parser.error("provide exactly one of --summary or --summary-file")
    if has_text:
        return args.summary
    return Path(args.summary_file).read_text(encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compact ae.py context (Responses json.input or legacy json.messages)"
    )
    parser.add_argument(
        "input_json",
        nargs="?",
        default=None,
        help="transcript path; default AE_INPUT_FILE / AE_CONVERSATION_ID",
    )
    parser.add_argument(
        "--summary",
        help="inline summary text (preferred; do not write a current_summary.md file)",
    )
    parser.add_argument(
        "--summary-file",
        help="optional path to an existing summary file; prefer --summary instead",
    )
    parser.add_argument(
        "--active",
        action="store_true",
        help="active compact-and-stop (summary only; stops parent when AE_RUNNER=1)",
    )
    parser.add_argument(
        "--active-keep-tools",
        action="store_true",
        help="legacy active compact that retains the current tool-call group",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--keep-from-index", type=int)
    group.add_argument("--keep-from-user", type=int, default=0)
    args = parser.parse_args(argv)
    summary = _load_summary(args, parser)
    target = resolve_input_path(args.input_json)
    if args.active and args.active_keep_tools:
        parser.error("--active cannot be combined with --active-keep-tools")
    if args.active:
        if args.keep_from_index is not None or args.keep_from_user:
            parser.error("--active cannot be combined with a retention boundary")
        result = compact_active_file(target, summary)
    elif args.active_keep_tools:
        if args.keep_from_index is not None or args.keep_from_user:
            parser.error("--active-keep-tools cannot be combined with a retention boundary")
        result = compact_active_file_keep_tools(target, summary)
    else:
        result = compact_file(
            target,
            summary,
            keep_from_index=args.keep_from_index,
            keep_user_turns=args.keep_from_user,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
