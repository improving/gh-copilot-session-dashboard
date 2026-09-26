#!/usr/bin/env python3
"""Minimal local dashboard for GitHub Copilot Chat's cached VS Code session logs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote


CREDIT_USD_RATE = 0.01  # 1 GitHub AI credit = $0.01 (usage-based billing, effective 2026-06-01)

APP_TITLE = "GitHub Copilot Session Dashboard"

FALLBACK_PLAN_NAME = "GitHub Copilot"

# Per-1M-token USD rates published at
# https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
_PRICING_ROWS: list[tuple[str, float, float, float]] = [
    ("GPT-5 mini", 0.25, 0.025, 2.00),
    ("GPT-5.3-Codex", 1.75, 0.175, 14.00),
    ("GPT-5.4", 2.50, 0.25, 15.00),
    ("GPT-5.4 (long context)", 5.00, 0.50, 22.50),
    ("GPT-5.4 mini", 0.75, 0.075, 4.50),
    ("GPT-5.4 nano", 0.20, 0.02, 1.25),
    ("GPT-5.5", 5.00, 0.50, 30.00),
    ("GPT-5.5 (long context)", 10.00, 1.00, 45.00),
    ("GPT-5.6 Luna", 0.20, 0.02, 1.20),
    ("GPT-5.6 Luna (long context)", 0.40, 0.04, 1.80),
    ("GPT-5.6 Sol", 4.00, 0.40, 20.00),
    ("GPT-5.6 Sol (long context)", 8.00, 0.80, 30.00),
    ("GPT-5.6 Terra", 2.00, 0.20, 12.00),
    ("GPT-5.6 Terra (long context)", 4.00, 0.40, 18.00),
    ("GPT-6 Astra", 10.00, 1.00, 50.00),
    ("GPT-6 Astra (long context)", 20.00, 2.00, 75.00),
    ("GPT-6 Luna", 0.10, 0.01, 0.50),
    ("GPT-6 Luna (long context)", 0.20, 0.02, 0.75),
    ("GPT-6 Sol", 2.00, 0.20, 10.00),
    ("GPT-6 Sol (long context)", 4.00, 0.40, 15.00),
    ("Claude Haiku 4.5", 1.00, 0.10, 5.00),
    ("Claude Sonnet 4", 3.00, 0.30, 15.00),
    ("Claude Sonnet 4.6", 3.00, 0.30, 15.00),
    ("Claude Opus 4.7", 5.00, 0.50, 25.00),
    ("Claude Opus 4.8", 5.00, 0.50, 25.00),
    ("Claude Opus 4.8 (fast mode)", 10.00, 1.00, 50.00),
    ("Claude Opus 5", 5.00, 0.50, 25.00),
    ("Claude Opus 5.5", 4.00, 0.20, 20.00),
    ("Claude Sonnet 5", 2.00, 0.20, 10.00),
    ("Claude Fable 5", 10.00, 1.00, 50.00),
    ("Claude Fable 5.1", 10.00, 0.25, 50.00),
    ("Gemini 3.5 Flash", 1.50, 0.15, 9.00),
    ("Gemini 3.6 Flash", 0.75, 0.075, 3.75),
    ("Gemini 3.7 Flash", 0.75, 0.075, 3.75),
    ("Gemini 3.8 Flash", 0.75, 0.075, 3.75),
    ("MAI-Code-1.1-Flash", 0.20, 0.02, 1.20),
    ("Grok 4.5", 2.00, 0.50, 6.00),
    ("Grok 4.6", 2.00, 0.50, 6.00),
    ("Grok 4.7", 2.00, 0.50, 6.00),
    ("Grok 4.5 (long context)", 4.00, 1.00, 12.00),
    ("Grok 4.6 (long context)", 4.00, 1.00, 12.00),
    ("Grok 4.7 (long context)", 4.00, 1.00, 12.00),
    ("Kimi K2.7 Code", 0.95, 0.19, 4.00),
    ("Kimi K3", 3.00, 0.30, 15.00),
]

DEFAULT_UNKNOWN_RATES = {"input": 3.0, "cached_input": 0.3, "output": 15.0}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


MODEL_PRICING_BY_SLUG: dict[str, dict[str, float]] = {}
MODEL_PRICING_DISPLAY: dict[str, str] = {}
for _name, _in, _cached, _out in _PRICING_ROWS:
    _key = _slug(_name)
    MODEL_PRICING_BY_SLUG[_key] = {"input": _in, "cached_input": _cached, "output": _out}
    MODEL_PRICING_DISPLAY[_key] = _name


def candidate_vscode_user_dirs() -> list[Path]:
    """Return VS Code's per-user data directory in platform-specific priority order."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Code/User")
            candidates.append(Path(appdata) / "Code - Insiders/User")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library/Application Support/Code/User")
        candidates.append(Path.home() / "Library/Application Support/Code - Insiders/User")
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            candidates.append(Path(config_home) / "Code/User")
            candidates.append(Path(config_home) / "Code - Insiders/User")
        candidates.append(Path.home() / ".config/Code/User")
        candidates.append(Path.home() / ".config/Code - Insiders/User")
    return list(dict.fromkeys(candidates))


def resolve_vscode_user_dir(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    for path in candidate_vscode_user_dirs():
        if path.is_dir():
            return path
    searched = "\n".join(f"  - {path}" for path in candidate_vscode_user_dirs())
    raise FileNotFoundError(f"VS Code user directory not found. Searched:\n{searched}")


def _open_ro_sqlite(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _open_state_db(user_dir: Path) -> sqlite3.Connection | None:
    """Open VS Code's own global state database (key/value cache), read-only."""
    return _open_ro_sqlite(user_dir / "globalStorage" / "state.vscdb")


def resolve_copilot_cli_dir() -> Path:
    """Location of the standalone GitHub Copilot CLI's own local state.

    This is a separate product from VS Code's Copilot Chat extension (which shims the CLI in
    under globalStorage/github.copilot-chat/copilotCli/ to run it), but the CLI keeps its own
    session history under the user's home directory regardless of which host launched it.
    """
    return Path.home() / ".copilot"


def resolve_subscription_account(user_dir: Path) -> str | None:
    """Best-effort local lookup of the GitHub account signed in for Copilot.

    VS Code records which account each auth provider was last used by as
    `github-<accountLabel>-usages` keys in its own state database. This reads
    only that label (never the OAuth token, which VS Code keeps in the OS
    keychain instead).
    """
    conn = _open_state_db(user_dir)
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'github-%-usages'").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    best_label: str | None = None
    best_used = -1.0
    prefix, suffix = "github-", "-usages"
    for key, value in rows:
        label = key[len(prefix):-len(suffix)]
        if not label:
            continue
        try:
            entries = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            entries = []
        last_used = 0.0
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("lastUsed"), (int, float)):
                    last_used = max(last_used, entry["lastUsed"])
        if last_used > best_used:
            best_used = last_used
            best_label = label
    return best_label


_PLAN_SKU_SUFFIXES = ("_seat_quota", "_seat", "_quota")


def resolve_plan_name(user_dir: Path) -> str | None:
    """Best-effort local lookup of the Copilot plan/seat tier.

    VS Code caches the seat SKU it was assigned (for its own feature-flagging)
    under `extensionsAssignmentFilterProvider.copilotSku`, e.g.
    'copilot_enterprise_seat_quota'. This turns that into a display label
    ('Copilot Enterprise'). There's no local cache of a numeric monthly quota
    to go with it — GitHub only serves that live from its own servers, never
    to disk — so the credit limit is always treated as unbounded in
    read_state() regardless of plan.
    """
    conn = _open_state_db(user_dir)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key = 'extensionsAssignmentFilterProvider.copilotSku'"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    sku = row[0] if row else None
    if not isinstance(sku, str) or not sku:
        return None
    tier = sku[len("copilot_"):] if sku.startswith("copilot_") else sku
    for suffix in _PLAN_SKU_SUFFIXES:
        if tier.endswith(suffix):
            tier = tier[: -len(suffix)]
            break
    tier = tier.replace("_", " ").strip()
    return f"Copilot {tier.title()}" if tier else None


def resolve_workspace_name(workspace_dir: Path) -> str:
    workspace_json = workspace_dir / "workspace.json"
    if workspace_json.is_file():
        try:
            data = json.loads(workspace_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        folder = data.get("folder") if isinstance(data, dict) else None
        if isinstance(folder, str) and folder:
            path_part = folder.split("://", 1)[-1]
            name = Path(unquote(path_part)).name
            if name:
                return name
    return "(no workspace)"


def iter_session_files(user_dir: Path) -> list[tuple[Path, str]]:
    """Yield (jsonl_path, workspace_name) for every local Copilot chat session log."""
    found: list[tuple[Path, str]] = []
    workspace_storage = user_dir / "workspaceStorage"
    if workspace_storage.is_dir():
        for ws_dir in sorted(p for p in workspace_storage.iterdir() if p.is_dir()):
            chat_dir = ws_dir / "chatSessions"
            if not chat_dir.is_dir():
                continue
            workspace = resolve_workspace_name(ws_dir)
            for jsonl_path in sorted(chat_dir.glob("*.jsonl")):
                found.append((jsonl_path, workspace))
    empty_dir = user_dir / "globalStorage" / "emptyWindowChatSessions"
    if empty_dir.is_dir():
        for jsonl_path in sorted(empty_dir.glob("*.jsonl")):
            found.append((jsonl_path, "(no workspace)"))
    return found


def _apply_patch(root: Any, kind: int, path: list, value: Any) -> None:
    """Apply one VS Code chat-session log op: kind 1 sets a path, kind 2 appends to a list at a path."""
    if not path or not isinstance(root, (dict, list)):
        return
    node = root
    for i, key in enumerate(path[:-1]):
        next_key = path[i + 1]
        want_list = isinstance(next_key, int)
        if isinstance(key, int):
            if not isinstance(node, list):
                return
            while len(node) <= key:
                node.append([] if want_list else {})
            if want_list and not isinstance(node[key], list):
                node[key] = []
            elif not want_list and not isinstance(node[key], dict):
                node[key] = {}
            node = node[key]
        else:
            if not isinstance(node, dict):
                return
            existing = node.get(key)
            if want_list and not isinstance(existing, list):
                node[key] = []
            elif not want_list and not isinstance(existing, dict):
                node[key] = {}
            node = node[key]
    last = path[-1]
    if kind == 1:
        if isinstance(last, int):
            if isinstance(node, list):
                while len(node) <= last:
                    node.append(None)
                node[last] = value
        elif isinstance(node, dict):
            node[last] = value
    elif kind == 2:
        if isinstance(last, int):
            if isinstance(node, list):
                while len(node) <= last:
                    node.append([])
                if not isinstance(node[last], list):
                    node[last] = []
                if isinstance(value, list):
                    node[last].extend(value)
        elif isinstance(node, dict):
            target = node.get(last)
            if not isinstance(target, list):
                target = []
                node[last] = target
            if isinstance(value, list):
                target.extend(value)


def replay_session(path: Path) -> dict | None:
    """Reconstruct a chat session's current document from its append-only JSONL op log."""
    root: Any = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                value = rec.get("v")
                if kind == 0:
                    root = value
                    continue
                if root is None:
                    continue
                _apply_patch(root, kind, rec.get("k") or [], value)
    except OSError:
        return None
    return root if isinstance(root, dict) else None


def harvest_model_catalog(doc: dict, catalog: dict[str, dict[str, Any]]) -> None:
    """Opportunistically learn model display names / context windows from a session's selected-model cache."""
    input_state = doc.get("inputState")
    selected = input_state.get("selectedModel") if isinstance(input_state, dict) else None
    meta = selected.get("metadata") if isinstance(selected, dict) else None
    if not isinstance(meta, dict):
        return
    model_id = meta.get("id")
    if not model_id:
        return
    entry = catalog.setdefault(_slug(str(model_id)), {})
    name = meta.get("name")
    if isinstance(name, str) and name:
        entry["name"] = name
    max_input = meta.get("maxInputTokens")
    if isinstance(max_input, (int, float)):
        entry["maxInputTokens"] = int(max_input)


def _iter_models_json_paths(user_dir: Path) -> list[Path]:
    """Every locally cached copy of Copilot's live model/pricing catalog we can find.

    The Copilot Chat extension fetches this from GitHub and writes it to disk purely for its own
    debug logging, under a randomly-named subdirectory per session, in both global and per-
    workspace storage. It is not a documented API and the path/schema can change.
    """
    paths: list[Path] = []
    global_dir = user_dir / "globalStorage" / "github.copilot-chat" / "debug-logs"
    if global_dir.is_dir():
        paths.extend(global_dir.glob("*/models.json"))
    workspace_storage = user_dir / "workspaceStorage"
    if workspace_storage.is_dir():
        paths.extend(workspace_storage.glob("*/[Gg]it[Hh]ub.copilot-chat/debug-logs/*/models.json"))
    return paths


def load_live_model_pricing(user_dir: Path) -> None:
    """Best-effort refresh of MODEL_PRICING_BY_SLUG/DISPLAY from Copilot's own locally-cached,
    live-fetched model catalog, in preference to the hardcoded table above (which only reflects
    GitHub's published rates as of whenever this file was last edited).

    Silently no-ops if no cache is found or it can't be parsed as expected; this file's
    presence/location/schema is an internal debug artifact of the extension, not a stable format.
    """
    paths = _iter_models_json_paths(user_dir)
    if not paths:
        return
    latest = max(paths, key=lambda p: p.stat().st_mtime)
    try:
        models = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(models, list):
        return
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        billing = model.get("billing") if isinstance(model.get("billing"), dict) else {}
        token_prices = billing.get("token_prices") if isinstance(billing.get("token_prices"), dict) else {}
        default_prices = token_prices.get("default") if isinstance(token_prices.get("default"), dict) else {}
        input_price = default_prices.get("input_price")
        output_price = default_prices.get("output_price")
        cache_read_price = default_prices.get("cache_read_price")
        if not isinstance(input_price, (int, float)) or not isinstance(output_price, (int, float)):
            continue
        if input_price == 0 and output_price == 0:
            continue  # a free/retired/embedding entry, not a priced chat model worth overriding with
        slug = _slug(model_id)
        MODEL_PRICING_BY_SLUG[slug] = {
            "input": input_price / 100,
            "cached_input": (cache_read_price / 100) if isinstance(cache_read_price, (int, float)) else 0.0,
            "output": output_price / 100,
        }
        name = model.get("name")
        if isinstance(name, str) and name:
            MODEL_PRICING_DISPLAY[slug] = name


def lookup_pricing(pricing_key: str | None) -> dict[str, float]:
    if pricing_key:
        rates = MODEL_PRICING_BY_SLUG.get(_slug(str(pricing_key)))
        if rates:
            return rates
    return DEFAULT_UNKNOWN_RATES


def estimate_credits(prompt_tokens: Any, output_tokens: Any, pricing_key: str | None, vendor: str) -> float:
    if vendor and vendor != "copilot":
        return 0.0  # bring-your-own-key model: billed by that provider directly, not GitHub credits
    p = prompt_tokens if isinstance(prompt_tokens, (int, float)) else 0
    o = output_tokens if isinstance(output_tokens, (int, float)) else 0
    rates = lookup_pricing(pricing_key)
    cost_usd = (p * rates["input"] + o * rates["output"]) / 1_000_000
    return cost_usd / CREDIT_USD_RATE


def display_model_name(model_id: str | None, catalog: dict[str, dict[str, Any]]) -> str:
    if not model_id:
        return "—"
    slug = _slug(str(model_id))
    entry = catalog.get(slug)
    if entry and entry.get("name"):
        return str(entry["name"])
    if slug in MODEL_PRICING_DISPLAY:
        return MODEL_PRICING_DISPLAY[slug]
    name = str(model_id)
    if name.startswith("models/"):
        name = name[len("models/"):]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.replace("-", " ").replace("_", " ").title()


def model_context_window(model_id: str | None, catalog: dict[str, dict[str, Any]]) -> int | None:
    if not model_id:
        return None
    entry = catalog.get(_slug(str(model_id)))
    return entry.get("maxInputTokens") if entry else None


def iso_from_epoch_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def extract_session(doc: dict, workspace: str, catalog: dict[str, dict[str, Any]], fallback_id: str) -> dict[str, Any] | None:
    session_id = str(doc.get("sessionId") or fallback_id)
    requests = doc.get("requests")
    requests = requests if isinstance(requests, list) else []

    custom_title = doc.get("customTitle")
    title = custom_title.strip() if isinstance(custom_title, str) and custom_title.strip() else "(untitled)"
    if title == "(untitled)" and not requests:
        return None  # hidden empty: no title, no messages

    credits = 0.0
    input_tokens = 0
    output_tokens = 0
    gen_ms = 0.0
    timestamps: list[float] = []
    completed_ats: list[float] = []
    modes: list[str] = []
    last_model_for_display: str | None = None

    for req in requests:
        if not isinstance(req, dict):
            continue
        timestamp = req.get("timestamp")
        if isinstance(timestamp, (int, float)):
            timestamps.append(timestamp)
        model_id = req.get("modelId") if isinstance(req.get("modelId"), str) else None
        mode_info = req.get("modeInfo")
        if isinstance(mode_info, dict) and isinstance(mode_info.get("kind"), str):
            modes.append(mode_info["kind"])
        result = req.get("result") if isinstance(req.get("result"), dict) else {}
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        prompt_tokens = meta.get("promptTokens")
        out_tokens = meta.get("outputTokens")
        resolved_model = meta.get("resolvedModel") if isinstance(meta.get("resolvedModel"), str) else None
        if isinstance(prompt_tokens, (int, float)):
            input_tokens += prompt_tokens
        if isinstance(out_tokens, (int, float)):
            output_tokens += out_tokens
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        total_elapsed = timings.get("totalElapsed")
        if not isinstance(total_elapsed, (int, float)):
            total_elapsed = req.get("elapsedMs")
        if isinstance(total_elapsed, (int, float)):
            gen_ms += total_elapsed
        model_state = req.get("modelState")
        if isinstance(model_state, dict):
            completed_at = model_state.get("completedAt")
            if isinstance(completed_at, (int, float)):
                completed_ats.append(completed_at)
        vendor = model_id.split("/", 1)[0] if model_id else ""
        if resolved_model:
            pricing_key = resolved_model
            last_model_for_display = resolved_model
        elif model_id:
            pricing_key = model_id.rsplit("/", 1)[-1]
            last_model_for_display = last_model_for_display or model_id
        else:
            pricing_key = None
        credits += estimate_credits(prompt_tokens, out_tokens, pricing_key, vendor)

    created_at = doc.get("creationDate")
    if completed_ats:
        updated_at = max(completed_ats)
    elif timestamps:
        updated_at = max(timestamps)
    else:
        updated_at = created_at

    runtime_ms = None
    if timestamps:
        end = max(completed_ats) if completed_ats else max(timestamps)
        runtime_ms = end - min(timestamps)

    mode = modes[-1] if modes else None
    if not mode:
        input_state = doc.get("inputState")
        selected_mode = input_state.get("mode") if isinstance(input_state, dict) else None
        mode = selected_mode.get("kind") if isinstance(selected_mode, dict) else None

    return {
        "id": session_id,
        "title": title,
        "workspace": workspace,
        "mode": mode or "—",
        "model": display_model_name(last_model_for_display, catalog),
        "messages": len(requests),
        "credits": round(credits, 4),
        "inputTokens": input_tokens or None,
        "outputTokens": output_tokens or None,
        "cachedTokens": None,  # not exposed anywhere in the Chat panel's own logs (see read_copilot_cli_sessions)
        "contextWindow": model_context_window(last_model_for_display, catalog),
        "genMs": gen_ms or None,
        "runtimeMs": runtime_ms,
        "createdAt": iso_from_epoch_ms(created_at),
        "updatedAt": iso_from_epoch_ms(updated_at),
    }


def _cli_iso(value: Any) -> str | None:
    """Normalize the CLI store's `...Z`-suffixed timestamps to the `+00:00` form used elsewhere,
    so datetime.fromisoformat() (used by _sort_key and here) parses them on Python < 3.11 too."""
    if not isinstance(value, str) or not value:
        return None
    return value[:-1] + "+00:00" if value.endswith("Z") else value


def read_copilot_cli_sessions(catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort read of the standalone GitHub Copilot CLI's own session history.

    This is a different product from the Copilot Chat panel sessions above (see
    resolve_copilot_cli_dir()), but its local SQLite store records something the panel's own
    JSONL logs never do: per-request cache_read/cache_write token counts and a ready-made cost
    figure per request (`total_nano_aiu`). 1 credit = 1_000_000_000 nano-AIU here, which lines up
    exactly with this dashboard's existing 1 credit = $0.01 (verified against the model catalog's
    own published per-token rates), so this is used as-is instead of re-estimating it. Schema is
    undocumented and can change between Copilot CLI releases, so every read here is defensive.
    """
    conn = _open_ro_sqlite(resolve_copilot_cli_dir() / "session-store.db")
    if conn is None:
        return []
    try:
        session_rows = conn.execute(
            "SELECT id, cwd, repository, summary, created_at, updated_at FROM sessions"
        ).fetchall()
        turn_rows = conn.execute("SELECT session_id, timestamp FROM turns").fetchall()
        usage_rows = conn.execute(
            "SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens, "
            "duration_ms, total_nano_aiu FROM assistant_usage_events"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    turn_counts: dict[str, int] = {}
    turn_span: dict[str, list[str]] = {}
    for session_id, timestamp in turn_rows:
        turn_counts[session_id] = turn_counts.get(session_id, 0) + 1
        iso = _cli_iso(timestamp)
        if iso:
            span = turn_span.setdefault(session_id, [iso, iso])
            span[0] = min(span[0], iso)
            span[1] = max(span[1], iso)

    usage_by_session: dict[str, dict[str, Any]] = {}
    for session_id, model, in_tok, out_tok, cache_read, duration_ms, nano_aiu in usage_rows:
        agg = usage_by_session.setdefault(
            session_id,
            {"inputTokens": 0, "outputTokens": 0, "cachedTokens": 0, "genMs": 0.0, "credits": 0.0, "models": {}},
        )
        agg["inputTokens"] += in_tok or 0
        agg["outputTokens"] += out_tok or 0
        agg["cachedTokens"] += cache_read or 0
        agg["genMs"] += duration_ms or 0
        agg["credits"] += (nano_aiu or 0) / 1_000_000_000
        if model:
            agg["models"][model] = agg["models"].get(model, 0) + 1

    sessions: list[dict[str, Any]] = []
    for session_id, cwd, repository, summary, created_at, updated_at in session_rows:
        usage = usage_by_session.get(session_id, {})
        message_count = turn_counts.get(session_id, 0)
        title = summary.strip() if isinstance(summary, str) and summary.strip() else "(untitled)"
        if title == "(untitled)" and not message_count:
            continue  # hidden empty, mirrors the classic session logs' own rule

        if isinstance(cwd, str) and cwd:
            workspace = Path(cwd).name or cwd
        elif isinstance(repository, str) and repository:
            workspace = repository
        else:
            workspace = "(no workspace)"

        models = usage.get("models") or {}
        top_model = max(models, key=models.get) if models else None

        span = turn_span.get(session_id)
        runtime_ms = None
        if span:
            try:
                start = datetime.fromisoformat(span[0])
                end = datetime.fromisoformat(span[1])
                runtime_ms = (end - start).total_seconds() * 1000
            except ValueError:
                runtime_ms = None

        sessions.append(
            {
                "id": session_id,
                "title": title,
                "workspace": workspace,
                "mode": "cli",
                "model": display_model_name(top_model, catalog),
                "messages": message_count,
                "credits": round(usage.get("credits", 0.0), 4),
                "inputTokens": usage.get("inputTokens") or None,
                "outputTokens": usage.get("outputTokens") or None,
                "cachedTokens": usage.get("cachedTokens") or None,
                "contextWindow": model_context_window(top_model, catalog),
                "genMs": usage.get("genMs") or None,
                "runtimeMs": runtime_ms,
                "createdAt": _cli_iso(created_at),
                "updatedAt": _cli_iso(updated_at) or _cli_iso(created_at),
            }
        )
    return sessions


def _sort_key(session: dict[str, Any]) -> float:
    value = session.get("updatedAt")
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def read_state(user_dir: Path) -> dict[str, Any]:
    catalog: dict[str, dict[str, Any]] = {}
    parsed: list[tuple[dict, str, str]] = []
    for jsonl_path, workspace in iter_session_files(user_dir):
        doc = replay_session(jsonl_path)
        if doc is None:
            continue
        harvest_model_catalog(doc, catalog)
        parsed.append((doc, workspace, jsonl_path.stem))

    load_live_model_pricing(user_dir)

    sessions: list[dict[str, Any]] = []
    for doc, workspace, fallback_id in parsed:
        session = extract_session(doc, workspace, catalog, fallback_id)
        if session is not None:
            sessions.append(session)

    hidden_empty = len(parsed) - len(sessions)
    sessions.extend(read_copilot_cli_sessions(catalog))

    sessions.sort(key=_sort_key, reverse=True)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "userDir": str(user_dir),
        "hiddenEmpty": hidden_empty,
        "plan": {
            "name": resolve_plan_name(user_dir) or FALLBACK_PLAN_NAME,
            "limit": 0,  # no local cache of a numeric monthly quota exists for any plan tier
            "account": resolve_subscription_account(user_dir),
        },
        "sessions": sessions,
    }


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub Copilot Session Dashboard</title><style>
:root{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:white;background:#000;line-height:1.4}*{box-sizing:border-box}body{margin:0}.shell{max-width:1780px;margin:auto;padding:32px 24px 48px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:28px}h1{font-size:25px;letter-spacing:-.02em;margin:0 0 5px}h2{font-size:16px;margin:0 0 16px}.muted,.meta{color:#9da69e;font-size:13px}.actions{display:flex;gap:9px;align-items:center}button,.input{font:inherit;border:1px solid #d5d9e0;border-radius:7px;background:#343836;padding:8px 11px;color:inherit}button{cursor:pointer}button:hover{background:#454b48}input[type=checkbox]{accent-color:#343836}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}.card,.panel{border-radius:10px;box-shadow:0 1px 2px #00000008}.panel{border:1px solid #5eed83}.card{background:#0f1511;padding:17px}.card-label{text-transform:uppercase;letter-spacing:.07em;color:#9da69e;font-size:11px;font-weight:650}.metric{font-size:23px;font-weight:650;margin:8px 0 3px}.panel{padding:20px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:16px}.controls{display:flex;gap:9px;flex-wrap:wrap}.search{min-width:260px}.table-wrap{overflow:auto;margin:0 -20px}.session-table{width:100%;border-collapse:collapse;min-width:980px}.session-table th,.session-table td{text-align:left;padding:11px 10px;border-top:0.1px solid #2a2c2a;font-size:13px;white-space:nowrap}.session-table th{color:#9da69e;font-weight:600;font-size:12px;cursor:pointer}.session-table th:hover{color:white}.session-table tbody tr:hover{background:#1a201c}.title{max-width:220px;overflow:hidden;text-overflow:ellipsis}.workspace{max-width:150px;overflow:hidden;text-overflow:ellipsis}.share-col{padding-left:6px;padding-right:3px}.session-table th[data-sort="updatedAt"],.session-table td.updated{width:1%}.session-table th[data-sort="messages"],.session-table td.messages{width:1%}.tip{position:fixed;z-index:2000;background:#343836;color:#fff;padding:6px 9px;border-radius:6px;font-size:12px;max-width:420px;white-space:normal;word-break:break-word;pointer-events:none;box-shadow:0 4px 14px #00000033}.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:#5eed83;color:black;font-size:11px}.footer{padding-top:13px}.settings{display:flex;align-items:center;gap:7px;font-size:13px}.error{padding:20px;color:#a12727;background:#fff0f0;border:1px solid #f2c8c8;border-radius:10px;white-space:pre-wrap}.nowrap{white-space:nowrap}.share-btn{border:0;background:none;padding:0;width:26px;height:26px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;color:#9da69e;cursor:pointer}.share-btn:hover{background:#454b48;color:white}.session-table th.no-sort{cursor:default}.share-menu{position:fixed;background:#0f1511;border:1px solid #d5d9e0;border-radius:8px;box-shadow:0 6px 20px #0000001f;z-index:1000;min-width:170px;padding:5px;font-size:13px}.share-menu button{display:block;width:100%;text-align:left;border:0;background:none;border-radius:6px;padding:7px 11px;font:inherit;color:inherit;cursor:pointer}.share-menu button:hover{background:#454b48}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}header,.panel-head{align-items:flex-start;flex-direction:column}}@media(max-width:500px){.shell{padding:22px 14px}.grid{grid-template-columns:1fr}.actions{width:100%}.actions button{flex:1}}
</style></head><body><main class="shell"><header><div><h1>GitHub Copilot Session Dashboard</h1><div class="meta" id="source">Loading local state…</div></div><div class="actions"><span class="meta" id="asof"></span><button id="refresh" title="Refresh" aria-label="Refresh" style="display:inline-flex;align-items:center;justify-content:center"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89"/><polyline points="13.5 1.5 13.5 4.5 10.5 4.5"/></svg></button><label class="settings"><input id="auto" type="checkbox"> Auto-refresh</label></div></header><div id="app"><div class="muted">Loading…</div></div><div id="tip" class="tip" hidden></div></main><script>
const CREDIT_USD_RATE=__CREDIT_USD_RATE__;
const state={data:null,sort:'updatedAt',desc:true};const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=v=>v?new Date(v).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}):'—';const dte=v=>v?new Date(v).toLocaleDateString([], {dateStyle:'medium'}):'—';const dmy=v=>{if(!v)return'—';const d=new Date(v);return`${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')}/${String(d.getFullYear()).slice(-2)}`};const fmt=v=>v==null?'—':Number(v).toLocaleString();const dur=v=>{if(v==null)return'—';const s=Math.round(Number(v)/1000);if(s<60)return`${s}s`;const m=Math.floor(s/60);if(m<60)return`${m}m ${s%60}s`;return`${(s/3600).toFixed(1)}h`};
function render(){closeShareMenu();hideTip();const d=state.data,p=d.plan||{},rate=CREDIT_USD_RATE;const now=new Date(),monthSessions=(d.sessions||[]).filter(s=>{const t=s.createdAt?new Date(s.createdAt):null;return t&&t.getMonth()===now.getMonth()&&t.getFullYear()===now.getFullYear()});const consumed=monthSessions.reduce((a,s)=>a+(Number(s.credits)||0),0),est=(consumed*rate).toFixed(2);const tokIn=monthSessions.reduce((a,s)=>a+(Number(s.inputTokens)||0),0),tokOut=monthSessions.reduce((a,s)=>a+(Number(s.outputTokens)||0),0),modelCounts={},modelTok={},modelCredits={};monthSessions.forEach(s=>{if(s.model&&s.model!=='—'){modelCounts[s.model]=(modelCounts[s.model]||0)+1;modelTok[s.model]=(modelTok[s.model]||0)+(Number(s.inputTokens)||0)+(Number(s.outputTokens)||0);modelCredits[s.model]=(modelCredits[s.model]||0)+(Number(s.credits)||0)}});const top=o=>Object.entries(o).sort((a,b)=>b[1]-a[1])[0],topTok=top(modelTok),topCred=top(modelCredits),topCnt=top(modelCounts);
$('source').textContent=`Local Copilot sessions (Chat panel + CLI) · ${(d.sessions||[]).length} total`;$('asof').textContent=`Last update: ${date(d.generatedAt)}`;let rows=d.sessions||[];const prevSearch=$('search'),raw=prevSearch?.value||'',q=raw.toLowerCase(),focused=prevSearch&&document.activeElement===prevSearch,caret=prevSearch?.selectionStart;if(q)rows=rows.filter(s=>[s.title,s.workspace,s.id,s.model,s.mode].join(' ').toLowerCase().includes(q));rows.sort((a,b)=>{const v=s=>state.sort==='cost'?(s.credits==null?'':s.credits*rate):s[state.sort];let x=v(a)??'',y=v(b)??'';if(state.sort.includes('At')){x=new Date(x||0).getTime();y=new Date(y||0).getTime()}else if(typeof x==='string'){x=x.toLowerCase();y=String(y).toLowerCase()}return (x<y?-1:x>y?1:0)*(state.desc?-1:1)});
$('app').innerHTML=`<section class="grid"><article class="card"><div class="card-label">Plan</div><div class="metric">${esc(p.name)}</div>${p.account?`<div class="muted">@${esc(p.account)}</div>`:''}<div class="card-label" style="margin-top:14px">Estimated cost (this month)</div><div class="metric">$${est}</div><div class="muted">$${rate.toFixed(2)} / credit</div></article><article class="card"><div class="card-label">Credits consumed</div><div class="metric">${consumed.toFixed(2)}</div><div class="muted">Unlimited plan allotment</div><div class="muted">Estimated locally from logged token usage</div></article><article class="card"><div class="card-label">Sessions this month</div><div class="metric">${fmt(monthSessions.length)}</div><div class="muted">Tokens:</div><div class="muted">IN ${fmt(tokIn)} · OUT ${fmt(tokOut)}</div></article><article class="card"><div class="card-label">Model metrics</div><div class="metric">${fmt(Object.keys(modelCounts).length)}</div><div class="muted">Top by tokens: ${topTok?esc(topTok[0])+' · '+fmt(topTok[1]):'—'}</div><div class="muted">Top by credits: ${topCred?esc(topCred[0])+' · '+topCred[1].toFixed(2):'—'}</div><div class="muted">Top by count: ${topCnt?esc(topCnt[0])+' · '+fmt(topCnt[1]):'—'}</div></article></section><section class="panel"><div class="panel-head"><div><h2>Sessions <span class="muted">(${rows.length!==d.sessions.length?`${rows.length} of `:''}${d.sessions.length} Total)</span></h2></div><div class="controls"><input class="input search" id="search" placeholder="Search title, workspace, model…" value="${esc(raw)}"></div></div><div class="table-wrap"><table class="session-table"><thead><tr><th class="no-sort share-col"></th>${[['updatedAt','Last updated'],['workspace','Workspace'],['title','Title'],['model','Model'],['mode','Mode'],['credits','Credits'],['cost','Est. cost'],['inputTokens','Tokens in'],['outputTokens','Tokens out'],['cachedTokens','Cached'],['contextWindow','Context'],['genMs','Gen. time'],['runtimeMs','Runtime'],['createdAt','Created'],['messages',MESSAGE_ICON_SVG]].map(([k,l])=>`<th data-sort="${k}">${l}${state.sort===k?' '+(state.desc?'↓':'↑'):''}</th>`).join('')}</tr></thead><tbody>${rows.map(s=>`<tr><td class="nowrap share-col"><button class="share-btn" data-id="${esc(s.id)}" title="Share" aria-label="Share session">${SHARE_ICON_SVG}</button></td><td class="nowrap updated">${date(s.updatedAt)}</td><td class="workspace">${esc(s.workspace||'—')}</td><td class="title">${esc(s.title)}</td><td>${esc(s.model||'—')}</td><td class="nowrap"><span class="badge">${esc(s.mode||'—')}</span></td><td class="nowrap">${s.credits==null?'—':s.credits.toFixed(3)}</td><td class="nowrap">${s.credits==null?'—':'$'+(s.credits*rate).toFixed(2)}</td><td class="nowrap">${fmt(s.inputTokens)}</td><td class="nowrap">${fmt(s.outputTokens)}</td><td class="nowrap">${fmt(s.cachedTokens)}</td><td class="nowrap">${fmt(s.contextWindow)}</td><td class="nowrap">${dur(s.genMs)}</td><td class="nowrap">${dur(s.runtimeMs)}</td><td class="nowrap">${dmy(s.createdAt)}</td><td class="messages">${s.messages}</td></tr>`).join('')||'<tr><td colspan="16" class="muted">No sessions match this search.</td></tr>'}</tbody></table></div><div class="footer muted">${d.sessions.length} sessions loaded from local VS Code Copilot Chat logs and the Copilot CLI's own session history.${d.hiddenEmpty?' '+d.hiddenEmpty+' empty sessions (no title, no messages) hidden.':''} Chat panel credits are estimated locally from logged token usage using GitHub's published per-model rates; CLI sessions (mode "cli") use the CLI's own already-computed cost. Neither is the authoritative total from your GitHub account.</div></section>`;
$('search').addEventListener('input',render);document.querySelectorAll('[data-sort]').forEach(el=>el.addEventListener('click',()=>{if(state.sort===el.dataset.sort)state.desc=!state.desc;else{state.sort=el.dataset.sort;state.desc=true}render()}));const search=$('search');if(search&&focused){search.focus();search.setSelectionRange(caret??raw.length,caret??raw.length)}}
const SHARE_ICON_SVG='<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.6v7.9"/><polyline points="4.9 4.8 8 1.6 11.1 4.8"/><path d="M4.6 6.6H3.6a1 1 0 0 0-1 1v5.8a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1V7.6a1 1 0 0 0-1-1h-1"/></svg>';
const MESSAGE_ICON_SVG='<span title="Messages" aria-label="Messages" style="display:inline-flex;vertical-align:middle"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 10.5a1.5 1.5 0 0 1-1.5 1.5H5l-3 2.5V3.5A1.5 1.5 0 0 1 3.5 2h9A1.5 1.5 0 0 1 14 3.5z"/></svg></span>';
let shareMenu=null;
function closeShareMenu(){if(!shareMenu)return;const m=shareMenu;shareMenu=null;m.el.remove();document.removeEventListener('click',m.onDoc,true);document.removeEventListener('keydown',m.onKey)}
function shareFileDate(s){const v=s.createdAt||s.updatedAt;const d=v?new Date(v):new Date();if(isNaN(d))return new Date().toISOString().slice(0,10);const p=n=>String(n).padStart(2,'0');return`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`}
function shareSanitize(v){return String(v||'').replace(/[\\/:*?"<>|\x00-\x1f]/g,'-').replace(/\s+/g,' ').replace(/^[.\s]+/,'').replace(/[.\s]+$/,'').slice(0,80).trim()}
function shareFilename(s){let base=shareSanitize(s.title);if(!base||base==='(untitled)')base=shareSanitize(s.id)||'session';return`${base} ${shareFileDate(s)}.md`}
function estCost(s){return s.credits==null?'—':'$'+(s.credits*CREDIT_USD_RATE).toFixed(2)}
function sessionMarkdown(s){const pipe=v=>String(v??'').replace(/\|/g,'\\|');const val=v=>pipe(v==null||v===''?'—':v);return['# '+pipe(s.title||'(untitled)'),'','| Field | Value |','| --- | --- |','| Session ID | '+val(s.id)+' |','| Workspace | '+val(s.workspace)+' |','| Mode | '+val(s.mode)+' |','| Model | '+val(s.model)+' |','| Messages | '+val(s.messages)+' |','| Credits consumed | '+(s.credits==null?'—':s.credits.toFixed(3))+' |','| Est. cost | '+estCost(s)+' |','| Tokens in | '+fmt(s.inputTokens)+' |','| Tokens out | '+fmt(s.outputTokens)+' |','| Tokens cached | '+fmt(s.cachedTokens)+' |','| Context window | '+fmt(s.contextWindow)+' |','| Gen. time | '+dur(s.genMs)+' |','| Runtime | '+dur(s.runtimeMs)+' |','| Created | '+date(s.createdAt)+' |','| Last updated | '+date(s.updatedAt)+' |','','Estimated at $'+CREDIT_USD_RATE.toFixed(2)+' / credit from locally logged token usage.'].join('\n')}
async function copySessionJson(s){const text=JSON.stringify({...s,creditUsdRate:CREDIT_USD_RATE,estimatedCostUsd:s.credits==null?null:Number((s.credits*CREDIT_USD_RATE).toFixed(2))},null,2);try{await navigator.clipboard.writeText(text);return}catch(e){}const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{document.execCommand('copy')}catch(e){}ta.remove()}
function downloadSessionMarkdown(s){const blob=new Blob([sessionMarkdown(s)],{type:'text/markdown'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=shareFilename(s);document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url)}
function openShareMenu(btn,s){closeShareMenu();const el=document.createElement('div');el.className='share-menu';const copy=document.createElement('button');copy.type='button';copy.textContent='Copy JSON';const dl=document.createElement('button');dl.type='button';dl.textContent='Download .md file';el.append(copy,dl);document.body.appendChild(el);const r=btn.getBoundingClientRect(),w=el.offsetWidth,h=el.offsetHeight;let top=r.bottom+6,left=r.right-w;if(top+h>window.innerHeight-8&&r.top-h-6>=8)top=r.top-h-6;left=Math.max(8,Math.min(left,window.innerWidth-w-8));el.style.top=top+'px';el.style.left=left+'px';const onDoc=e=>{if(!el.contains(e.target)&&e.target!==btn)closeShareMenu()};const onKey=e=>{if(e.key==='Escape')closeShareMenu()};document.addEventListener('click',onDoc,true);document.addEventListener('keydown',onKey);shareMenu={el,onDoc,onKey};copy.addEventListener('click',async()=>{await copySessionJson(s);copy.textContent='Copied!';setTimeout(closeShareMenu,1000)});dl.addEventListener('click',()=>{downloadSessionMarkdown(s);closeShareMenu()})}
document.addEventListener('click',e=>{const btn=e.target.closest&&e.target.closest('.share-btn');if(!btn)return;const s=(state.data&&state.data.sessions||[]).find(x=>x.id===btn.dataset.id);if(s)openShareMenu(btn,s)});
function hideTip(){const t=$('tip');if(t)t.hidden=true}
function showTip(cell){const t=$('tip');if(!t||cell.scrollWidth<=cell.clientWidth+1)return;t.textContent=cell.textContent;t.hidden=false;const r=cell.getBoundingClientRect();t.style.left=Math.max(8,Math.min(r.left,window.innerWidth-t.offsetWidth-8))+'px';let top=r.top-t.offsetHeight-6;if(top<8)top=r.bottom+6;t.style.top=top+'px'}
document.addEventListener('mouseover',e=>{const c=e.target.closest&&e.target.closest('.title,.workspace');if(c)showTip(c)});
document.addEventListener('mouseout',e=>{const c=e.target.closest&&e.target.closest('.title,.workspace');if(c)hideTip()});
window.addEventListener('scroll',hideTip,true);

async function load(){try{const r=await fetch('/api/data',{cache:'no-store'});if(!r.ok)throw new Error(await r.text());state.data=await r.json();render()}catch(e){$('app').innerHTML=`<div class="error">Unable to load dashboard data.\n\n${esc(e.message)}</div>`}}$('refresh').addEventListener('click',load);$('auto').addEventListener('change',e=>{if(e.target.checked)window.refreshTimer=setInterval(load,60000);else clearInterval(window.refreshTimer)});load();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    user_dir: Path

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path == "/index.html":
            html = HTML.replace("__CREDIT_USD_RATE__", str(CREDIT_USD_RATE))
            self.send_text(html, "text/html; charset=utf-8")
            return
        if self.path == "/api/data":
            try:
                payload = json.dumps(read_state(self.user_dir), ensure_ascii=False).encode()
            except Exception as exc:  # surface a safe local diagnostic to the UI
                payload = json.dumps({"error": str(exc)}).encode()
                self.send_bytes(payload, "application/json", 500)
                return
            self.send_bytes(payload, "application/json")
            return
        self.send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def send_text(self, text: str, content_type: str) -> None:
        self.send_bytes(text.encode("utf-8"), content_type)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local GitHub Copilot session and credits dashboard.")
    parser.add_argument("--vscode-user-dir", help="Path to VS Code's User directory (auto-detected by default)")
    parser.add_argument("--host", default="localhost", help="Bind address (default: localhost)")
    parser.add_argument("--port", type=int, default=8788, help="Starting port (default: 8788)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically")
    args = parser.parse_args()
    try:
        user_dir = resolve_vscode_user_dir(args.vscode_user_dir)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    Handler.user_dir = user_dir
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"Could not bind a port from {args.port} to {args.port + 19}.", file=sys.stderr)
        return 1
    url = f"http://{args.host}:{server.server_port}/"
    print(f"{APP_TITLE} running at {url}")
    print(f"Reading: {user_dir}")
    if not args.no_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
