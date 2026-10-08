# GitHub Copilot Session Dashboard

A minimalist, offline dashboard for GitHub Copilot Chat's cached VS Code session logs and the standalone Copilot CLI's local usage history. It reads local files read-only and has no third-party dependencies.

## Run

macOS/Linux:

```sh
python3 copilot_dashboard.py
```

Windows:

```bat
py copilot_dashboard.py
```

The app opens `http://localhost:8788` automatically. Keep the terminal running while using the dashboard and press `Ctrl-C` to stop it.

Useful options:

```sh
python3 copilot_dashboard.py --no-browser
python3 copilot_dashboard.py --port 9000
python3 copilot_dashboard.py --vscode-user-dir "/path/to/Code/User"
```

VS Code's `User` directory is auto-detected at `~/Library/Application Support/Code/User` on macOS, `%APPDATA%\Code\User` on Windows, and `~/.config/Code/User` (or `$XDG_CONFIG_HOME/Code/User`) on Linux. `Code - Insiders` is also checked as a fallback.

## Dashboard features

- Searchable, sortable session table with lifetime token totals, reported/estimated credits, current context occupancy, timings, and session timestamps.
- Current-month cards and per-model rankings attributed from per-call timestamps in the browser's local calendar timezone. Calls without timestamps are excluded from monthly totals, rather than assigned to a session's creation month.
- Manual refresh and optional 60-second auto-refresh.
- Per-row **Copy JSON** and **Download .md file** exports, including metric coverage and provenance.

## Local data sources

The server reconstructs and merges these sources at request time:

- **VS Code Chat sessions** — `workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl` and `globalStorage/emptyWindowChatSessions/<sessionId>.jsonl`. The JSONL files are append-only operations; `replay_session()` rebuilds each current session document. Adjacent `workspace.json` files provide workspace names.
- **VS Code local state** — `globalStorage/state.vscdb` provides best-effort plan/account labels only. No token or credential is read; no locally cached monthly quota is available.
- **Copilot CLI sessions** — `~/.copilot/session-store.db`, read through SQLite's read-only mode. The dashboard selects only known session/turn/usage columns and tolerates missing optional columns, including older schemas without `cache_write_tokens` or event `created_at`.
- **Copilot CLI event logs** — `~/.copilot/session-state/<id>/events.jsonl`, used by sessions launched from VS Code's Agents window (and newer CLI versions that no longer write `session-store.db`). Each event log yields title (first user message), model, workspace (`cwd`), timestamps, and cumulative credits from `session.usage_checkpoint` `totalNanoAiu`. Tokens, gen. time and context come from per-call `session.usage_record`/`assistant.usage` events when logged. Otherwise they come from `session.shutdown` (`modelMetrics`, `totalApiDurationMs`, `currentTokens`), whose totals are cumulative across resumes. Each process segment's delta is attributed to that model's last call in the segment. Calls after the latest shutdown remain unknown until the next shutdown. Context limits use the cached catalog's `capabilities.limits.max_prompt_tokens`. Runtime counts only active time: each span runs from a user message to the last assistant/tool event before the next message, shutdown or resume. It excludes idle gaps, and Last updated is the last such activity.
- **Copilot model catalog snapshots** — `globalStorage/github.copilot-chat/debug-logs/*/models.json` and equivalent workspace-storage paths. These are undocumented local debug artifacts, not a stable API. Valid snapshots are tried independently (a corrupt newest file does not mask an older valid one). Historical requests use the newest valid snapshot not newer than that request; if none is available, the dashboard does not silently apply a newer rate card. Selected-model picker prices are a fallback only for an exact matching model ID (allowing the known `copilot/` wrapper) and are marked with an unverified historical price date.

The application makes no network requests, reads no credentials, and does not call GitHub billing APIs. All storage access is local and read-only. If a Chat and CLI record share the exact session ID, the CLI duplicate is excluded; different IDs are not heuristically merged.

## Credits, prices, and coverage

GitHub's [published model-price page](https://docs.github.com/copilot/reference/copilot-billing/models-and-pricing) is in USD per million tokens, while Copilot Chat's cached `models.json` contains catalog billing prices and billing units. VS Code normalizes catalog rates using each tier's `batch_size`; this dashboard similarly normalizes cached rates to AI credits per million tokens and parses default/long-context plus input, output, cache-read, and cache-write rates. Since Chat logs do not reliably provide cache splits, estimates use default input/output rates and are marked partial when cache pricing is present. Invalid, non-finite, missing-unit, or unmatched prices are not used. The previous hand-entered `_PRICING_ROWS` and generic unknown-model rate were removed because they could drift from current prices and falsely imply precision. No rate is inferred from a model's display name.

For **Chat sessions**, cost accounting prefers persisted per-turn `copilotCredits` (including an explicit zero). If VS Code also persists `sessionCopilotCredits`, the dashboard reconciles it with turn credits using the larger reported session amount; it does not add a session total on top of its turns. A local catalog or exact-ID picker rate can produce an estimate only when model and token inputs are available. Rate estimates are explicitly marked estimated; missing cache-read/write breakdowns or an unknown long-context threshold make them partial estimates. Bring-your-own-key model calls are not counted as GitHub Copilot credits. Without a reported amount or defensible matching rate, cost is unavailable—not zero.

For **CLI sessions**, per-event `total_nano_aiu` is treated as the CLI-reported charge and converted to dashboard credits. Null/invalid event costs make the aggregate partial or unavailable. CLI cache reads and writes are retained separately; cache reads are a subset of input and are never added to IN.

**Cost and tokens are different confidence dimensions.** Some Chat logs lack `modelTotals`, which VS Code only supplies for some hosts. Where present and valid, `modelTotals` supports per-turn model input/cached/output totals. Otherwise legacy `result.metadata` values are retained as partial lifetime usage because local history may omit model, tool, or subagent calls. `Cached` is always a subset of `IN`; prompt-source breakdowns are not interpreted as cache hits. Unknown fields are null and render as `—`; a real zero remains zero. The CLI's per-event token details are more complete when the schema provides them.

## Lifetime usage versus current context

- **IN / OUT / Cached** are cumulative session totals across observed model calls. Cached input is included in IN.
- **Current context** is a separate occupancy snapshot from the last relevant persisted response's `contextUsage.currentTokens` and `tokenLimit`, or (when available) that response's `promptTokens + completionTokens`. A percentage is shown only with a persisted or verifiable configured limit. It is not a lifetime token total.
- CLI usage events do not describe current prompt occupancy, so CLI context is unavailable rather than guessed from lifetime totals.

## Monthly attribution and model rankings

Monthly cards and model rankings use call/event timestamps, not session creation timestamps. Browser-local calendar month boundaries match the dashboard's existing display timezone. Calls with absent or invalid timestamps cannot be assigned to a month and are omitted; the dashboard reports that timestamp limitation. Monthly credit totals are marked incomplete when any timestamped Copilot call has unavailable charges, and known subtotals are not presented as complete totals. Model metrics are based on observed request-model attribution; per-call reported credits may not be divisible among multiple models used within a single turn.

The plan and account labels are best-effort values from VS Code's local state database. No local monthly quota is available, so the dashboard does not claim to show an authoritative account-wide balance. Local session history also excludes activity on other devices and products.

## Privacy and limitations

- The server binds to `localhost` by default and serves no external assets.
- VS Code and CLI storage schemas are private implementation details and may change. Unavailable or malformed fields degrade to partial/unavailable values.
- Catalog rates are estimates rather than server-confirmed billed amounts and may differ because of billing adjustments, cache accounting, long-context rules, or historical pricing changes.
- Session transcripts, tool-call details, and file contents are not surfaced; only metadata and aggregate counts are used.
- Local files can lag behind the latest editor state; the page includes the data generation time.
