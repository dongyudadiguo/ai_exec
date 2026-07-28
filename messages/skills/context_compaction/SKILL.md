# Context Compaction Skill

Compress `ae.py` context (`input.json`) while retaining state. Designed for the Anthropic Messages API (`json.system`, `json.messages` with content blocks).

## Rules
1. Preserve `system` (top-level field) and any leading `system` role messages exactly.
2. Replace archived messages with 1 `user` summary message containing a single `text` block.
3. If the summary and first retained message share the same role, prepend the summary text block into that message to maintain role alternation.
4. Create timestamped backup; write atomically.
5. Report format, item counts, byte counts, and whether the summary was merged.
6. Summary (inline string, no sidecar files): Retain only high-signal state (active goal, file paths, architecture, completed changes, verification, exact next task). No credentials/logs.

## Execution
**Offline** (no running ae.py):
`python -m skills.context_compaction.compact [path] --summary "..."`
*Retention (offline only):* `--keep-from-user N` or `--keep-from-index INDEX`.

**Active** (`AE_RUNNER=1`, stops ae.py by default to prevent auto-continue):
Python: `from skills.context_compaction.compact import compact_active_file; print(compact_active_file(summary="..."))`
CLI: `python -m skills.context_compaction.compact --summary "..." --active`
*Legacy auto-continue:* Use `--active-keep-tools` or `compact_active_file_keep_tools`.

## Messages API Shape
- `json.system`: system prompt (string or block list), preserved as top-level field
- `json.messages`: list of `{"role": "user"|"assistant", "content": [blocks]}`
- Tool use: assistant messages contain `{"type": "tool_use", "id": ...}` blocks
- Tool results: user messages contain `{"type": "tool_result", "tool_use_id": ...}` blocks
- Active boundary: last assistant message with tool_use blocks, followed only by user tool_result messages
