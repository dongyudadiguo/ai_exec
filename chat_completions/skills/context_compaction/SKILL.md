# Context Compaction Skill

Compress `ae.py` context (`input.json`) while retaining state. Auto-detects Responses API (`json.input`, `json.instructions`) or legacy Chat API (`json.messages`).

## Rules
1. Preserve `instructions` and leading `system` items exactly.
2. Replace all other items with 1 `user` summary string (use `input_text` parts for Responses API).
3. Create timestamped backup; write atomically.
4. Report format, item counts, and byte counts before/after.
5. Summary (inline string, no sidecar files): Retain only high-signal state (active goal, file paths, architecture, completed changes, verification, exact next task). No credentials/logs.

## Execution
**Offline** (no running ae.py):
`python -m skills.context_compaction.compact [path] --summary "..."`
*Retention (offline only):* `--keep-from-user N` or `--keep-from-index INDEX`.

**Active** (`AE_RUNNER=1`, stops ae.py by default to prevent auto-continue):
Python: `from skills.context_compaction.compact import compact_active_file; print(compact_active_file(summary="..."))`
CLI: `python -m skills.context_compaction.compact --summary "..." --active`
*Legacy auto-continue:* Use `--active-keep-tools` or `compact_active_file_keep_tools`.