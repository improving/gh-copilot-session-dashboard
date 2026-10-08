#!/usr/bin/env python3
"""Minimal local dashboard for GitHub Copilot Chat's cached VS Code session logs."""

from __future__ import annotations

import argparse
import json
import math
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

# Dashboard theme colors — tweak these to restyle the UI.
THEME = {
    "bg": "#0e1116",              # page background
    "fg": "white",             # primary text
    "muted": "#9198a1",        # secondary text, labels, icons
    "accent": "#238636",       # panel border & badge highlight
    "card-bg": "#161b22",      # cards and popup menus
    "control-bg": "#22282f",   # buttons, inputs, tooltip
    "control-hover": "#202329",
    "border": "#d5d9e0",       # button/menu outlines
    "table-border": "#2a2c2a", # row separators
    "row-hover": "#202329",
    "badge-fg": "white",       # text on accent badges
    "mode-cli": "#7a22e5",     # Mode badge: Copilot CLI (console)
    "mode-autopilot": "#3281e7", # Mode badge: Autopilot
    "mode-agents": "#3281e7",  # Mode badge: VS Code Agents window
    "mode-chat": "#238636",    # Mode badge: VS Code Chat panel (agent mode)
    "card-shadow": "#00000008",
    "popup-shadow": "#00000033",
    "menu-shadow": "#0000001f",
    "error-fg": "#a12727",
    "error-bg": "#fff0f0",
    "error-border": "#f2c8c8",
}

THEME_VARS_CSS = ";".join(f"--{name}:{value}" for name, value in THEME.items())

def _slug(value: str) -> str:
    return value.strip().lower()


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
        data = data if isinstance(data, dict) else {}
        for key in ("folder", "workspace"):
            value = data.get(key)
            if isinstance(value, str) and value:
                path = Path(unquote(value.split("://", 1)[-1]))
                name = path.stem if key == "workspace" else path.name
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


def _valid_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value)


def _valid_rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    try:
        rate = float(value)
    except (OverflowError, ValueError):
        return None
    return rate if math.isfinite(rate) else None


def harvest_model_catalog(doc: dict, catalog: dict[str, dict[str, Any]]) -> None:
    input_state = doc.get("inputState")
    selected = input_state.get("selectedModel") if isinstance(input_state, dict) else None
    meta = selected.get("metadata") if isinstance(selected, dict) else None
    if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
        return
    entry = catalog.setdefault(_slug(meta["id"]), {})
    if isinstance(meta.get("name"), str) and meta["name"]:
        entry["name"] = meta["name"]
    for key in ("maxInputTokens", "contextWindow", "contextSize"):
        count = _valid_count(meta.get(key))
        if count:
            entry["selectedContextTokens"] = count
            break
    prices = meta.get("billing") or meta.get("token_prices") or meta.get("tokenPrices")
    if isinstance(prices, dict):
        token_prices = prices.get("token_prices") if isinstance(prices.get("token_prices"), dict) else prices
        normalized = _normalize_token_prices(token_prices, prices.get("batch_size"))
        if not normalized:
            tier = token_prices.get("default") if isinstance(token_prices.get("default"), dict) else token_prices
            direct = {}
            for target, keys in {"input": ("inputPrice", "input_price"), "output": ("outputPrice", "output_price"),
                                 "cacheRead": ("cacheReadPrice", "cache_read_price", "cache_price"),
                                 "cacheWrite": ("cacheWritePrice", "cache_write_price")}.items():
                value = next((_valid_rate(tier.get(key)) for key in keys if _valid_rate(tier.get(key)) is not None), None)
                if value is not None:
                    direct[target] = value
            if "input" in direct and "output" in direct:
                normalized = {"default": direct}
        if normalized:
            entry["pickerPricing"] = normalized


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


def _normalize_price_tier(tier: dict[str, Any]) -> dict[str, float] | None:
    batch_size = _valid_count(tier.get("batch_size"))
    if not batch_size:
        return None
    normalized: dict[str, float] = {}
    for key, aliases in {
        "input": ("input_price",),
        "output": ("output_price",),
        "cacheRead": ("cache_read_price", "cache_price"),
        "cacheWrite": ("cache_write_price",),
    }.items():
        raw = next((tier.get(alias) for alias in aliases if tier.get(alias) is not None), None)
        rate = _valid_rate(raw)
        if rate is not None:
            normalized[key] = rate * 1_000_000 / batch_size
    threshold = next((_valid_count(tier.get(key)) for key in ("threshold_tokens", "threshold", "max_prompt_tokens")
                      if _valid_count(tier.get(key)) is not None), None)
    if threshold is not None:
        normalized["thresholdTokens"] = threshold
    return normalized if "input" in normalized and "output" in normalized else None


def _normalize_token_prices(token_prices: dict[str, Any], inherited_batch_size: Any = None) -> dict[str, Any] | None:
    batch_size = _valid_count(token_prices.get("batch_size", inherited_batch_size))
    default = token_prices.get("default")
    default = dict(default) if isinstance(default, dict) else dict(token_prices)
    if batch_size is not None:
        default.setdefault("batch_size", batch_size)
    default_rates = _normalize_price_tier(default)
    if not default_rates:
        return None
    result: dict[str, Any] = {"default": default_rates}
    long_context = token_prices.get("long_context")
    if isinstance(long_context, dict):
        long_context = dict(long_context)
        if batch_size is not None:
            long_context.setdefault("batch_size", batch_size)
        long_rates = _normalize_price_tier(long_context)
        if long_rates:
            result["longContext"] = long_rates
    threshold = next((_valid_count(token_prices.get(key)) for key in ("long_context_threshold", "threshold_tokens")
                      if _valid_count(token_prices.get(key)) is not None), None)
    if threshold is not None and "longContext" in result:
        result["longContext"].setdefault("thresholdTokens", threshold)
    return result


def load_live_model_pricing(user_dir: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for path in _iter_models_json_paths(user_dir):
        try:
            captured = path.stat().st_mtime
            models = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(models, list):
            continue
        entries: dict[str, dict[str, Any]] = {}
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                continue
            model_id = model["id"]
            billing = model.get("billing") if isinstance(model.get("billing"), dict) else {}
            token_prices = billing.get("token_prices")
            prices = _normalize_token_prices(token_prices, billing.get("batch_size")) if isinstance(token_prices, dict) else None
            long_threshold = _valid_count(model.get("long_context_threshold"))
            if prices and prices.get("longContext") and long_threshold is not None:
                prices["longContext"].setdefault("thresholdTokens", long_threshold)
            item: dict[str, Any] = {"id": model_id}
            if isinstance(model.get("name"), str):
                item["name"] = model["name"]
            if prices:
                item["prices"] = prices
            for key in ("max_prompt_tokens", "context_max"):
                count = _valid_count(model.get(key))
                if count:
                    item["maxInputTokens"] = count
                    break
            capabilities = model.get("capabilities") if isinstance(model.get("capabilities"), dict) else {}
            limits = capabilities.get("limits") if isinstance(capabilities.get("limits"), dict) else {}
            prompt_limit = _valid_count(limits.get("max_prompt_tokens"))
            if prompt_limit:
                item["promptLimit"] = prompt_limit
            entries[_slug(model_id)] = item
        if entries:
            snapshots.append({"path": str(path), "capturedAt": captured, "models": entries})
    return sorted(snapshots, key=lambda item: item["capturedAt"])


def lookup_pricing(pricing_key: str | None, snapshots: list[dict[str, Any]], timestamp: float | None,
                   picker_catalog: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    if not pricing_key:
        return None, None
    keys = [_slug(pricing_key)]
    if pricing_key.startswith("copilot/"):
        keys.append(_slug(pricing_key.split("/", 1)[1]))
    eligible = [item for item in snapshots if timestamp is None or item["capturedAt"] <= timestamp]
    for snapshot in reversed(eligible):
        for key in keys:
            model = snapshot["models"].get(key)
            if model and model.get("prices"):
                if timestamp is None:
                    source = f"local catalog ({Path(snapshot['path']).name}; request timestamp unavailable)"
                else:
                    age = max(0, int(timestamp - snapshot["capturedAt"]))
                    source = f"local catalog ({Path(snapshot['path']).name}; {age // 86400}d old)"
                return model["prices"], source
    entry = next((picker_catalog.get(key) for key in keys if picker_catalog.get(key, {}).get("pickerPricing")), None)
    if entry:
        return entry["pickerPricing"], "selected-model picker snapshot (historical price unverified)"
    return None, None


def display_model_name(model_id: str | None, catalog: dict[str, dict[str, Any]]) -> str:
    if not model_id:
        return "—"
    slug = _slug(str(model_id))
    entry = catalog.get(slug)
    if entry is None and str(model_id).startswith("copilot/"):
        entry = catalog.get(_slug(str(model_id).split("/", 1)[1]))
    if entry and entry.get("name"):
        return str(entry["name"])
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
    if entry is None and str(model_id).startswith("copilot/"):
        entry = catalog.get(_slug(str(model_id).split("/", 1)[1]))
    return entry.get("maxInputTokens") if entry else None


def iso_from_epoch_ms(value: Any) -> str | None:
    value = _valid_rate(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _reported_value(objects: list[dict[str, Any]], key: str) -> Any:
    for obj in objects:
        if key in obj:
            return obj[key]
    return None


def _response_totals(req: dict[str, Any], result: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = next((obj.get("modelTotals") for obj in (result, meta, req) if isinstance(obj.get("modelTotals"), (dict, list))), None)
    if raw is None:
        return None
    entries = raw if isinstance(raw, list) else list(raw.values()) if all(isinstance(v, dict) for v in raw.values()) else [raw]
    totals: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        input_count = _valid_count(item.get("inputTokens", item.get("input")))
        cached_count = _valid_count(item.get("cachedInputTokens", item.get("cachedTokens", item.get("cacheReadTokens"))))
        output_count = _valid_count(item.get("outputTokens", item.get("output")))
        model = item.get("modelId", item.get("model"))
        if input_count is not None or output_count is not None or cached_count is not None:
            totals.append({"model": model if isinstance(model, str) else None, "input": input_count,
                           "cached": cached_count, "output": output_count})
    return totals


def _context_usage(req: dict[str, Any], result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any] | None:
    objects = [result, meta, req]
    context = next((obj.get("contextUsage") for obj in objects if isinstance(obj.get("contextUsage"), dict)), None)
    if isinstance(context, dict):
        current = _valid_count(context.get("currentTokens"))
        limit = _valid_count(context.get("tokenLimit"))
        if current is not None:
            return {"currentTokens": current, "tokenLimit": limit or None,
                    "percent": round(current * 100 / limit, 1) if limit else None, "source": "persisted context usage"}
    response = result.get("response") if isinstance(result.get("response"), dict) else {}
    response_objects = [result, response, req]
    prompt = next((_valid_count(obj.get("promptTokens")) for obj in response_objects
                   if _valid_count(obj.get("promptTokens")) is not None), None)
    completion = next((_valid_count(obj.get("completionTokens")) for obj in response_objects
                       if _valid_count(obj.get("completionTokens")) is not None), None)
    if prompt is None or completion is None:
        return None
    return {"currentTokens": prompt + completion, "tokenLimit": None, "percent": None,
            "source": "persisted response token counts"}


def _estimate_request_credits(input_tokens: int | None, output_tokens: int | None, model_id: str | None,
                              request_time: float | None, snapshots: list[dict[str, Any]],
                              catalog: dict[str, dict[str, Any]], vendor: str) -> tuple[float | None, str, str | None]:
    if vendor and vendor != "copilot":
        return None, "byok", None
    if not model_id or input_tokens is None or output_tokens is None:
        return None, "unavailable", None
    prices, source = lookup_pricing(model_id, snapshots, request_time, catalog)
    if not prices:
        return None, "unavailable", None
    rates = prices.get("default", prices)
    assumptions = []
    if source and "historical price unverified" in source:
        assumptions.append("historical picker price unverified")
    if source and "request timestamp unavailable" in source:
        assumptions.append("request timestamp unavailable")
    long_rates = prices.get("longContext")
    if isinstance(long_rates, dict):
        threshold = _valid_count(long_rates.get("thresholdTokens"))
        if threshold is not None and input_tokens > threshold:
            rates = long_rates
            source = (source or "catalog") + " (long-context tier)"
        elif threshold is None:
            assumptions.append("long-context threshold unknown")
    in_rate, out_rate = _valid_rate(rates.get("input")), _valid_rate(rates.get("output"))
    if in_rate is None or out_rate is None:
        return None, "unavailable", source
    credits = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    if any(key in tier for tier in (prices.get("default", prices), long_rates or {})
           for key in ("cacheRead", "cacheWrite")):
        assumptions.append("cache split unavailable")
    quality = "estimated-partial" if assumptions else "estimated"
    return credits, quality, source + ("; " + ", ".join(assumptions) if assumptions else "")


def extract_session(doc: dict, workspace: str, catalog: dict[str, dict[str, Any]], fallback_id: str,
                    snapshots: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    session_id = str(doc.get("sessionId") or fallback_id)
    requests = doc.get("requests") if isinstance(doc.get("requests"), list) else []
    requests = [req for req in requests if isinstance(req, dict)]
    custom_title = doc.get("customTitle")
    title = custom_title.strip() if isinstance(custom_title, str) and custom_title.strip() else "(untitled)"
    if title == "(untitled)" and not requests:
        return None

    snapshots = snapshots or []
    picker_catalog: dict[str, dict[str, Any]] = {}
    harvest_model_catalog(doc, picker_catalog)
    totals = {"input": 0, "output": 0, "cached": 0}
    seen = {"input": 0, "output": 0, "cached": 0}
    complete_model_totals = bool(requests)
    input_complete = output_complete = cached_complete = bool(requests)
    credits_reported = 0.0
    credits_estimated = 0.0
    billable_calls = byok_calls = 0
    any_estimate = False
    any_partial = False
    all_costed = True
    session_credit_reports: list[float] = []
    call_rows: list[dict[str, Any]] = []
    gen_ms = 0.0
    gen_count = 0
    timestamps: list[float] = []
    completed_ats: list[float] = []
    modes: list[str] = []
    latest_context: tuple[float, dict[str, Any]] | None = None
    last_model: str | None = None

    for req in requests:
        timestamp = _valid_rate(req.get("timestamp"))
        if timestamp is not None:
            timestamps.append(timestamp)
        mode_info = req.get("modeInfo")
        if isinstance(mode_info, dict) and isinstance(mode_info.get("kind"), str):
            modes.append(mode_info["kind"])
        result = req.get("result") if isinstance(req.get("result"), dict) else {}
        meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        model_id = req.get("modelId") if isinstance(req.get("modelId"), str) else None
        resolved = meta.get("resolvedModel") if isinstance(meta.get("resolvedModel"), str) else None
        pricing_model = resolved or (model_id.split("/", 1)[-1] if model_id else None)
        display_model = resolved or model_id
        if display_model:
            last_model = display_model
        model_totals = _response_totals(req, result, meta)
        if model_totals is None:
            complete_model_totals = False
            in_count = _valid_count(meta.get("promptTokens"))
            out_count = _valid_count(meta.get("outputTokens"))
            per_models = [{"model": display_model, "input": in_count, "cached": None, "output": out_count}]
        else:
            per_models = model_totals
        for item in per_models:
            if item["cached"] is not None and (item["input"] is None or item["cached"] > item["input"]):
                item["cached"] = None
        req_input = req_output = 0
        req_input_seen = req_output_seen = False
        req_cached = 0
        req_cached_seen = True
        for item in per_models:
            for total_key, value_key in (("input", "input"), ("output", "output"), ("cached", "cached")):
                value = item[value_key]
                if value is None:
                    if value_key == "cached":
                        req_cached_seen = False
                    continue
                totals[total_key] += value
                seen[total_key] += 1
                if value_key == "input":
                    req_input += value
                    req_input_seen = True
                elif value_key == "output":
                    req_output += value
                    req_output_seen = True
                else:
                    req_cached += value
        req_cached_seen = req_cached_seen and bool(per_models)
        if not per_models or any(item["input"] is None for item in per_models):
            input_complete = False
        if not per_models or any(item["output"] is None for item in per_models):
            output_complete = False
        if not per_models or any(item["cached"] is None for item in per_models):
            cached_complete = False
        billed_objects = [result, meta, req]
        reported = _valid_rate(_reported_value(billed_objects, "copilotCredits"))
        session_report = _valid_rate(_reported_value(billed_objects, "sessionCopilotCredits"))
        if session_report is not None:
            session_credit_reports.append(session_report)
        vendor = model_id.split("/", 1)[0] if model_id and "/" in model_id else ("copilot" if model_id else "")
        if vendor and vendor != "copilot" and reported is None:
            byok_calls += 1
        else:
            billable_calls += 1
        if reported is not None:
            call_credits, cost_quality, price_source = reported, "reported", "VS Code reported per-turn credits"
            credits_reported += reported
        else:
            call_credits, cost_quality, price_source = _estimate_request_credits(
                req_input if req_input_seen else None, req_output if req_output_seen else None,
                pricing_model, timestamp / 1000 if timestamp is not None else None, snapshots, picker_catalog, vendor)
            if cost_quality.startswith("estimated") and call_credits is not None:
                credits_estimated += call_credits
                any_estimate = True
                any_partial = any_partial or cost_quality == "estimated-partial"
            elif cost_quality != "byok":
                all_costed = False
        breakdown = [{"model": display_model_name(item.get("model") or display_model, catalog),
                      "inputTokens": item["input"], "outputTokens": item["output"]} for item in per_models]
        approximate = bool(model_totals and len(model_totals) > 1 and any(not item.get("model") for item in model_totals))
        call_rows.append({"timestamp": iso_from_epoch_ms(timestamp),
                          "model": display_model_name(display_model, catalog),
                          "inputTokens": req_input if req_input_seen else None,
                          "outputTokens": req_output if req_output_seen else None,
                          "cachedTokens": req_cached if req_cached_seen else None,
                          "credits": call_credits, "creditsQuality": cost_quality, "priceSource": price_source,
                          "modelAttribution": "approximate" if approximate else "observed", "modelBreakdown": breakdown})
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        elapsed = _valid_rate(timings.get("totalElapsed"))
        if elapsed is None:
            elapsed = _valid_rate(req.get("elapsedMs"))
        if elapsed is not None:
            gen_ms += elapsed
            gen_count += 1
        model_state = req.get("modelState")
        if isinstance(model_state, dict):
            completed = _valid_rate(model_state.get("completedAt"))
            if completed is not None:
                completed_ats.append(completed)
        context = _context_usage(req, result, meta)
        if context:
            latest_context = (timestamp or 0, context)

    document_credit = _valid_rate(doc.get("sessionCopilotCredits"))
    if document_credit is not None:
        session_credit_reports.append(document_credit)
    all_session_reports = max(session_credit_reports) if session_credit_reports else None
    turn_cost = credits_reported if not any_estimate else None
    if all_session_reports is not None:
        credits = max(credits_reported, all_session_reports)
        credits_quality = "reported/reconciled"
    elif not billable_calls and byok_calls:
        credits, credits_quality = None, "byok"
    elif turn_cost is not None and all_costed:
        credits, credits_quality = turn_cost, "reported"
    elif any_estimate:
        credits = credits_reported + credits_estimated
        credits_quality = "partial" if any_partial or not all_costed or credits_reported else "estimated"
    elif credits_reported:
        credits, credits_quality = credits_reported, "partial"
    elif not requests:
        credits, credits_quality = None, "unavailable"
    else:
        credits, credits_quality = None, "unavailable"

    if all_session_reports is not None:
        credits_source = "VS Code sessionCopilotCredits reconciled with turn credits"
    elif credits_quality == "reported":
        credits_source = "VS Code per-turn copilotCredits"
    elif credits_quality in ("estimated", "partial"):
        credits_source = "Per-turn credits and local catalog estimates"
    elif credits_quality == "byok":
        credits_source = "Bring-your-own-key calls; no GitHub Copilot charge"
    else:
        credits_source = "No reported credits or matched local rate"
    created_at = doc.get("creationDate")
    updated_at = max(completed_ats) if completed_ats else max(timestamps, default=created_at)
    runtime_ms = max(completed_ats or timestamps) - min(timestamps) if timestamps else None
    if runtime_ms is not None and runtime_ms < 0:
        runtime_ms = None
    mode = modes[-1] if modes else None
    if not mode:
        input_state = doc.get("inputState")
        selected_mode = input_state.get("mode") if isinstance(input_state, dict) else None
        mode = selected_mode.get("kind") if isinstance(selected_mode, dict) else None
    token_quality = "exact" if complete_model_totals and input_complete and output_complete else "partial"
    cached = totals["cached"] if cached_complete else None
    context = latest_context[1] if latest_context else None
    if context and not context.get("tokenLimit"):
        context_keys = [_slug(last_model)] if last_model else []
        if last_model and last_model.startswith("copilot/"):
            context_keys.append(_slug(last_model.split("/", 1)[1]))
        selected_entry = next((picker_catalog[key] for key in context_keys if key in picker_catalog), {})
        limit = _valid_count(selected_entry.get("selectedContextTokens")) or model_context_window(last_model, catalog)
        if limit:
            context["tokenLimit"] = limit
            context["percent"] = round(context["currentTokens"] * 100 / limit, 1)
            context["source"] += "; configured model context limit"
    if title == "(untitled)" and credits is None and not seen["input"] and not seen["output"] and not context:
        return None
    return {
        "id": session_id, "title": title, "workspace": workspace, "mode": "chat" if mode == "agent" else mode or "—",
        "model": display_model_name(last_model, catalog), "messages": len(requests),
        "credits": round(credits, 6) if credits is not None else None, "creditsQuality": credits_quality,
        "creditsSource": credits_source,
        "inputTokens": totals["input"] if seen["input"] else None, "outputTokens": totals["output"] if seen["output"] else None,
        "cachedTokens": cached, "cacheWriteTokens": None, "tokenQuality": token_quality, "contextUsage": context,
        "contextWindow": context.get("tokenLimit") if context else None,
        "currentContextTokens": context.get("currentTokens") if context else None,
        "genMs": gen_ms if gen_count else None, "runtimeMs": runtime_ms, "createdAt": iso_from_epoch_ms(created_at),
        "updatedAt": iso_from_epoch_ms(updated_at), "activity": call_rows,
        "untimestampedCalls": sum(1 for call in call_rows if call["timestamp"] is None),
        "priceSources": sorted({call["priceSource"] for call in call_rows if call.get("priceSource")}),
    }


def _cli_iso(value: Any) -> str | None:
    """Normalize the CLI store's `...Z`-suffixed timestamps to the `+00:00` form used elsewhere,
    so datetime.fromisoformat() (used by _sort_key and here) parses them on Python < 3.11 too."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return normalized


def cli_session_mode(session_id: str) -> str:
    """Classify a Copilot CLI-family session from `client_name` in ~/.copilot/session-state/<id>/workspace.yaml."""
    try:
        text = (resolve_copilot_cli_dir() / "session-state" / session_id / "workspace.yaml").read_text(encoding="utf-8")
    except OSError:
        return "cli"
    match = re.search(r"^client_name:\s*(\S+)", text, re.M)
    client = match.group(1).strip("'\"").lower() if match else ""
    if "autopilot" in client:
        return "autopilot"
    if client.startswith("vscode"):
        return "agents"
    return "cli"


def read_copilot_cli_sessions(catalog: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    conn = _open_ro_sqlite(resolve_copilot_cli_dir() / "session-store.db")
    if conn is None:
        return []
    try:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"sessions", "assistant_usage_events"}.issubset(tables):
            return []
        def select_known(table: str, candidates: tuple[str, ...]) -> list[dict[str, Any]]:
            available = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            columns = [column for column in candidates if column in available]
            if not columns:
                return []
            return [dict(row) for row in conn.execute(f"SELECT {', '.join(columns)} FROM {table}")]
        sessions = select_known("sessions", ("id", "cwd", "repository", "summary", "created_at", "updated_at"))
        usage = select_known("assistant_usage_events", ("session_id", "model", "input_tokens", "output_tokens",
                         "cache_read_tokens", "cache_write_tokens", "duration_ms", "total_nano_aiu", "created_at"))
        turns = select_known("turns", ("session_id", "timestamp")) if "turns" in tables else []
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    turn_counts: dict[str, int] = {}
    turn_times: dict[str, list[str]] = {}
    for row in turns:
        session_id = row.get("session_id")
        if not session_id:
            continue
        turn_counts[session_id] = turn_counts.get(session_id, 0) + 1
        stamp = _cli_iso(row.get("timestamp"))
        if stamp:
            span = turn_times.setdefault(session_id, [stamp, stamp])
            span[0], span[1] = min(span[0], stamp), max(span[1], stamp)

    usage_by_session: dict[str, dict[str, Any]] = {}
    for row in usage:
        session_id = row.get("session_id")
        if not session_id:
            continue
        agg = usage_by_session.setdefault(session_id, {"events": [], "models": {}, "genMs": 0, "genCount": 0})
        model = row.get("model") if isinstance(row.get("model"), str) else None
        in_tok = _valid_count(row.get("input_tokens"))
        out_tok = _valid_count(row.get("output_tokens"))
        cached = _valid_count(row.get("cache_read_tokens"))
        if cached is not None and (in_tok is None or cached > in_tok):
            cached = None
        cache_write = _valid_count(row.get("cache_write_tokens"))
        nano_aiu = _valid_count(row.get("total_nano_aiu"))
        duration = _valid_count(row.get("duration_ms"))
        event = {"timestamp": _cli_iso(row.get("created_at")), "model": display_model_name(model, catalog),
                 "inputTokens": in_tok, "outputTokens": out_tok, "cachedTokens": cached,
                 "cacheWriteTokens": cache_write, "credits": nano_aiu / 1_000_000_000 if nano_aiu is not None else None,
                 "creditsQuality": "reported" if nano_aiu is not None else "unavailable",
                 "priceSource": "Copilot CLI total_nano_aiu" if nano_aiu is not None else None,
                 "modelAttribution": "observed"}
        agg["events"].append(event)
        if model:
            agg["models"][model] = agg["models"].get(model, 0) + 1
        if duration is not None:
            agg["genMs"] += duration
            agg["genCount"] += 1

    result: list[dict[str, Any]] = []
    for session in sessions:
        session_id = session.get("id")
        if not session_id:
            continue
        aggregate = usage_by_session.get(session_id, {})
        events = aggregate.get("events", [])
        message_count = turn_counts.get(session_id, 0)
        title = session.get("summary")
        title = title.strip() if isinstance(title, str) and title.strip() else "(untitled)"
        if title == "(untitled)" and not message_count:
            continue
        cwd, repository = session.get("cwd"), session.get("repository")
        workspace = Path(cwd).name or cwd if isinstance(cwd, str) and cwd else repository or "(no workspace)"
        models = aggregate.get("models", {})
        top_model = max(models, key=models.get) if models else None
        span = turn_times.get(session_id)
        runtime = None
        if span:
            try:
                runtime = (datetime.fromisoformat(span[1]) - datetime.fromisoformat(span[0])).total_seconds() * 1000
            except ValueError:
                pass
        def summed(field: str) -> int | None:
            values = [event[field] for event in events]
            return sum(values) if values and all(value is not None for value in values) else None
        credit_values = [event["credits"] for event in events]
        credits = sum(value for value in credit_values if value is not None) if any(value is not None for value in credit_values) else None
        credits_quality = "reported" if credit_values and all(value is not None for value in credit_values) else "partial" if credits is not None else "unavailable"
        result.append({
            "id": session_id, "title": title, "workspace": workspace, "mode": cli_session_mode(session_id),
            "model": display_model_name(top_model, catalog), "messages": message_count,
            "credits": round(credits, 6) if credits is not None else None, "creditsQuality": credits_quality,
            "creditsSource": "Copilot CLI total_nano_aiu per-event totals",
            "inputTokens": summed("inputTokens"), "outputTokens": summed("outputTokens"),
            "cachedTokens": summed("cachedTokens"), "cacheWriteTokens": summed("cacheWriteTokens"),
            "tokenQuality": "exact" if events and all(event[k] is not None for event in events for k in ("inputTokens", "outputTokens")) else "partial",
            "contextUsage": None, "contextWindow": None, "currentContextTokens": None,
            "genMs": aggregate.get("genMs") if aggregate.get("genCount") else None,
            "runtimeMs": runtime, "createdAt": _cli_iso(session.get("created_at")),
            "updatedAt": _cli_iso(session.get("updated_at")) or _cli_iso(session.get("created_at")),
            "activity": events, "untimestampedCalls": sum(1 for event in events if event["timestamp"] is None),
            "priceSources": ["Copilot CLI total_nano_aiu"] if events else [],
        })
    return result


_EVENT_ACTIVITY_TYPES = ("user.message", "assistant.", "tool.execution_", "session.usage_record")


def _event_usage_record(kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Per-call usage from `session.usage_record` (newer CLI) or `assistant.usage` events."""
    usage = data.get("usage") if kind == "session.usage_record" else data
    if not isinstance(usage, dict):
        return None
    copilot_usage = usage.get("copilotUsage") if isinstance(usage.get("copilotUsage"), dict) else {}
    model = usage.get("model")
    return {"model": model if isinstance(model, str) and model else None,
            "callId": usage.get("apiCallId") if isinstance(usage.get("apiCallId"), str) else None,
            "inputTokens": _valid_count(usage.get("inputTokens")),
            "outputTokens": _valid_count(usage.get("outputTokens")),
            "cachedTokens": _valid_count(usage.get("cacheReadTokens")),
            "cacheWriteTokens": _valid_count(usage.get("cacheWriteTokens")),
            "durationMs": _valid_rate(usage.get("duration")),
            "nanoAiu": _valid_count(copilot_usage.get("totalNanoAiu")),
            "contextLimit": _valid_count(usage.get("maxPromptTokens"))}


def _shutdown_model_totals(data: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Cumulative per-model totals from `session.shutdown` (the CLI restores them on resume)."""
    totals: dict[str, dict[str, int]] = {}
    metrics = data.get("modelMetrics")
    if not isinstance(metrics, dict):
        return totals
    for model, entry in metrics.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        totals[model] = {"inputTokens": _valid_count(usage.get("inputTokens")) or 0,
                         "outputTokens": _valid_count(usage.get("outputTokens")) or 0,
                         "cachedTokens": _valid_count(usage.get("cacheReadTokens")) or 0,
                         "cacheWriteTokens": _valid_count(usage.get("cacheWriteTokens")) or 0,
                         "nanoAiu": _valid_count(entry.get("totalNanoAiu")) or 0}
    return totals


def _event_context_limit(model_id: str | None, catalog: dict[str, dict[str, Any]],
                         snapshots: list[dict[str, Any]]) -> int | None:
    """Prompt-token limit for a model: newest cached catalog `capabilities.limits`, then the catalog."""
    if not model_id:
        return None
    for snapshot in reversed(snapshots):
        limit = snapshot["models"].get(_slug(model_id), {}).get("promptLimit")
        if limit:
            return limit
    return model_context_window(model_id, catalog)


def read_copilot_cli_event_sessions(catalog: dict[str, dict[str, Any]], known_ids: set[str],
                                    snapshots: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Parse ~/.copilot/session-state/<id>/events.jsonl — the store used by Copilot
    sessions launched from VS Code's Agents window, which never land in session-store.db.
    Per-call tokens come from `session.usage_record`/`assistant.usage` when logged; otherwise
    from the cumulative `session.shutdown` model metrics, split per process segment."""
    state_dir = resolve_copilot_cli_dir() / "session-state"
    if not state_dir.is_dir():
        return []
    sessions: list[dict[str, Any]] = []
    for events_path in sorted(state_dir.glob("*/events.jsonl")):
        session_id = events_path.parent.name
        if session_id in known_ids:
            continue
        created_at: str | None = None
        last_stamp: str | None = None
        cwd: str | None = None
        title: str | None = None
        models: dict[str, int] = {}
        nano_aiu: int | None = None
        message_count = 0
        activity: list[dict[str, Any]] = []
        # A segment is one CLI process lifetime, closed by session.shutdown.
        segment_messages: list[dict[str, Any]] = []
        segment_usage: list[dict[str, Any]] = []
        seen_call_ids: set[str] = set()
        baseline: dict[str, dict[str, int]] = {}
        last_shutdown: dict[str, Any] | None = None
        usage_after_shutdown_ms = 0.0
        usage_after_shutdown_count = 0
        context: dict[str, Any] | None = None
        busy_ms = 0.0
        span_start: datetime | None = None
        span_end: datetime | None = None
        last_active: str | None = None

        def call_row(stamp: str | None, model: str | None, record: dict[str, Any] | None,
                     source: str | None) -> dict[str, Any]:
            nano = record.get("nanoAiu") if record else None
            input_tokens = record.get("inputTokens") if record else None
            cached = record.get("cachedTokens") if record else None
            if cached is not None and (input_tokens is None or cached > input_tokens):
                cached = None
            return {"timestamp": stamp, "model": display_model_name(model, catalog),
                    "inputTokens": input_tokens, "outputTokens": record.get("outputTokens") if record else None,
                    "cachedTokens": cached, "cacheWriteTokens": record.get("cacheWriteTokens") if record else None,
                    "credits": nano / 1_000_000_000 if nano is not None else None,
                    "creditsQuality": "reported" if nano is not None else "unavailable",
                    "priceSource": source if nano is not None else None,
                    "modelAttribution": "observed" if model else None}

        def close_segment(shutdown: dict[str, Any] | None, stamp: str | None) -> None:
            nonlocal baseline
            if segment_usage:
                activity.extend(call_row(r["timestamp"], r["model"], r, "Copilot CLI usage record totalNanoAiu")
                                for r in segment_usage)
            else:
                rows = [call_row(m["timestamp"], m["model"], None, None) for m in segment_messages]
                if shutdown is not None:
                    totals = _shutdown_model_totals(shutdown)
                    for model, total in totals.items():
                        delta = {k: max(0, v - baseline.get(model, {}).get(k, 0)) for k, v in total.items()}
                        if not any(delta.values()):
                            continue
                        owners = [i for i, m in enumerate(segment_messages) if m["model"] == model]
                        if not owners:
                            rows.append(call_row(stamp, model, None, None))
                            owners = [len(rows) - 1]
                        # Shutdown metrics are per segment, not per call: attribute the segment
                        # delta to the model's last call and zero its other calls.
                        for i in owners[:-1]:
                            rows[i] = call_row(rows[i]["timestamp"], model, dict.fromkeys(delta, 0),
                                               "Copilot CLI session.shutdown totalNanoAiu (segment aggregate)")
                        rows[owners[-1]] = call_row(rows[owners[-1]]["timestamp"], model, delta,
                                                    "Copilot CLI session.shutdown totalNanoAiu (segment aggregate)")
                activity.extend(rows)
            if shutdown is not None:
                baseline = _shutdown_model_totals(shutdown) or baseline
            segment_messages.clear()
            segment_usage.clear()

        def close_span() -> None:
            nonlocal busy_ms, span_start, span_end
            if span_start and span_end and span_end > span_start:
                busy_ms += (span_end - span_start).total_seconds() * 1000
            span_start = span_end = None

        try:
            with events_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    stamp = _cli_iso(event.get("timestamp"))
                    if stamp:
                        last_stamp = stamp
                    data = event.get("data")
                    data = data if isinstance(data, dict) else {}
                    kind = event.get("type")
                    kind = kind if isinstance(kind, str) else ""
                    moment = datetime.fromisoformat(stamp) if stamp else None
                    if moment and kind.startswith(_EVENT_ACTIVITY_TYPES):
                        last_active = stamp
                        if kind == "user.message":
                            close_span()
                        span_start = span_start or moment
                        span_end = max(span_end or moment, moment)
                    if kind in ("session.shutdown", "session.resume", "session.start"):
                        close_span()
                    if kind == "session.start":
                        created_at = created_at or _cli_iso(data.get("startTime")) or stamp
                        start_context = data.get("context")
                        if isinstance(start_context, dict) and isinstance(start_context.get("cwd"), str):
                            cwd = start_context["cwd"]
                        model = data.get("selectedModel")
                        if isinstance(model, str) and model:
                            models[model] = models.get(model, 0) + 1
                    elif kind == "user.message":
                        message_count += 1
                        content = data.get("content")
                        if title is None and isinstance(content, str) and content.strip():
                            title = content.strip().splitlines()[0][:120]
                    elif kind == "assistant.message":
                        model = data.get("model")
                        model = model if isinstance(model, str) and model else None
                        if model:
                            models[model] = models.get(model, 0) + 1
                        segment_messages.append({"timestamp": stamp, "model": model})
                    elif kind in ("session.usage_record", "assistant.usage"):
                        record = _event_usage_record(kind, data)
                        if record is None or (record["callId"] and record["callId"] in seen_call_ids):
                            continue
                        if record["callId"]:
                            seen_call_ids.add(record["callId"])
                        record["timestamp"] = stamp
                        segment_usage.append(record)
                        if record["durationMs"] is not None:
                            usage_after_shutdown_ms += record["durationMs"]
                            usage_after_shutdown_count += 1
                        if record["inputTokens"] is not None:
                            current = record["inputTokens"] + (record["outputTokens"] or 0)
                            limit = record["contextLimit"] or _event_context_limit(record["model"], catalog, snapshots or [])
                            context = {"currentTokens": current, "tokenLimit": limit,
                                       "percent": round(current * 100 / limit, 1) if limit else None,
                                       "source": "Copilot CLI usage record (latest call prompt + output)"}
                    elif kind == "session.usage_checkpoint":
                        value = _valid_count(data.get("totalNanoAiu"))
                        if value is not None:
                            nano_aiu = max(nano_aiu or 0, value)
                    elif kind == "session.shutdown":
                        close_segment(data, stamp)
                        last_shutdown = data
                        usage_after_shutdown_ms, usage_after_shutdown_count = 0.0, 0
                        current = _valid_count(data.get("currentTokens"))
                        if current is not None:
                            model = data.get("currentModel") if isinstance(data.get("currentModel"), str) else None
                            limit = _event_context_limit(model, catalog, snapshots or [])
                            context = {"currentTokens": current, "tokenLimit": limit,
                                       "percent": round(current * 100 / limit, 1) if limit else None,
                                       "source": "Copilot CLI session.shutdown currentTokens"
                                                 + ("; configured model context limit" if limit else "")}
        except OSError:
            continue
        close_span()
        close_segment(None, last_stamp)
        if not activity and not message_count and created_at is None:
            continue
        workspace = Path(cwd).name or cwd if cwd else "(no workspace)"
        top_model = max(models, key=models.get) if models else None

        def known_sum(field: str) -> int | None:
            values = [row[field] for row in activity if row[field] is not None]
            return sum(values) if values else None
        token_known = bool(activity) and all(row["inputTokens"] is not None and row["outputTokens"] is not None
                                              for row in activity)
        gen_ms = None
        if last_shutdown is not None and _valid_rate(last_shutdown.get("totalApiDurationMs")) is not None:
            gen_ms = _valid_rate(last_shutdown.get("totalApiDurationMs")) + usage_after_shutdown_ms
        elif usage_after_shutdown_count:
            gen_ms = usage_after_shutdown_ms
        price_sources = sorted({row["priceSource"] for row in activity if row.get("priceSource")})
        if nano_aiu is not None and "Copilot CLI total_nano_aiu" not in price_sources:
            price_sources.append("Copilot CLI total_nano_aiu")
        credits, credits_quality = (nano_aiu / 1_000_000_000, "reported") if nano_aiu is not None else (None, "unavailable")
        credits_source = "Copilot CLI session.usage_checkpoint totalNanoAiu"
        call_credits = [row["credits"] for row in activity if row["credits"] is not None]
        if credits is None and call_credits:
            credits = sum(call_credits)
            credits_quality = "reported" if len(call_credits) == len(activity) else "partial"
            credits_source = "Copilot CLI per-call totalNanoAiu"
        sessions.append({
            "id": session_id, "title": title or "(untitled)", "workspace": workspace,
            "mode": cli_session_mode(session_id), "model": display_model_name(top_model, catalog),
            "messages": message_count,
            "credits": round(credits, 6) if credits is not None else None,
            "creditsQuality": credits_quality, "creditsSource": credits_source,
            "inputTokens": known_sum("inputTokens"), "outputTokens": known_sum("outputTokens"),
            "cachedTokens": known_sum("cachedTokens"), "cacheWriteTokens": known_sum("cacheWriteTokens"),
            "tokenQuality": "exact" if token_known else "partial",
            "contextUsage": context, "contextWindow": context.get("tokenLimit") if context else None,
            "currentContextTokens": context.get("currentTokens") if context else None,
            "genMs": gen_ms, "runtimeMs": busy_ms if busy_ms > 0 else None,
            "createdAt": created_at, "updatedAt": last_active or last_stamp or created_at,
            "activity": activity,
            "untimestampedCalls": sum(1 for event in activity if event["timestamp"] is None),
            "priceSources": price_sources,
        })
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
    snapshots = load_live_model_pricing(user_dir)
    for snapshot in snapshots:
        for slug, model in snapshot["models"].items():
            entry = catalog.setdefault(slug, {})
            if model.get("name"):
                entry.setdefault("name", model["name"])
            if model.get("maxInputTokens"):
                entry.setdefault("maxInputTokens", model["maxInputTokens"])

    sessions: list[dict[str, Any]] = []
    for doc, workspace, fallback_id in parsed:
        session = extract_session(doc, workspace, catalog, fallback_id, snapshots)
        if session is not None:
            sessions.append(session)
    hidden_empty = len(parsed) - len(sessions)
    chat_ids = {session["id"] for session in sessions}
    cli_sessions = read_copilot_cli_sessions(catalog)
    duplicates = sum(1 for session in cli_sessions if session["id"] in chat_ids)
    sessions.extend(session for session in cli_sessions if session["id"] not in chat_ids)
    known_ids = {session["id"] for session in sessions}
    sessions.extend(read_copilot_cli_event_sessions(catalog, known_ids, snapshots))
    kept = [item for item in sessions if item.get("title") not in (None, "", "(untitled)") and item.get("credits") is not None]
    hidden_empty += len(sessions) - len(kept)
    sessions = kept
    sessions.sort(key=_sort_key, reverse=True)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(), "userDir": str(user_dir),
        "hiddenEmpty": hidden_empty,
        "plan": {"name": resolve_plan_name(user_dir) or FALLBACK_PLAN_NAME, "limit": 0,
                 "account": resolve_subscription_account(user_dir)},
        "sessions": sessions,
        "accounting": {"priceCatalogSnapshots": len(snapshots), "duplicateCliSessionsExcluded": duplicates,
                       "untimestampedCalls": sum(session.get("untimestampedCalls", 0) for session in sessions),
                       "offline": True},
    }


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub Copilot Session Dashboard</title><style>
:root{__THEME_VARS__;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--fg);background:var(--bg);line-height:1.4}*{box-sizing:border-box}body{margin:0}.shell{max-width:1780px;margin:auto;padding:32px 24px 48px}header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:28px}h1{font-size:25px;letter-spacing:-.02em;margin:0 0 5px}h2{font-size:16px;margin:0 0 16px}.muted,.meta{color:var(--muted);font-size:13px}.actions{display:flex;gap:9px;align-items:center}button,.input{font:inherit;border:1px solid var(--border);border-radius:7px;background:var(--control-bg);padding:8px 11px;color:inherit}button{cursor:pointer}button:hover{background:var(--control-hover)}input[type=checkbox]{accent-color:var(--control-bg)}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}.card,.panel{border-radius:10px;box-shadow:0 1px 2px var(--card-shadow)}.panel{border:1px solid var(--accent)}.card{background:var(--card-bg);padding:17px}.card-label{text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-size:11px;font-weight:650}.metric{font-size:23px;font-weight:650;margin:8px 0 3px}.panel{padding:20px}.panel-head{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:16px}.controls{display:flex;gap:9px;flex-wrap:wrap}.search{min-width:260px}.table-wrap{overflow:auto;margin:0 -20px}.session-table{width:100%;border-collapse:collapse;min-width:980px}.session-table th,.session-table td{text-align:left;padding:11px 10px;border-top:0.1px solid var(--table-border);font-size:13px;white-space:nowrap}.session-table th{color:var(--muted);font-weight:600;font-size:12px;cursor:pointer}.session-table th:hover{color:var(--fg)}.session-table tbody tr:hover{background:var(--row-hover)}.title{width:220px;max-width:220px;overflow:hidden;text-overflow:ellipsis}.workspace{width:150px;max-width:150px;overflow:hidden;text-overflow:ellipsis}.share-col{padding-left:6px;padding-right:3px}.session-table th[data-sort="updatedAt"],.session-table td.updated{width:1%}.session-table th[data-sort="messages"],.session-table td.messages{width:1%}.tip{position:fixed;z-index:2000;background:var(--control-bg);color:var(--fg);padding:6px 9px;border-radius:6px;font-size:12px;max-width:420px;white-space:normal;word-break:break-word;pointer-events:none;box-shadow:0 4px 14px var(--popup-shadow)}.badge{display:inline-block;padding:3px 7px;border-radius:12px;background:var(--accent);color:var(--badge-fg);font-size:11px}.badge.m-cli{background:var(--mode-cli)}.badge.m-autopilot{background:var(--mode-autopilot)}.badge.m-agents{background:var(--mode-agents)}.badge.m-chat{background:var(--mode-chat)}.footer{padding-top:13px}.settings{display:flex;align-items:center;gap:7px;font-size:13px}.error{padding:20px;color:var(--error-fg);background:var(--error-bg);border:1px solid var(--error-border);border-radius:10px;white-space:pre-wrap}.nowrap{white-space:nowrap}.share-btn{border:0;background:none;padding:0;width:26px;height:26px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;color:var(--muted);cursor:pointer}.share-btn:hover{background:var(--control-hover);color:var(--fg)}.session-table th.no-sort{cursor:default}.share-menu{position:fixed;background:var(--card-bg);border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 20px var(--menu-shadow);z-index:1000;min-width:170px;padding:5px;font-size:13px}.share-menu button{display:block;width:100%;text-align:left;border:0;background:none;border-radius:6px;padding:7px 11px;font:inherit;color:inherit;cursor:pointer}.share-menu button:hover{background:var(--control-hover)}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}header,.panel-head{align-items:flex-start;flex-direction:column}}@media(max-width:500px){.shell{padding:22px 14px}.grid{grid-template-columns:1fr}.actions{width:100%}.actions button{flex:1}}
</style></head><body><main class="shell"><header><div><h1>GitHub Copilot Session Dashboard</h1><div class="meta" id="source">Loading local state…</div></div><div class="actions"><span class="meta" id="asof"></span><button id="refresh" title="Refresh" aria-label="Refresh" style="display:inline-flex;align-items:center;justify-content:center"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.89"/><polyline points="13.5 1.5 13.5 4.5 10.5 4.5"/></svg></button><label class="settings"><input id="auto" type="checkbox"> Auto-refresh</label></div></header><div id="app"><div class="muted">Loading…</div></div><div id="tip" class="tip" hidden></div></main><script>
const CREDIT_USD_RATE=__CREDIT_USD_RATE__;
const state={data:null,sort:'updatedAt',desc:true};const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date=v=>v?new Date(v).toLocaleString([], {dateStyle:'medium',timeStyle:'short'}):'—';const dte=v=>v?new Date(v).toLocaleDateString([], {dateStyle:'medium'}):'—';const dmy=v=>{if(!v)return'—';const d=new Date(v);return`${String(d.getMonth()+1).padStart(2,'0')}/${String(d.getDate()).padStart(2,'0')}/${String(d.getFullYear()).slice(-2)}`};const fmt=v=>v==null?'—':Number(v).toLocaleString();const dur=v=>{if(v==null)return'—';const s=Math.round(Number(v)/1000);if(s<60)return`${s}s`;const m=Math.floor(s/60);if(m<60)return`${m}m ${s%60}s`;return`${(s/3600).toFixed(1)}h`};
function renderLegacy(){closeShareMenu();hideTip();const d=state.data,p=d.plan||{},rate=CREDIT_USD_RATE;const now=new Date(),monthSessions=(d.sessions||[]).filter(s=>{const t=s.createdAt?new Date(s.createdAt):null;return t&&t.getMonth()===now.getMonth()&&t.getFullYear()===now.getFullYear()});const consumed=monthSessions.reduce((a,s)=>a+(Number(s.credits)||0),0),est=(consumed*rate).toFixed(2);const tokIn=monthSessions.reduce((a,s)=>a+(Number(s.inputTokens)||0),0),tokOut=monthSessions.reduce((a,s)=>a+(Number(s.outputTokens)||0),0),modelCounts={},modelTok={},modelCredits={};monthSessions.forEach(s=>{if(s.model&&s.model!=='—'){modelCounts[s.model]=(modelCounts[s.model]||0)+1;modelTok[s.model]=(modelTok[s.model]||0)+(Number(s.inputTokens)||0)+(Number(s.outputTokens)||0);modelCredits[s.model]=(modelCredits[s.model]||0)+(Number(s.credits)||0)}});const top=o=>Object.entries(o).sort((a,b)=>b[1]-a[1])[0],topTok=top(modelTok),topCred=top(modelCredits),topCnt=top(modelCounts);
$('source').textContent=`Local Copilot sessions (Chat panel + CLI) · ${(d.sessions||[]).length} total`;$('asof').textContent=`Last update: ${date(d.generatedAt)}`;let rows=d.sessions||[];const prevSearch=$('search'),raw=prevSearch?.value||'',q=raw.toLowerCase(),focused=prevSearch&&document.activeElement===prevSearch,caret=prevSearch?.selectionStart;if(q)rows=rows.filter(s=>[s.title,s.workspace,s.id,s.model,s.mode].join(' ').toLowerCase().includes(q));rows.sort((a,b)=>{const v=s=>state.sort==='cost'?(s.credits==null?'':s.credits*rate):s[state.sort];let x=v(a)??'',y=v(b)??'';if(state.sort.includes('At')){x=new Date(x||0).getTime();y=new Date(y||0).getTime()}else if(typeof x==='string'){x=x.toLowerCase();y=String(y).toLowerCase()}return (x<y?-1:x>y?1:0)*(state.desc?-1:1)});
$('app').innerHTML=`<section class="grid"><article class="card"><div class="card-label">Plan</div><div class="metric">${esc(p.name)}</div>${p.account?`<div class="muted">@${esc(p.account)}</div>`:''}<div class="card-label" style="margin-top:14px">Estimated cost (this month)</div><div class="metric">$${est}</div><div class="muted">$${rate.toFixed(2)} / credit</div></article><article class="card"><div class="card-label">Credits consumed</div><div class="metric">${consumed.toFixed(2)}</div><div class="muted">Unlimited plan allotment</div><div class="muted">Estimated locally from logged token usage</div></article><article class="card"><div class="card-label">Sessions this month</div><div class="metric">${fmt(monthSessions.length)}</div><div class="muted">Tokens:</div><div class="muted">IN ${fmt(tokIn)} · OUT ${fmt(tokOut)}</div></article><article class="card"><div class="card-label">Model metrics</div><div class="metric">${fmt(Object.keys(modelCounts).length)}</div><div class="muted">Top by tokens: ${topTok?esc(topTok[0])+' · '+fmt(topTok[1]):'—'}</div><div class="muted">Top by credits: ${topCred?esc(topCred[0])+' · '+topCred[1].toFixed(2):'—'}</div><div class="muted">Top by count: ${topCnt?esc(topCnt[0])+' · '+fmt(topCnt[1]):'—'}</div></article></section><section class="panel"><div class="panel-head"><div><h2>Sessions <span class="muted">(${rows.length!==d.sessions.length?`${rows.length} of `:''}${d.sessions.length} Total)</span></h2></div><div class="controls"><input class="input search" id="search" placeholder="Search title, workspace, model…" value="${esc(raw)}"></div></div><div class="table-wrap"><table class="session-table"><thead><tr><th class="no-sort share-col"></th>${[['updatedAt','Last updated'],['workspace','Workspace'],['title','Title'],['model','Model'],['mode','Mode'],['credits','Credits'],['cost','Est. cost'],['inputTokens','Tokens in'],['outputTokens','Tokens out'],['cachedTokens','Cached'],['contextWindow','Context'],['genMs','Gen. time'],['runtimeMs','Runtime'],['createdAt','Created'],['messages',MESSAGE_ICON_SVG]].map(([k,l])=>`<th data-sort="${k}">${l}${state.sort===k?' '+(state.desc?'↓':'↑'):''}</th>`).join('')}</tr></thead><tbody>${rows.map(s=>`<tr><td class="nowrap share-col"><button class="share-btn" data-id="${esc(s.id)}" title="Share" aria-label="Share session">${SHARE_ICON_SVG}</button></td><td class="nowrap updated">${date(s.updatedAt)}</td><td class="workspace">${esc(s.workspace||'—')}</td><td class="title">${esc(s.title)}</td><td>${esc(s.model||'—')}</td><td class="nowrap"><span class="badge m-${esc(s.mode||'')}">${esc(s.mode||'—')}</span></td><td class="nowrap">${s.credits==null?'—':s.credits.toFixed(2)}</td><td class="nowrap">${s.credits==null?'—':'$'+(s.credits*rate).toFixed(2)}</td><td class="nowrap">${fmt(s.inputTokens)}</td><td class="nowrap">${fmt(s.outputTokens)}</td><td class="nowrap">${fmt(s.cachedTokens)}</td><td class="nowrap">${fmt(s.contextWindow)}</td><td class="nowrap">${dur(s.genMs)}</td><td class="nowrap">${dur(s.runtimeMs)}</td><td class="nowrap">${dmy(s.createdAt)}</td><td class="messages">${s.messages}</td></tr>`).join('')||'<tr><td colspan="16" class="muted">No sessions match this search.</td></tr>'}</tbody></table></div><div class="footer muted">${d.sessions.length} sessions loaded from local VS Code Copilot Chat logs and the Copilot CLI's own session history.${d.hiddenEmpty?' '+d.hiddenEmpty+' empty sessions (no title, no messages) hidden.':''} Chat panel credits are estimated locally from logged token usage using GitHub's published per-model rates; CLI sessions (mode "cli") use the CLI's own already-computed cost. Neither is the authoritative total from your GitHub account.</div></section>`;
$('search').addEventListener('input',render);document.querySelectorAll('[data-sort]').forEach(el=>el.addEventListener('click',()=>{if(state.sort===el.dataset.sort)state.desc=!state.desc;else{state.sort=el.dataset.sort;state.desc=true}render()}));const search=$('search');if(search&&focused){search.focus();search.setSelectionRange(caret??raw.length,caret??raw.length)}}
function render(){
 closeShareMenu();hideTip();const d=state.data,p=d.plan||{},sessions=d.sessions||[],now=new Date(),monthCalls=[],unknownTime=sessions.reduce((n,s)=>n+(s.untimestampedCalls||0),0);
 sessions.forEach(s=>(s.activity||[]).forEach(c=>{if(!c.timestamp)return;const t=new Date(c.timestamp);if(!isNaN(t)&&t.getMonth()===now.getMonth()&&t.getFullYear()===now.getFullYear())monthCalls.push({...c,sessionId:s.id})}));
 const monthSessionCount=new Set(monthCalls.map(c=>c.sessionId)).size,billableCalls=monthCalls.filter(c=>c.creditsQuality!=='byok'),hasBillableCalls=billableCalls.length>0,knownCredits=billableCalls.reduce((n,c)=>n+(c.credits==null?0:Number(c.credits)),0),creditMissing=billableCalls.some(c=>c.credits==null),estimated=billableCalls.some(c=>String(c.creditsQuality||'').startsWith('estimated')||c.creditsQuality==='partial'),incompleteTokens=key=>monthCalls.some(c=>c[key]==null),sum=key=>monthCalls.reduce((n,c)=>n+(c[key]==null?0:Number(c[key])),0);
 const tokIn=sum('inputTokens'),tokOut=sum('outputTokens'),models={};monthCalls.forEach(c=>{const parts=c.modelBreakdown?.length?c.modelBreakdown:[{model:c.model,inputTokens:c.inputTokens,outputTokens:c.outputTokens}],perCall={};parts.forEach(part=>{if(!part.model||part.model==='—')return;const m=perCall[part.model]||(perCall[part.model]={tokens:0});m.tokens+=(Number(part.inputTokens)||0)+(Number(part.outputTokens)||0)});Object.entries(perCall).forEach(([name,part])=>{const m=models[name]||(models[name]={calls:0,tokens:0,credits:0,creditCalls:0,estimatedCalls:0});m.calls++;m.tokens+=part.tokens;if(Object.keys(perCall).length===1&&c.credits!=null){m.credits+=Number(c.credits);m.creditCalls++;if(String(c.creditsQuality||'').startsWith('estimated')||c.creditsQuality==='partial')m.estimatedCalls++}})});
 const top=(key,field)=>Object.entries(models).sort((a,b)=>b[1][field]-a[1][field])[0],topTok=top('tokens','tokens'),topCred=top('credits','credits'),topCount=top('calls','calls');
 $('source').textContent=`Offline local Copilot sessions (Chat panel + CLI) · ${sessions.length} total`;$('asof').textContent=`Last update: ${date(d.generatedAt)}`;
 let rows=sessions;const prev=$('search'),raw=prev?.value||'',q=raw.toLowerCase(),focused=prev&&document.activeElement===prev,caret=prev?.selectionStart;if(q)rows=rows.filter(s=>[s.title,s.workspace,s.id,s.model,s.mode].join(' ').toLowerCase().includes(q));
 rows=[...rows].sort((a,b)=>{const v=s=>state.sort==='cost'?(s.credits==null?null:s.credits*CREDIT_USD_RATE):s[state.sort];let x=v(a),y=v(b);if(x==null)x='';if(y==null)y='';if(state.sort.includes('At')){x=new Date(x||0).getTime();y=new Date(y||0).getTime()}else if(typeof x==='string'){x=x.toLowerCase();y=String(y).toLowerCase()}return(x<y?-1:x>y?1:0)*(state.desc?-1:1)});
 const creditText=!hasBillableCalls?'—':creditMissing?(knownCredits?knownCredits.toFixed(2):'—'):`${estimated?'~':''}${knownCredits.toFixed(2)}`,usdText=!hasBillableCalls?'—':(estimated?'~ ':'')+'$'+(knownCredits*CREDIT_USD_RATE).toFixed(2),monthNote=monthCalls.length?`${monthCalls.length} timestamped calls${unknownTime?` · ${unknownTime} calls have no timestamp and are excluded`:''}`:'No timestamped calls this month';
 $('app').innerHTML=`<section class="grid"><article class="card"><div class="card-label">Plan</div><div class="metric">${esc(p.name||'GitHub Copilot')}</div>${p.account?`<div class="muted">@${esc(p.account)}</div>`:''}<div class="card-label" style="margin-top:14px">Cost this month</div><div class="metric">${usdText}</div><div class="muted">${!hasBillableCalls?'No Copilot-billed calls':creditMissing?(estimated?'Incomplete coverage; includes estimates':'Incomplete per-call coverage'):estimated?'Includes local estimates':'From reported credits'} · $${CREDIT_USD_RATE.toFixed(2)} / credit</div></article><article class="card"><div class="card-label">Credits this month</div><div class="metric">${creditText}</div><div class="muted">${!hasBillableCalls?'No Copilot-billed calls':creditMissing?(estimated?'Known subtotal; incomplete and estimated':'Known subtotal; incomplete coverage'):estimated?'Reported and estimated amounts':'Reported per-call credits'}</div><div class="muted">${esc(monthNote)}</div></article><article class="card"><div class="card-label">Sessions this month</div><div class="metric">${fmt(monthSessionCount)}</div><div class="muted">Per-call activity by local calendar month</div><div class="muted">IN ${monthCalls.length?fmt(tokIn):'—'} · OUT ${monthCalls.length?fmt(tokOut):'—'} · Cached subset ${monthCalls.length?fmt(sum('cachedTokens')):'—'}</div></article><article class="card"><div class="card-label">Model metrics this month</div><div class="metric">${fmt(Object.keys(models).length)}</div><div class="muted">Top by tokens${incompleteTokens('inputTokens')||incompleteTokens('outputTokens')?' (partial)':''}${monthCalls.some(c=>c.modelAttribution==='approximate')?' (approximate)':''}: ${topTok?`${esc(topTok[0])} · ${fmt(topTok[1].tokens)}`:'—'}</div><div class="muted">Top by known credits: ${topCred?`${esc(topCred[0])} · ${topCred[1].estimatedCalls?'~':''}${topCred[1].credits.toFixed(2)}${topCred[1].creditCalls<topCred[1].calls?' (partial)':''}`:'—'}</div><div class="muted">Top by calls: ${topCount?`${esc(topCount[0])} · ${fmt(topCount[1].calls)}`:'—'} · request-model attribution</div></article></section><section class="panel"><div class="panel-head"><div><h2>Sessions <span class="muted">(${rows.length!==sessions.length?`${rows.length} of `:''}${sessions.length} total)</span></h2><div class="muted">Lifetime totals; Cached is a subset of IN. Current context is the latest persisted response only.</div></div><div class="controls"><input class="input search" id="search" placeholder="Search title, workspace, model…" value="${esc(raw)}"></div></div><div class="table-wrap"><table class="session-table"><thead><tr><th class="no-sort share-col"></th>${[['updatedAt','Last updated'],['workspace','Workspace'],['title','Title'],['model','Model'],['mode','Mode'],['credits','Credits'],['cost','Cost'],['inputTokens','IN'],['outputTokens','OUT'],['currentContextTokens','Context'],['genMs','Gen. time'],['runtimeMs','Runtime'],['messages',MESSAGE_ICON_SVG]].map(([k,v])=>`<th data-sort="${k==='credits'?'credits':k==='cost'?'cost':k}">${v}</th>`).join('')}</tr></thead><tbody>${rows.map(s=>{const cu=s.contextUsage,credits=s.credits==null?'—':`${s.credits.toFixed(2)}${s.creditsQuality==='reported'||s.creditsQuality==='reported/reconciled'?'':'*'}`,context=cu?`${fmt(cu.currentTokens)}${cu.tokenLimit?` (${cu.percent}%)`:''}`:'—';return`<tr><td class="share-col"><button class="share-btn" data-id="${esc(s.id)}" title="Share session" aria-label="Share session">${SHARE_ICON_SVG}</button></td><td class="updated" title="${esc(s.updatedAt||'Unavailable')}">${date(s.updatedAt)}</td><td class="workspace" title="${esc(s.workspace)}">${esc(s.workspace)}</td><td class="title" title="${esc(s.title)}">${esc(s.title)}</td><td title="${esc((s.priceSources||[]).join('; '))}">${esc(s.model)}</td><td class="nowrap"><span class="badge m-${esc(s.mode||'')}">${esc(s.mode||'—')}</span></td><td title="${esc(s.creditsSource||s.creditsQuality||'unavailable')}">${credits}</td><td>${s.credits==null?'—':`${s.creditsQuality==='reported'||s.creditsQuality==='reported/reconciled'?'':'~'}${estCost(s)}`}</td><td title="${esc(s.tokenQuality||'unavailable')}">${fmt(s.inputTokens)}${s.tokenQuality==='partial'?'*':''}</td><td title="${esc(s.tokenQuality||'unavailable')}">${fmt(s.outputTokens)}${s.tokenQuality==='partial'?'*':''}</td><td title="${esc(cu?.source||'No current context data')}">${context}</td><td>${dur(s.genMs)}</td><td>${dur(s.runtimeMs)}</td><td class="messages">${fmt(s.messages)}</td></tr>`}).join('')}</tbody></table></div><div class="muted footer">* Estimated or partial amount. — means unavailable, not zero. Monthly totals omit calls without timestamps; model credit ranking is based on observed request-model attribution.</div></section>`;
 $('search').addEventListener('input',render);document.querySelectorAll('[data-sort]').forEach(el=>el.addEventListener('click',()=>{if(state.sort===el.dataset.sort)state.desc=!state.desc;else{state.sort=el.dataset.sort;state.desc=true}render()}));const search=$('search');if(search&&focused){search.focus();search.setSelectionRange(caret??raw.length,caret??raw.length)}
}
const SHARE_ICON_SVG='<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.6v7.9"/><polyline points="4.9 4.8 8 1.6 11.1 4.8"/><path d="M4.6 6.6H3.6a1 1 0 0 0-1 1v5.8a1 1 0 0 0 1 1h8.8a1 1 0 0 0 1-1V7.6a1 1 0 0 0-1-1h-1"/></svg>';
const MESSAGE_ICON_SVG='<span title="Messages" aria-label="Messages" style="display:inline-flex;vertical-align:middle"><svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 10.5a1.5 1.5 0 0 1-1.5 1.5H5l-3 2.5V3.5A1.5 1.5 0 0 1 3.5 2h9A1.5 1.5 0 0 1 14 3.5z"/></svg></span>';
let shareMenu=null;
function closeShareMenu(){if(!shareMenu)return;const m=shareMenu;shareMenu=null;m.el.remove();document.removeEventListener('click',m.onDoc,true);document.removeEventListener('keydown',m.onKey)}
function shareFileDate(s){const v=s.createdAt||s.updatedAt;const d=v?new Date(v):new Date();if(isNaN(d))return new Date().toISOString().slice(0,10);const p=n=>String(n).padStart(2,'0');return`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`}
function shareSanitize(v){return String(v||'').replace(/[\\/:*?"<>|\x00-\x1f]/g,'-').replace(/\s+/g,' ').replace(/^[.\s]+/,'').replace(/[.\s]+$/,'').slice(0,80).trim()}
function shareFilename(s){let base=shareSanitize(s.title);if(!base||base==='(untitled)')base=shareSanitize(s.id)||'session';return`${base} ${shareFileDate(s)}.md`}
function estCost(s){return s.credits==null?'—':(s.credits*CREDIT_USD_RATE).toFixed(2)}
function sessionMarkdown(s){const pipe=v=>String(v??'').replace(/\|/g,'\\|'),val=v=>pipe(v==null||v===''?'—':v),cu=s.contextUsage;return['# '+pipe(s.title||'(untitled)'),'','| Field | Value |','| --- | --- |','| Session ID | '+val(s.id)+' |','| Workspace | '+val(s.workspace)+' |','| Mode | '+val(s.mode)+' |','| Model | '+val(s.model)+' |','| Calls | '+val(s.messages)+' |','| Credits | '+(s.credits==null?'—':s.credits.toFixed(6)+' ('+val(s.creditsQuality)+')')+' |','| Cost at $0.01/credit | '+(s.credits==null?'—':(s.credits*CREDIT_USD_RATE).toFixed(4))+' |','| Cost provenance | '+val(s.creditsSource)+' |','| IN lifetime | '+fmt(s.inputTokens)+' ('+val(s.tokenQuality)+') |','| OUT lifetime | '+fmt(s.outputTokens)+' ('+val(s.tokenQuality)+') |','| Cached subset of IN | '+fmt(s.cachedTokens)+' |','| Cache writes | '+fmt(s.cacheWriteTokens)+' |','| Current context | '+(cu?fmt(cu.currentTokens):'—')+' |','| Context limit | '+(cu?fmt(cu.tokenLimit):'—')+' |','| Context source | '+val(cu?.source)+' |','| Price source | '+val((s.priceSources||[]).join('; '))+' |','| Gen. time | '+dur(s.genMs)+' |','| Runtime | '+dur(s.runtimeMs)+' |','| Created | '+date(s.createdAt)+' |','| Last updated | '+date(s.updatedAt)+' |','','Costs prefer VS Code reported credits; rate-card values, when available, are estimates. Unknown is not zero.'].join('\n')}
async function copySessionJson(s){const text=JSON.stringify({...s,creditUsdRate:CREDIT_USD_RATE,costUsd:s.credits==null?null:Number((s.credits*CREDIT_USD_RATE).toFixed(4)),costQuality:s.creditsQuality,costProvenance:s.creditsSource||s.priceSources||[]},null,2);try{await navigator.clipboard.writeText(text);return}catch(e){}const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{document.execCommand('copy')}catch(e){}ta.remove()}
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
            html = HTML.replace("__CREDIT_USD_RATE__", str(CREDIT_USD_RATE)).replace("__THEME_VARS__", THEME_VARS_CSS)
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
