# Paperclip `process` adapter contract (verified 2026-06-05 BKK)

Plan-phase research artifact for `~/.claude/plans/setup-paperclip-for-me-swift-newell.md`. Captures what the process adapter actually does, not what the README claims, after empirically running the Hyperbrowser chain through it end-to-end.

## Where I was wrong before

The earlier plan said *"Bash adapter is the mature path."* **There is no `bash` adapter.** That term came from a WebFetch summary of the README that may have been hallucinated or paraphrased loosely. The actual generic-shell-spawn primitive Paperclip ships is the **`process`** adapter, defined server-side at `paperclip/server/src/adapters/process/` (NOT as a separate npm package under `packages/adapters/`).

Other server-side built-in adapters: `http`. Everything else under `packages/adapters/` is LLM-coding-agent specific (claude-local, codex-local, cursor-local, gemini-local, grok-local, opencode-local, pi-local, acpx-local, openclaw-gateway, cursor-cloud).

## Adapter config (`adapterType: "process"`)

Source: `server/src/adapters/process/execute.ts`

| Field | Type | Default | Purpose |
|---|---|---|---|
| `command` | string | (required) | Executable path or shell command |
| `args` | string[] | `[]` | Arguments to pass |
| `cwd` | string | `process.cwd()` | Working directory |
| `env` | object | `{}` | Extra env vars (merged onto Paperclip's injected set) |
| `timeoutSec` | number | `0` (no timeout) | Hard timeout in seconds |
| `graceSec` | number | `15` | SIGTERM grace period before SIGKILL |

Captures stdout + stderr to `resultJson`. Non-zero exit code → run status `failed` with `errorMessage`. Exit 0 → `succeeded`.

## Env vars Paperclip injects (via `buildPaperclipEnv`)

- `PAPERCLIP_API_URL` — base URL for callback API
- `PAPERCLIP_API_KEY` — bearer token for API writes (in non-local-trusted modes)
- `PAPERCLIP_RUN_ID` — current run identifier (also goes in `X-Paperclip-Run-Id` header for callback writes)
- `PAPERCLIP_RESOLVED_COMMAND` — the resolved absolute command path
- Plus `PATH`, `HOME` etc. from the parent process

The auth-rule pattern (visible in `hermes_local` adapter source): **agents that write back to Paperclip use `Authorization: Bearer $PAPERCLIP_API_KEY` + `X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID`.**

## Task input is NOT passed via argv

This is the most counter-intuitive part. Paperclip's model is **heartbeat-based**: the adapter spawns the process with credentials + run-id; the process is expected to **call back to the API** to fetch its assigned issue's title/description/body. There is no `PAPERCLIP_TASK_INPUT` env var or argv equivalent.

For v1 we side-step this by hardcoding the target URL in `hyperbrowser_signup.sh` — the wrapper isn't generic "log into any URL," it's specifically "sign up to Hyperbrowser." That matches the YAGNI scope: per-tool wrappers, no detection/routing.

If a future wrapper needs the task body, it'd call `GET /api/heartbeat-runs/$PAPERCLIP_RUN_ID` (or equivalent) and parse the issue description.

## REST endpoints used (verified, dev mode `local_trusted`)

| Action | Method + path | Notes |
|---|---|---|
| Health | `GET /api/health` | Returns `{status, deploymentMode, authReady, bootstrapStatus, ...}` |
| Create company | `POST /api/companies` | Body `{name, slug}` |
| List companies | `GET /api/companies` | |
| Create agent | `POST /api/companies/:companyId/agents` | Body `{name, role, adapterType, adapterConfig, ...}` |
| Wake agent | `POST /api/agents/:agentId/wakeup` | Body `{source, reason}`; returns the spawned run object |
| Get run | `GET /api/heartbeat-runs/:runId` | Fields: `status`, `exitCode`, `startedAt`, `finishedAt`, `resultJson` (with `stdout` + `stderr`), `stdoutExcerpt`, `stderrExcerpt` |
| Create issue | `POST /api/companies/:companyId/issues` | Body `{title, description, status, assigneeAgentId}` — status MUST be set explicitly (no default) |
| List issues | `GET /api/companies/:companyId/issues` | |

`local_trusted` deployment mode (the default `pnpm dev` setup) needs no `Authorization` header on the dev box — Paperclip trusts the local origin.

## What Paperclip does NOT do that I expected

1. **Does not auto-close the issue on exit 0.** The run transitions to `succeeded`, but the issue stays `in_progress`. The agent is expected to PATCH the issue status via callback API. Process adapter doesn't do this automatically — would need wrapper to call `PATCH /api/issues/:id` with `{status: "done"}` before exiting.
2. **Does not pass the task body to the spawned process.** Heartbeat model: agent fetches its own work. For v1 hardcoded wrappers this is fine.
3. **Heartbeat is disabled by default** on new agents (`runtimeConfig.heartbeat.enabled: false`). You must `POST /agents/:id/wakeup` manually for the first run. To enable polling, PATCH the agent's `runtimeConfig.heartbeat`.

## Verified shape of a working agent registration

```json
{
  "name": "hyperbrowser-signup-agent",
  "role": "general",
  "adapterType": "process",
  "adapterConfig": {
    "command": "/home/anak/dev/scrap/scripts/hyperbrowser_signup.sh",
    "args": [],
    "cwd": "/home/anak/dev/scrap",
    "timeoutSec": 900
  }
}
```

Result of running this end-to-end (2026-06-05 08:28→08:30 UTC, 116s wall time):
- exitCode `0`
- stdout final line: `{"status":"done","tool":"hyperbrowser","api_key":"hb_022d548b1ba2dde9d36022896c28"}`
- resultJson.stdout contains full chain output
- resultJson.stderr contains opencli + claude-p traces from all 3 sub-agents

## Recipe for adding a second tool

```bash
# 1. Copy hyperbrowser_signup.sh, change URL + (optionally) sub-agent sequence
cp scripts/hyperbrowser_signup.sh scripts/<tool>_signup.sh
# edit URL=... inside

# 2. Register a new agent (single curl)
curl -X POST http://localhost:3100/api/companies/<companyId>/agents \
  -H "Content-Type: application/json" \
  -d '{"name":"<tool>-signup-agent","adapterType":"process",
       "adapterConfig":{"command":"/abs/path/<tool>_signup.sh","cwd":"/abs/path","timeoutSec":900}}'

# 3. Create + wake
curl -X POST http://localhost:3100/api/companies/<companyId>/issues \
  -d '{"title":"...","status":"in_progress","assigneeAgentId":"<id>"}'
curl -X POST http://localhost:3100/api/agents/<id>/wakeup -d '{"source":"on_demand"}'
```

When tool #3 needs login, the pressure of THREE concrete per-tool wrappers will design the generalization. Until then, YAGNI: copy-edit-register.
