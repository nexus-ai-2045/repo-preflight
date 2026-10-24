"""採用先リポジトリの preflight 設定 (dismiss / snooze)。

依存ゼロの JSON。パスは採用先 repo の `.repo-preflight.json`。
secret / personal path など fail-closed 項目は dismiss 不可。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PREFERENCES_SCHEMA = "repo-preflight.preferences/v1"
PREFERENCES_FILENAME = ".repo-preflight.json"

# 絶対に「次から出さない」を付けない kind
NEVER_DISMISSIBLE_KINDS = frozenset(
    {
        "security_hold",
        "privacy_hold",
        "tool_error",
        "missing_artifact",
        "invalid_artifact",
        "worktree_state",
        "identity_hold",
        "missing_remote",
        "missing_scan",
        "intent_confirmation",
        "audience",
        "github_settings",  # private default / owner 確定は毎回
    }
)

GITHUB_BASELINE_MARKER = re.compile(
    r"<!--\s*repo-preflight:github-baseline\s+"
    r"last_reviewed:\s*(?P<last>\d{4}-\d{2}-\d{2})\s+"
    r"max_age_days:\s*(?P<max>\d+)\s*"
    r"-->"
)


def preferences_path(repo: Path) -> Path:
    return repo / PREFERENCES_FILENAME


def empty_preferences() -> dict[str, Any]:
    return {
        "schema": PREFERENCES_SCHEMA,
        "dismissals": {},
        "notes": (
            "dismissals は推奨・任意の再質問を抑止する。"
            "secret / 必須文書欠落 / 危険操作確認は抑止できない。"
        ),
    }


def load_preferences(repo: Path | None) -> dict[str, Any]:
    if repo is None:
        return empty_preferences()
    path = preferences_path(repo)
    if not path.is_file():
        return empty_preferences()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # 壊れた設定で fail-open にせず、空設定 + 警告相当を返す
        prefs = empty_preferences()
        prefs["load_error"] = "preferences_unreadable"
        return prefs
    if not isinstance(data, dict):
        prefs = empty_preferences()
        prefs["load_error"] = "preferences_invalid"
        return prefs
    prefs = empty_preferences()
    prefs.update({k: v for k, v in data.items() if k != "dismissals"})
    dismissals = data.get("dismissals") or {}
    if isinstance(dismissals, dict):
        prefs["dismissals"] = dismissals
    prefs["schema"] = PREFERENCES_SCHEMA
    return prefs


def save_preferences(repo: Path, preferences: dict[str, Any]) -> Path:
    path = preferences_path(repo)
    payload = {
        "schema": PREFERENCES_SCHEMA,
        "dismissals": preferences.get("dismissals") or {},
    }
    if preferences.get("notes"):
        payload["notes"] = preferences["notes"]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def dismissal_is_active(entry: dict[str, Any], *, today: date | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    mode = entry.get("mode")
    if mode == "forever":
        return True
    if mode == "snooze":
        until = entry.get("until")
        if not until:
            return False
        until_date = _parse_iso_date(str(until))
        if until_date is None:
            return False
        current = today or datetime.now(timezone.utc).date()
        return current <= until_date
    return False


def active_dismissal_ids(
    preferences: dict[str, Any], *, today: date | None = None
) -> set[str]:
    active: set[str] = set()
    for proposal_id, entry in (preferences.get("dismissals") or {}).items():
        if dismissal_is_active(entry, today=today):
            active.add(str(proposal_id))
    return active


def max_dismissal_mode(proposal: dict[str, Any]) -> str | None:
    """提案ごとに許可する最大 dismiss。None なら抑止不可。

    - forever: 次から出さない
    - 90d / 30d / 7d: 期限付き snooze のみ
    """
    kind = proposal.get("kind")
    severity = proposal.get("severity")
    if kind in NEVER_DISMISSIBLE_KINDS:
        return None
    if kind == "github_baseline":
        return "90d"
    if severity == "recommended":
        return "forever"
    if kind == "external_evidence" and severity == "required":
        # merge/publish 前の CI 未確認など。永久スキップは不可
        return "30d"
    if severity == "required":
        return None
    return "forever"


def dismissal_options_for(proposal: dict[str, Any]) -> list[dict[str, str]]:
    mode = max_dismissal_mode(proposal)
    if mode is None:
        return []
    options = [
        {
            "id": "dismiss_30d",
            "label": "30日間はこの項目を出さない",
        }
    ]
    if mode in {"90d", "forever"}:
        options.append(
            {
                "id": "dismiss_90d",
                "label": "90日間はこの項目を出さない",
            }
        )
    if mode == "forever":
        options.append(
            {
                "id": "dismiss_forever",
                "label": "次からこの項目は出さない (設定を残したまま)",
            }
        )
    return options


def apply_dismissal_options(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for proposal in proposals:
        item = dict(proposal)
        options = list(item.get("options") or [])
        dismissible = dismissal_options_for(item)
        if dismissible:
            item["dismissible"] = True
            item["max_dismissal"] = max_dismissal_mode(item)
            # 既存 options に dismiss 系が無ければ追加
            existing = {opt.get("id") for opt in options}
            for opt in dismissible:
                if opt["id"] not in existing:
                    options.append(opt)
            item["options"] = options
        else:
            item["dismissible"] = False
            item["max_dismissal"] = None
        enriched.append(item)
    return enriched


def filter_dismissed_proposals(
    proposals: list[dict[str, Any]],
    preferences: dict[str, Any],
    *,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """returns (visible, suppressed)."""
    active = active_dismissal_ids(preferences, today=today)
    visible: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for proposal in proposals:
        proposal_id = str(proposal.get("id") or "")
        # fail-closed は dismiss 記録があっても抑止しない
        if proposal_id in active and max_dismissal_mode(proposal) is not None:
            suppressed.append(
                {
                    "id": proposal_id,
                    "reason": "dismissed_in_preferences",
                    "dismissal": (preferences.get("dismissals") or {}).get(proposal_id),
                }
            )
            continue
        visible.append(proposal)
    return visible, suppressed


def record_dismissal(
    preferences: dict[str, Any],
    proposal_id: str,
    *,
    mode: str,
    reason: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    current = today or datetime.now(timezone.utc).date()
    entry: dict[str, Any] = {
        "dismissed_at": current.isoformat(),
        "reason": reason,
    }
    if mode == "forever":
        entry["mode"] = "forever"
    elif mode in {"7d", "30d", "90d"}:
        days = int(mode[:-1])
        entry["mode"] = "snooze"
        entry["until"] = (current + timedelta(days=days)).isoformat()
    elif mode.startswith("snooze_"):
        days = int(mode.split("_", 1)[1].rstrip("d"))
        entry["mode"] = "snooze"
        entry["until"] = (current + timedelta(days=days)).isoformat()
    else:
        raise ValueError(f"unknown dismissal mode: {mode}")
    prefs = dict(preferences)
    dismissals = dict(prefs.get("dismissals") or {})
    dismissals[proposal_id] = entry
    prefs["dismissals"] = dismissals
    prefs["schema"] = PREFERENCES_SCHEMA
    return prefs


def map_answer_to_dismissal_mode(answer_id: str) -> str | None:
    if answer_id == "dismiss_forever":
        return "forever"
    if answer_id == "dismiss_90d":
        return "90d"
    if answer_id == "dismiss_30d":
        return "30d"
    if answer_id == "dismiss_7d":
        return "7d"
    return None


def parse_github_baseline(text: str) -> dict[str, Any] | None:
    match = GITHUB_BASELINE_MARKER.search(text)
    if not match:
        return None
    last = _parse_iso_date(match.group("last"))
    if last is None:
        return None
    max_age = int(match.group("max"))
    return {
        "last_reviewed": last.isoformat(),
        "max_age_days": max_age,
    }


def github_baseline_status(
    baseline_path: Path,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """toolkit 同梱の GitHub 設定ガイド鮮度。

    保証: last_reviewed と max_age を機械判定し、期限切れなら対話で更新確認を出す。
    非保証: GitHub 製品変更のリアルタイム自動追従そのもの。
    """
    current = today or datetime.now(timezone.utc).date()
    if not baseline_path.is_file():
        return {
            "status": "unknown",
            "reason": "baseline_document_missing",
            "path": baseline_path.name,
        }
    try:
        text = baseline_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {
            "status": "unknown",
            "reason": "baseline_unreadable",
            "path": baseline_path.name,
        }
    parsed = parse_github_baseline(text)
    if parsed is None:
        return {
            "status": "unknown",
            "reason": "baseline_marker_missing",
            "path": baseline_path.name,
        }
    last = _parse_iso_date(parsed["last_reviewed"])
    assert last is not None
    age_days = (current - last).days
    max_age = int(parsed["max_age_days"])
    stale = age_days > max_age
    return {
        "status": "stale" if stale else "fresh",
        "last_reviewed": parsed["last_reviewed"],
        "max_age_days": max_age,
        "age_days": age_days,
        "path": baseline_path.name,
        "document": str(baseline_path),
    }


def default_github_baseline_path() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "github-settings.md"
