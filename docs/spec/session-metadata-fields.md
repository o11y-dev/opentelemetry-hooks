# Session Metadata Fields

Persist only session-stable, low-cardinality metadata in `.state/sessions/<session>.json`.

## 1. Workspace identity
- `cwd_first_seen`
- `repo_root`
- `repo_name` (derived)
- `vcs_remote_host`
- `vcs_remote_repo` (sanitized)

## 2. Agent/runtime identity
- `ide`
- `agent_engine`
- `client_version`
- `hook_version`

## 3. Trace/session linkage
- `trace_id`
- `context_origin` (`synthetic|upstream`)
- `session_id` (if present)
- `conversation_id` (if present)

## 4. Model/tool summary
- `last_known_model`
- `models_seen` (set/list)
- `tools_seen` with counts
- `mcp_servers_seen`

## 5. File/work summary
- `files_touched_count`
- `top_dirs_touched` (repo-relative)
- `commands_count`

## 6. Timing/state
- `created_at`
- `updated_at`
- `last_event_at`
- `generation_count`
- `status` (`active|ended|aborted`)

## Notes
- Prefer normalized repo-relative paths over absolute paths.
- Do not store raw prompt/response text in session metadata.
- Cap list sizes to keep session files compact.
