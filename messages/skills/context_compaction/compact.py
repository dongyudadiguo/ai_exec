import argparse
import json
import os
import signal
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

SUMMARY_PREFIX = "[Compacted context summary; archived messages were replaced locally]"


def resolve_input_path(input_path=None):
    """Resolve the conversation transcript path.

    Order:
    1. explicit input_path argument
    2. AE_INPUT_FILE (set by viewer.py per chat)
    3. conversations/<AE_CONVERSATION_ID>.json
    4. ./input.json (single-file layout: ae.py argv[1] = input.json)
    5. parent of skills package -> input.json (tool child cwd may differ)
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
    for cand in (Path("input.json"), Path(__file__).resolve().parents[2] / "input.json"):
        if cand.is_file():
            return str(cand.resolve())
    raise FileNotFoundError(
        "no transcript path: pass a file, set AE_INPUT_FILE / AE_CONVERSATION_ID, "
        "or run from the agent dir with input.json present"
    )


def _get_transcript(data):
    """Return (body, "messages", items) for the Anthropic Messages API shape.

    ae.py in this folder posts:
      data["json"]["system"]   = system prompt (string or block list)
      data["json"]["messages"] = [{"role": ..., "content": [blocks]}, ...]

    The system prompt is a top-level field, not a message, so it is never part
    of the compaction window.
    """
    body = data.get("json")
    if not isinstance(body, dict):
        raise ValueError("input JSON has no json object")
    if isinstance(body.get("messages"), list):
        return body, "messages", body["messages"]
    raise ValueError("input JSON has no json.messages list (expected Messages API)")


def _blocks(item):
    """Normalize message content to a list of blocks."""
    if not isinstance(item, dict):
        return []
    content = item.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _is_system_item(item, transcript_key=None):
    """Messages API keeps system out of the array; guard for stray system rows."""
    return isinstance(item, dict) and item.get("role") == "system"


def _leading_system_count(items, transcript_key=None):
    count = 0
    for item in items:
        if not _is_system_item(item):
            break
        count += 1
    return count


def _is_user_item(item):
    """True for a real user turn, not a tool_result carrier message."""
    if not isinstance(item, dict) or item.get("role") != "user":
        return False
    return not _is_tool_result(item)


def _boundary_from_user_turns(items, keep_user_turns):
    if keep_user_turns < 0:
        raise ValueError("keep_user_turns must be non-negative")
    if keep_user_turns == 0:
        return len(items)
    users = [i for i, item in enumerate(items) if _is_user_item(item)]
    if not users:
        return len(items)
    return users[max(0, len(users) - keep_user_turns)]


def _iter_tool_call_ids(item):
    """Yield tool_use ids declared by an assistant message."""
    if not isinstance(item, dict) or item.get("role") != "assistant":
        return
    for block in _blocks(item):
        if block.get("type") == "tool_use":
            cid = block.get("id")
            if cid:
                yield cid


def _is_tool_result(item):
    """True when a user message carries only tool_result blocks."""
    if not isinstance(item, dict) or item.get("role") != "user":
        return False
    blocks = _blocks(item)
    if not blocks:
        return False
    return all(b.get("type") == "tool_result" for b in blocks)


def _iter_tool_result_ids(item):
    for block in _blocks(item):
        if block.get("type") == "tool_result":
            cid = block.get("tool_use_id")
            if cid:
                yield cid


def _validate_retained_tools(items):
    """Every retained tool_result must pair with a retained tool_use."""
    calls = set()
    for item in items:
        for cid in _iter_tool_call_ids(item):
            calls.add(cid)
        if _is_tool_result(item):
            for cid in _iter_tool_result_ids(item):
                if cid not in calls:
                    raise ValueError(
                        f"retained tool_result {cid!r} has no retained assistant tool_use"
                    )


def _active_tool_boundary(items):
    """Return the index of the in-flight assistant tool_use turn.

    The group is the last assistant message containing tool_use blocks whose
    trailing messages are only user tool_result messages for those ids.
    """
    for index in range(len(items) - 1, -1, -1):
        call_ids = set(_iter_tool_call_ids(items[index]))
        if not call_ids:
            continue
        trailing = items[index + 1 :]
        if not trailing:
            return index
        if all(
            _is_tool_result(x) and set(_iter_tool_result_ids(x)) <= call_ids
            for x in trailing
        ):
            return index
        break
    raise ValueError("no active assistant tool_use group found")


def _summary_text(summary):
    return SUMMARY_PREFIX + "\n\n" + summary.strip()


def _summary_item(summary, transcript_key=None, summary_role="user"):
    return {
        "role": summary_role,
        "content": [{"type": "text", "text": _summary_text(summary)}],
    }


def _splice_summary(head, summary, retained, summary_role="user"):
    """Insert the summary while keeping Messages API role alternation valid.

    A user summary followed by a retained user message (or an assistant summary
    followed by an assistant message) would break alternation, so in that case
    the summary is prepended as a text block to the existing message instead.
    """
    summary_item = _summary_item(summary, summary_role=summary_role)
    if retained and isinstance(retained[0], dict) and retained[0].get("role") == summary_role:
        first = dict(retained[0])
        blocks = list(_blocks(first))
        first["content"] = [{"type": "text", "text": _summary_text(summary)}] + blocks
        return head + [first] + list(retained[1:]), True
    return head + [summary_item] + list(retained), False


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

    system_count = _leading_system_count(items)
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
    compacted, merged = _splice_summary(
        items[:system_count], summary, retained, summary_role=summary_role
    )

    before_messages = len(items)
    before_bytes = path.stat().st_size
    body[transcript_key] = compacted
    # json.system stays untouched: it is a top-level field, not a message.
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

    system_value = body.get("system")
    return {
        "backup": str(backup),
        "format": transcript_key,
        "boundary": boundary,
        "messages_before": before_messages,
        "messages_after": len(compacted),
        "bytes_before": before_bytes,
        "bytes_after": len(encoded),
        "summary_merged_into_first_retained": merged,
        "system_preserved": bool(system_value),
    }


def _stop_parent_runner():
    """Stop the parent ae.py so it cannot start another API turn after compaction.

    ae.py continues its while-loop once tool children return. Ending the parent
    is the only way to honor "compact then stop" without editing ae.py. Tool
    children inherit AE_RUNNER=1 when launched from viewer.py.
    """
    if os.environ.get("AE_RUNNER") != "1":
        return {"attempted": False, "reason": "AE_RUNNER not set"}

    ppid = os.getppid()
    if not ppid or ppid <= 1:
        return {"attempted": False, "reason": "no parent pid"}

    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(ppid), "/F"],
                capture_output=True,
                text=True,
                errors="ignore",
            )
            return {
                "attempted": True,
                "pid": ppid,
                "ok": completed.returncode == 0,
                "returncode": completed.returncode,
                "output": ((completed.stdout or "") + (completed.stderr or "")).strip(),
            }
        os.kill(ppid, signal.SIGTERM)
        return {"attempted": True, "pid": ppid, "ok": True}
    except OSError as exc:
        return {"attempted": True, "pid": ppid, "ok": False, "error": str(exc)}


def compact_active_file(input_path=None, summary=""):
    """Replace non-system history with the summary and stop the active runner.

    Retaining the in-flight tool group would leave the old runner free to make
    another model call after this child exits, so the transcript becomes a
    single user summary message, then the parent runner is terminated when this
    process was launched under AE_RUNNER=1.
    """
    path = resolve_input_path(input_path)
    result = compact_file(path, summary, keep_user_turns=0)
    result["stopped_parent"] = _stop_parent_runner()
    return result


def compact_active_file_keep_tools(input_path=None, summary=""):
    """Legacy active compact: keep the current tool group and let ae.py continue.

    The retained assistant message keeps its tool_use blocks (and any thinking
    blocks with signatures), so the pending tool_result messages stay valid.
    """
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
        description="Compact ae.py context (Anthropic Messages API json.messages)"
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
        help="legacy active compact that retains the current tool_use group",
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
