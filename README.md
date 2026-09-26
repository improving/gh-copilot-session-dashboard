# GitHub Copilot Session Dashboard

A minimalist, offline dashboard for GitHub Copilot Chat's cached VS Code session logs. It reads only local files, no third-party dependencies.

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

VS Code's `User` directory is auto-detected at:

- macOS: `~/Library/Application Support/Code/User`
- Windows: `%APPDATA%\Code\User`
- Linux: `~/.config/Code/User` (or `$XDG_CONFIG_HOME/Code/User`)

(`Code - Insiders` is also checked as a fallback on every platform.)

## Dashboard features

- Searchable, sortable session table: title, workspace, chat mode (agent/ask/edit), model, estimated credits consumed, estimated cost, input/output token counts, model context window, generation time, session runtime, created time, and last-updated time.
- Estimated credits consumed for the current calendar month (no plan has a locally-cached monthly limit, so this is always shown against an unbounded allotment).
- Current-month totals: session count, tokens in, tokens out, and top models by tokens, credits, and session count.
- Manual refresh and optional 60-second auto-refresh.
- Per-row share menu: **Copy JSON** copies the full session record (pretty-printed, including the estimated cost) to the clipboard, and **Download .md file** saves the session details as a Markdown file named after the title and created date.

## Where the data comes from

All data is read at request time from local files VS Code's GitHub Copilot Chat extension already writes to disk, all read-only:

- **Chat session logs** — one JSONL file per chat session at `workspaceStorage/<hash>/chatSessions/<sessionId>.jsonl` (per opened folder/workspace) and `globalStorage/emptyWindowChatSessions/<sessionId>.jsonl` (windows with no folder open). Each line is an append-only operation (`{"kind": 0|1|2, "k": [...path], "v": ...}`) — `kind 0` is the session's initial full document, `kind 1` sets a value at a path, `kind 2` appends items to a list at a path. `replay_session()` replays these in order to reconstruct each session's current state (title, per-request model, token counts, timings). There is no snapshot table to query directly, unlike a SQLite-backed tool.
- **`workspace.json`** next to each `workspaceStorage/<hash>/` folder, giving the workspace's folder path (and so its display name).

The server merges both sources in `read_state()` and exposes the result as a single JSON payload at `GET /api/data`. The browser then renders everything client-side from that payload; no computation happens against a remote service, and nothing here makes a network call or reads any auth token or API key.

### Dashboard cards

- **Plan card** — Plan name and account are both auto-detected from VS Code's own local state, no flags needed:
  - `resolve_plan_name()` reads the seat SKU VS Code caches for its own feature-flagging (`extensionsAssignmentFilterProvider.copilotSku` in `globalStorage/state.vscdb`, e.g. `copilot_enterprise_seat_quota`) and turns it into a label like "Copilot Enterprise". Falls back to the generic "GitHub Copilot" if that key isn't present.
  - `resolve_subscription_account()` reads the `github-<accountLabel>-usages` key VS Code itself keeps in the same database — only the account label, never the OAuth token, which VS Code stores separately in the OS keychain.
  - There is no local cache of a numeric monthly quota for **any** plan tier (GitHub only serves that live from its own servers for the in-editor quota banners, never persists it to disk) — searched `state.vscdb` and the `github.copilot-chat` extension's own storage/debug logs for it and found nothing. So the credit limit is always treated as unbounded; the card shows "Unlimited" rather than guessing a number.
  - Estimated cost is `credits consumed this month × $0.01`.
- **Credits consumed card** — Credits are computed **entirely from local session logs**, not fetched from GitHub. For each Copilot-hosted chat/agent request (`modelId` starting with `copilot/`), the dashboard reads that request's logged `promptTokens`/`outputTokens` (from `result.metadata` in the session log) and multiplies by the resolved model's published per-token rate from [GitHub's model pricing table](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing) (`MODEL_PRICING_BY_SLUG` in `copilot_dashboard.py`), converting at 1 credit = $0.01. Requests to a bring-your-own-key model (any `modelId` not prefixed `copilot/`, e.g. a directly configured Gemini or local Ollama model) are counted as 0 credits, since GitHub does not bill those. Models not in the published table fall back to a documented flat estimate (`DEFAULT_UNKNOWN_RATES`) rather than silently reading as zero. Since the monthly allotment is always unbounded (see above), there's no progress bar to compare against — just the running total.
- **Sessions this month card** — Computed in the browser from sessions whose `creationDate` falls in the current calendar month. Token totals sum each session's aggregated `inputTokens`/`outputTokens`.
- **Model metrics card** — Also browser-side, over the same current-month sessions, using each session's most-recently-used model (`result.metadata.resolvedModel`, falling back to the request's `modelId`).

### Session table

Each row is one chat session, reconstructed entirely from its own JSONL log:

- **Title** — the session's `customTitle` if VS Code has generated one, else `(untitled)`.
- **Workspace** — the folder name from that workspace's `workspace.json`, or "(no workspace)" for sessions opened in a window with no folder.
- **Mode** — the most recent request's `modeInfo.kind` (`agent`, `ask`, or `edit`).
- **Model** — the display name of the most recently used model, preferring the model catalog GitHub Copilot Chat itself caches locally (`inputState.selectedModel.metadata`, which is how the app learns model context-window sizes too) over a raw model id.
- **Credits** / **Est. cost** — see "Credits consumed card" above; summed per-request over the session.
- **Tokens in / out** — summed `result.metadata.promptTokens` / `outputTokens` over every request in the session. There is no cached-token count exposed anywhere in these logs, so there is no "Cached" column (Devin's equivalent tool has one; Copilot's local logs simply don't carry that figure).
- **Context** — the selected model's maximum input token window (a static model spec pulled from the same local model-catalog cache), not a live usage figure.
- **Gen. time** — summed `result.timings.totalElapsed` per request (falling back to the request's top-level `elapsedMs`).
- **Runtime** — the span from the session's first request timestamp to its last completed response; a simpler wall-clock figure than idle-gap-aware "active runtime," since Copilot's logs don't need that distinction the way a long-running background agent's do.

Sessions with no title and zero requests are hidden, and the list is sorted by last-updated time. The browser then applies your search text (matched against title, workspace, session ID, model, and mode) and column sorting on top of that list.

## Privacy and limitations

- The server binds to `localhost` by default and serves no external assets. It makes no network calls at all.
- Nothing here reads, decodes, or transmits any GitHub token, API key, or other credential. It only reads the same local session-metadata files VS Code's own Copilot Chat UI already reads.
- **Credits consumed is a local estimate, not your account's authoritative usage.** Unlike some agent tools that cache a server-confirmed usage total locally, GitHub Copilot does not persist your account's real-time credit balance anywhere on disk that VS Code can read. This dashboard reconstructs an estimate purely from the token counts your own local chat logs already record, using GitHub's currently published per-model rates. It will drift from your real GitHub billing if a model's rate changes after this file was written, or your account uses a model not in the pricing table (falls back to a flat estimate).
- Long-context pricing tiers for a given model are not distinguished locally (VS Code's logs don't record which pricing tier a request landed in), so all requests to a model price at that model's "default" tier rate.
- Session conversation transcripts, tool-call details, and file contents are not surfaced by this dashboard — only metadata and aggregate counts.
- Cached values can lag behind what VS Code has written most recently; the page displays the local data timestamp.
