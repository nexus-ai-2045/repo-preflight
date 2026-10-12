"""GitHub repository Settings の読み取り専用 review gate。

設定変更は行わず、inspect -> compare -> preview までを機械可読 packet にする。
404/403/plan 制約は false と推測せず unavailable として扱う。
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

SCHEMA_VERSION = "repo-preflight.github-settings-review/v1"
PROFILES = ("solo_public", "team_public", "high_risk_public")

# readiness_scan と同じ。GIT_DIR 等で別 repository の origin を読まない。
_GIT_REPO_OVERRIDE_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_INDEX_FILE",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_isolation_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in _GIT_REPO_OVERRIDE_VARS:
        env.pop(key, None)
    return env


class ApiUnavailable(RuntimeError):
    def __init__(self, *, status_code: int | None, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def repository_from_remote(remote: str) -> str | None:
    """GitHub.com の remote URL だけを OWNER/REPO に正規化する。"""
    value = remote.strip().split("?", 1)[0].split("#", 1)[0]
    scp = re.match(r"^(?:[^@/:]+@)?([^/:]+):(.+)$", value)
    if "://" not in value and scp:
        host, path = scp.groups()
    else:
        parts = urlsplit(value)
        host = parts.hostname or ""
        path = parts.path
    if host.lower() != "github.com":
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    pieces = path.split("/")
    if len(pieces) != 2 or not all(pieces):
        return None
    return f"{pieces[0]}/{pieces[1]}"


def repository_from_repo(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        capture_output=True,
        shell=False,
        env=git_isolation_env(),
    )
    if result.returncode != 0:
        return None
    return repository_from_remote(result.stdout)


_NOT_FOUND_REASONS = {
    "Branch not protected": "branch_not_protected",
    "Branch not found": "branch_not_found",
}


def _gh_error_details(stdout: str, stderr: str) -> tuple[int | None, str | None]:
    """gh の失敗応答から HTTP status と GitHub の message を読む。

    message は既知の固定文言と照合するためだけに使い、packet へは転記しない。
    """
    message: str | None = None
    body_status: int | None = None
    try:
        body = json.loads(stdout)
    except ValueError:
        body = None
    if isinstance(body, dict):
        if isinstance(body.get("message"), str):
            message = body["message"]
        if str(body.get("status") or "").isdigit():
            body_status = int(str(body["status"]))
    if message is None:
        gh_line = re.search(r"^gh:\s*(.+?)\s*\(HTTP\s+\d{3}\)\s*$", stderr, re.M)
        message = gh_line.group(1) if gh_line else None
    status_match = re.search(r"HTTP\s+(\d{3})", stderr)
    status_code = int(status_match.group(1)) if status_match else body_status
    return status_code, message


def gh_api_get(endpoint: str) -> Any:
    """`gh api` を GET 専用で実行する。応答本文は packet に転記しない。"""
    try:
        result = subprocess.run(
            ["gh", "api", "--method", "GET", endpoint],
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            capture_output=True,
            shell=False,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise ApiUnavailable(status_code=None, reason="gh_cli_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise ApiUnavailable(status_code=None, reason="github_api_timeout") from exc
    if result.returncode != 0:
        status_code, message = _gh_error_details(result.stdout, result.stderr)
        reason = (
            _NOT_FOUND_REASONS.get(message or "", "not_found_or_plan_unavailable")
            if status_code == 404
            else (
                "forbidden_or_plan_unavailable"
                if status_code == 403
                else (
                    "authentication_required"
                    if status_code == 401
                    else "github_api_unavailable"
                )
            )
        )
        raise ApiUnavailable(status_code=status_code, reason=reason)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ApiUnavailable(
            status_code=None, reason="invalid_github_api_json"
        ) from exc


def _operation(
    method: str,
    endpoint: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"method": method, "endpoint": endpoint}
    if body is not None:
        result["body"] = body
    return result


def _setting(
    *,
    name: str,
    tier: str,
    observed: Any,
    recommended: Any,
    source: str,
    reason: str,
    effect: str,
    proposed_operation: dict[str, Any] | None,
    rollback: dict[str, Any] | str | None,
    matches: bool | None,
    unavailable_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if matches is True:
        classification = "no_change"
    elif matches is None:
        classification = "unavailable"
    elif tier == "required":
        classification = "human_decision"
    else:
        classification = "recommended_change"
    setting = {
        "name": name,
        "tier": tier,
        "observed_value": observed if matches is not None else "unknown",
        "recommended_value": recommended,
        "classification": classification,
        "reason": reason,
        "source_endpoint": source,
        "unavailable_reason": unavailable_reason,
        "external_effect": effect,
        "proposed_operation": proposed_operation if matches is False else None,
        "rollback": rollback if matches is False else None,
        "approved": False,
        "blocks_intent": tier == "required" and matches is not True,
    }
    if extra:
        setting.update(extra)
    return setting


def _fetch(
    api_get: Callable[[str], Any], endpoint: str
) -> tuple[Any, ApiUnavailable | None]:
    try:
        return api_get(endpoint), None
    except ApiUnavailable as exc:
        return None, exc
    except Exception as exc:  # injection/runtime error details may contain secrets
        return None, ApiUnavailable(
            status_code=None, reason=f"api_reader_error:{type(exc).__name__}"
        )


def _simple_setting(
    *,
    endpoint: str,
    data: dict[str, Any] | None,
    error: ApiUnavailable | None,
    field: str,
    name: str,
    tier: str,
    recommended: Any,
    method: str,
    reason: str,
    effect: str,
    body: dict[str, Any] | None = None,
    rollback_body: dict[str, Any] | None = None,
    operation: dict[str, Any] | None = None,
    rollback_operation: dict[str, Any] | None = None,
    satisfied_values: tuple[Any, ...] = (),
) -> dict[str, Any]:
    if error or data is None or field not in data:
        unavailable_reason = error.reason if error else "field_not_returned"
        return _setting(
            name=name,
            tier=tier,
            observed=None,
            recommended=recommended,
            source=endpoint,
            reason=reason,
            effect=effect,
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason=unavailable_reason,
        )
    observed = data[field]
    desired_body = body if body is not None else {field: recommended}
    old_body = rollback_body if rollback_body is not None else {field: observed}
    return _setting(
        name=name,
        tier=tier,
        observed=observed,
        recommended=recommended,
        source=endpoint,
        reason=reason,
        effect=effect,
        proposed_operation=(
            operation
            if operation is not None
            else _operation(method, endpoint, desired_body)
        ),
        rollback=(
            rollback_operation
            if rollback_operation is not None
            else _operation(method, endpoint, old_body)
        ),
        matches=observed == recommended or observed in satisfied_values,
    )


FRESH_OVERLAY_PROCEDURE = (
    "PUT の直前に fresh_read を取り直し、copy_from_fresh_read の現在値へ overlay "
    "(承認された項目の変更だけ) を重ねて 1 回だけ PUT する。同じ endpoint で承認済みの"
    "項目が複数あるときは overlay を合成して 1 回で送る。観測時の値を固定 body にしない"
)


def _fresh_overlay_put(
    endpoint: str,
    put_fields: tuple[str, ...],
    overlay: dict[str, Any],
) -> dict[str, Any]:
    """PUT が項目をまとめて置き換えるため、固定 body ではなく取り直し手順で示す。"""
    return {
        "method": "PUT",
        "endpoint": endpoint,
        "body_basis": "fresh_get_then_overlay_approved_changes_only",
        "fresh_read": {"method": "GET", "endpoint": endpoint},
        "copy_from_fresh_read": [name for name in put_fields if name not in overlay],
        "overlay": dict(overlay),
        "put_once": True,
        "procedure": FRESH_OVERLAY_PROCEDURE,
    }


def _complete_put_setting(
    *,
    endpoint: str,
    data: dict[str, Any] | None,
    error: ApiUnavailable | None,
    field: str,
    name: str,
    tier: str,
    recommended: Any,
    required_fields: tuple[str, ...],
    preserved_fields: tuple[str, ...],
    reason: str,
    effect: str,
    satisfied_values: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """必須fieldを取り直したうえで、一項目だけを重ねるPUT手順を作る。"""
    missing = [key for key in required_fields if data is None or key not in data]
    effective_error = error
    if effective_error is None and missing:
        effective_error = ApiUnavailable(
            status_code=None,
            reason="required_put_field_not_returned:" + ",".join(missing),
        )
    if effective_error is not None or data is None:
        return _simple_setting(
            endpoint=endpoint,
            data=data,
            error=effective_error,
            field=field,
            name=name,
            tier=tier,
            recommended=recommended,
            method="PUT",
            reason=reason,
            effect=effect,
        )
    observed_overlay = {field: data[field]} if field in data else {}
    return _simple_setting(
        endpoint=endpoint,
        data=data,
        error=None,
        field=field,
        name=name,
        tier=tier,
        recommended=recommended,
        method="PUT",
        reason=reason,
        effect=effect,
        operation=_fresh_overlay_put(endpoint, preserved_fields, {field: recommended}),
        rollback_operation=_fresh_overlay_put(
            endpoint, preserved_fields, observed_overlay
        ),
        satisfied_values=satisfied_values,
    )


def _security_setting(
    *,
    repository: str,
    root: dict[str, Any] | None,
    error: ApiUnavailable | None,
    name: str,
    tier: str,
    reason: str,
    effect: str,
) -> dict[str, Any]:
    endpoint = f"repos/{repository}"
    if error:
        return _setting(
            name=name,
            tier=tier,
            observed=None,
            recommended="enabled",
            source=endpoint,
            reason=reason,
            effect=effect,
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason=error.reason,
        )
    security = (root or {}).get("security_and_analysis") or {}
    observed = (security.get(name) or {}).get("status")
    if observed not in {"enabled", "disabled"}:
        return _setting(
            name=name,
            tier=tier,
            observed=None,
            recommended="enabled",
            source=endpoint,
            reason=reason,
            effect=effect,
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason="security_setting_not_returned",
        )
    body = {"security_and_analysis": {name: {"status": "enabled"}}}
    rollback = {"security_and_analysis": {name: {"status": observed}}}
    return _setting(
        name=name,
        tier=tier,
        observed=observed,
        recommended="enabled",
        source=endpoint,
        reason=reason,
        effect=effect,
        proposed_operation=_operation("PATCH", endpoint, body),
        rollback=_operation("PATCH", endpoint, rollback),
        matches=observed == "enabled",
    )


MAX_WORKFLOW_FILES = 50
RULESETS_PAGE_SIZE = 100
LOCAL_ACTION_PREFIXES = ("./", "$/")
GITHUB_OWNED_ACTION_OWNERS = frozenset({"actions", "github"})
PR_CREATING_ACTIONS = (
    "googleapis/release-please-action",
    "google-github-actions/release-please-action",
    "peter-evans/create-pull-request",
)
SELECTED_ACTIONS_FIELDS = (
    "github_owned_allowed",
    "verified_allowed",
    "patterns_allowed",
)
ACTIONS_PERMISSION_FIELDS = ("enabled", "allowed_actions", "sha_pinning_required")
WORKFLOW_PERMISSION_FIELDS = (
    "default_workflow_permissions",
    "can_approve_pull_request_reviews",
)
CAN_APPROVE_EFFECT = (
    "GitHub ActionsからのPR review承認を禁止する。GITHUB_TOKENによるPRの作成も"
    "止まるため、release-pleaseなどがGITHUB_TOKENでPRを作っているrepositoryでは、"
    "ONを維持して理由を記録するか、GitHub App tokenへ移してからOFFにする"
)

_YAML_KEY = re.compile(r"^( *)(- +)?([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]+(.*))?$")
_NON_LF_LINE_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
_YAML_ALIAS = re.compile(r"(?:^|[\s\[{,])\*[A-Za-z0-9_-]")
_ACTION_REFERENCE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"(?P<path>(?:/[^@\s]+)?)@(?P<ref>\S+)$"
)
_WORKFLOW_FILE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


_QUOTES = ("'", '"')


def _closing_quote(value: str) -> int | None:
    quote_char = value[0]
    index = 1
    while index < len(value):
        char = value[index]
        if quote_char == '"' and char == "\\":
            index += 2
            continue
        if char == quote_char:
            if quote_char == "'" and value[index + 1 : index + 2] == "'":
                index += 2
                continue
            return index
        index += 1
    return None


def _value_token(value: str) -> tuple[str, str] | None:
    """key の値を (種類, 本文) に分ける。引用符が閉じない等、読めない値は None。"""
    value = value.strip()
    if not value or value.startswith("#"):
        return "empty", ""
    first = value[0]
    if first in _QUOTES:
        end = _closing_quote(value)
        if end is None:
            return None
        rest = value[end + 1 :].strip()
        if rest and not rest.startswith("#"):
            return None
        inner = value[1:end]
        return "quoted", inner.replace("''", "'") if first == "'" else inner
    if first in "|>":
        return "block", ""
    plain = re.split(r"(?:^|\s)#", value, maxsplit=1)[0].strip()
    if first in "&!*":
        return "anchor", plain
    if first in "{[":
        return "flow", plain
    return "plain", plain


def _flow_balanced(text: str) -> bool:
    return text.count("{") == text.count("}") and text.count("[") == text.count("]")


def workflow_action_references(text: str) -> list[str] | None:
    """workflow YAML の `jobs` 配下にある `uses:` の値を、出現順に返す。

    stdlib だけで安全に追える構文だけを読む。`jobs` の下を
    job id、job の key、step の key の 3 段として追い、読み飛ばしは各段の key
    だけに効かせる。追えない構文(インデントの tab、alias、anchor、flow 形式、
    閉じない引用符、block scalar の `uses` など)に当たったら、推測せず None を返す。
    最上位の `jobs` を一度も見なかった場合(文書全体の字下げ、`jobs` の無い文書)と、
    LF 以外で行を切る文字(CR 単独、NEL、U+2028 など)を含む場合も None を返す。
    """
    normalized = text.replace("\r\n", "\n")
    if any(char in normalized for char in _NON_LF_LINE_BREAKS):
        return None
    references: list[str] = []
    started = False
    jobs_seen = False
    in_jobs = False
    job_col: int | None = None
    child_col: int | None = None
    step_col: int | None = None
    in_steps = False
    block_col: int | None = None
    skip_col: int | None = None
    scalar_col: int | None = None
    for raw in normalized.split("\n"):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block_col is not None:
            if indent > block_col:
                continue
            block_col = None
        if "\t" in raw[: len(raw) - len(raw.lstrip())] or _YAML_ALIAS.search(stripped):
            return None
        if indent == 0 and stripped in {"---", "..."}:
            if started or stripped == "...":
                return None
            continue
        match = _YAML_KEY.match(raw.rstrip())
        if match is None:
            if indent == 0:
                return None
            if not in_jobs:
                continue
            sequence_entry = stripped == "-" or stripped.startswith("- ")
            if skip_col is not None and (
                indent > skip_col or (indent == skip_col and sequence_entry)
            ):
                continue
            if scalar_col is not None and indent > scalar_col:
                continue
            return None
        started = True
        dash = match.group(2)
        key_col = indent + len(dash or "")
        key = match.group(3)
        token = _value_token(match.group(4) or "")
        if token is None:
            return None
        kind, value = token
        scalar_col = None
        if indent == 0 and dash is None:
            in_jobs = key == "jobs"
            if in_jobs and kind != "empty":
                return None
            jobs_seen = jobs_seen or in_jobs
            job_col = child_col = step_col = skip_col = None
            in_steps = False
            if kind == "block":
                block_col = 0
            continue
        if not in_jobs:
            if kind == "block":
                block_col = key_col
            continue
        if kind == "anchor":
            return None
        if skip_col is not None:
            if key_col > skip_col:
                if kind == "block":
                    block_col = key_col
                continue
            skip_col = None
        if job_col is None:
            if dash is not None or kind != "empty":
                return None
            job_col = key_col
            continue
        if key_col < job_col:
            return None
        if key_col == job_col and dash is None:
            if kind != "empty":
                return None
            child_col = step_col = None
            in_steps = False
            continue
        if dash is not None:
            if not in_steps or child_col is None or indent < child_col:
                return None
            step_col = key_col
            at_job_level = False
        elif child_col is None:
            child_col = key_col
            at_job_level = True
        elif key_col == child_col:
            in_steps = False
            at_job_level = True
        elif in_steps and step_col is not None and key_col == step_col:
            at_job_level = False
        else:
            return None
        if kind == "flow" and (
            not _flow_balanced(value) or key == "steps" or "uses" in value
        ):
            return None
        if key == "uses":
            if kind not in {"plain", "quoted"} or not value or "\\" in value:
                return None
            references.append(value)
        elif at_job_level and key == "steps":
            if kind != "empty":
                return None
            in_steps = True
            step_col = None
        elif kind == "empty":
            skip_col = key_col
        elif kind == "block":
            block_col = key_col
        elif kind == "plain":
            scalar_col = key_col
    return references if jobs_seen else None


def derive_selected_actions(references: list[str]) -> dict[str, Any]:
    """workflow が実際に使う action から selected-actions の許可 list を導く。"""
    patterns: set[str] = set()
    third_party: set[str] = set()
    used: set[str] = set()
    unmapped: set[str] = set()
    local = 0
    github_owned = False
    for reference in references:
        if reference.startswith(LOCAL_ACTION_PREFIXES):
            local += 1
            continue
        match = (
            None
            if reference.startswith("docker://")
            else _ACTION_REFERENCE.match(reference)
        )
        if match is None:
            unmapped.add(reference)
            continue
        used.add(reference)
        if match["owner"].lower() in GITHUB_OWNED_ACTION_OWNERS:
            github_owned = True
        else:
            third_party.add(reference)
            patterns.add(f"{match['owner']}/{match['repo']}{match['path']}@*")
    return {
        "github_owned_used": github_owned,
        "patterns_allowed": sorted(patterns),
        "third_party_references": sorted(third_party),
        "used_references": sorted(used),
        "local_references": local,
        "unmapped_references": sorted(unmapped),
    }


NEGATION_UNCERTAIN_CHARS = "?+["


def _glob_tokens(pattern: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            tokens.append(("any", ""))
            while pattern.startswith("*", index):
                index += 1
        elif pattern[index] == "*":
            tokens.append(("segment", ""))
            index += 1
        else:
            tokens.append(("char", pattern[index]))
            index += 1
    return tokens


def _glob_match(
    pattern: str,
    text: str,
    *,
    star_crosses_slash: bool = False,
    ignore_case: bool = False,
) -> bool:
    """`*`(`/` を越えない)と `**`(越える)だけを持つ glob を、戻らずに照合する。

    正規表現へ直すと、`*` の多い pattern で照合が指数的に遅くなるため、位置の集合を
    1 文字ずつ進める。
    """
    if ignore_case:
        pattern, text = pattern.lower(), text.lower()
    tokens = _glob_tokens(pattern)

    def settle(states: set[int]) -> set[int]:
        pending = list(states)
        while pending:
            position = pending.pop()
            if (
                position < len(tokens)
                and tokens[position][0] != "char"
                and position + 1 not in states
            ):
                states.add(position + 1)
                pending.append(position + 1)
        return states

    states = settle({0})
    for char in text:
        following: set[int] = set()
        for position in states:
            if position >= len(tokens):
                continue
            kind, literal = tokens[position]
            if kind == "char":
                if literal == char:
                    following.add(position + 1)
            elif kind == "any" or star_crosses_slash or char != "/":
                following.add(position)
        states = settle(following)
        if not states:
            return False
    return len(tokens) in states


def _negation_matches(pattern: str, reference: str) -> bool:
    """拒否 pattern は、読み違えると許可側へ倒れるので、広く読む。"""
    if any(char in pattern for char in NEGATION_UNCERTAIN_CHARS):
        return True
    return _glob_match(pattern, reference, star_crosses_slash=True, ignore_case=True)


def allow_list_covers(patterns: list[str], reference: str) -> tuple[bool, list[str]]:
    """許可 list の pattern が workflow の参照を覆うかを、保守的に判定する。

    許可側の pattern は、`*` が `/` を越えず、大文字と小文字を区別する(覆っていると
    誤って言うより、足りないと言う方を選ぶ)。`!` の拒否 pattern は逆に、`*` が `/` も
    越え、大文字と小文字を区別せず、`?` `+` `[` を含めば一致とみなす。拒否は順序に
    関係なく優先する。戻り値は (覆うか, 一致した拒否 pattern)。
    """
    allowed = False
    conflicts: list[str] = []
    for pattern in patterns:
        if pattern.startswith("!"):
            if _negation_matches(pattern[1:], reference) and pattern not in conflicts:
                conflicts.append(pattern)
        elif _glob_match(pattern, reference):
            allowed = True
    return (allowed and not conflicts), conflicts


def _overly_broad(pattern: str) -> bool:
    """owner の部分に `*` を含む、または `**` を含む pattern は、広すぎるとみなす。"""
    return "**" in pattern or "*" in pattern.split("/", 1)[0]


def _decode_workflow(item: Any) -> str | None:
    if (
        not isinstance(item, dict)
        or item.get("encoding") != "base64"
        or not isinstance(item.get("content"), str)
    ):
        return None
    try:
        return base64.b64decode(item["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _read_workflow_references(
    api_get: Callable[[str], Any], repository: str, branch: str | None
) -> dict[str, Any]:
    def unavailable(reason: str) -> dict[str, Any]:
        return {"state": "unavailable", "reason": reason}

    if not branch:
        return unavailable("default_branch_unknown")
    ref = quote(branch, safe="")
    listing, error = _fetch(
        api_get, f"repos/{repository}/contents/.github/workflows?ref={ref}"
    )
    if error is not None:
        if error.status_code == 404:
            return {"state": "ok", "files": [], "references": []}
        return unavailable(error.reason)
    if not isinstance(listing, list):
        return unavailable("workflow_listing_invalid")
    names = sorted(
        item["name"]
        for item in listing
        if isinstance(item, dict)
        and item.get("type") == "file"
        and isinstance(item.get("name"), str)
        and item["name"].endswith((".yml", ".yaml"))
    )
    if len(names) > MAX_WORKFLOW_FILES:
        return unavailable("too_many_workflow_files")
    if not all(_WORKFLOW_FILE_NAME.fullmatch(name) for name in names):
        return unavailable("workflow_file_name_invalid")
    files: list[str] = []
    references: list[tuple[str, str]] = []
    for name in names:
        path = f".github/workflows/{name}"
        item, error = _fetch(api_get, f"repos/{repository}/contents/{path}?ref={ref}")
        if error is not None:
            return unavailable(error.reason)
        text = _decode_workflow(item)
        if text is None:
            return unavailable(f"workflow_content_unreadable:{path}")
        found = workflow_action_references(text)
        if found is None:
            return unavailable(f"workflow_scan_refused:{path}")
        files.append(path)
        references.extend((path, reference) for reference in found)
    return {"state": "ok", "files": files, "references": references}


class _WorkflowEvidence:
    """default branch の workflow 読み取りを 1 回の review で 1 度だけ行う。"""

    def __init__(
        self, api_get: Callable[[str], Any], repository: str, branch: str | None
    ) -> None:
        self._args = (api_get, repository, branch)
        self._result: dict[str, Any] | None = None

    def read(self) -> dict[str, Any]:
        if self._result is None:
            self._result = _read_workflow_references(*self._args)
        return self._result

    def allow_list(self) -> dict[str, Any]:
        result = self.read()
        if result["state"] != "ok":
            return {"state": "unavailable", "reason": result["reason"]}
        derived = derive_selected_actions(
            [reference for _, reference in result["references"]]
        )
        return {"state": "ok", "workflow_files": result["files"], **derived}


def _switch_to_selected_operation(
    actions_endpoint: str,
    selected_endpoint: str,
    derivation: dict[str, Any],
) -> dict[str, Any]:
    first = _fresh_overlay_put(
        actions_endpoint, ACTIONS_PERMISSION_FIELDS, {"allowed_actions": "selected"}
    )
    review_reasons: list[str] = []
    if derivation["state"] == "ok":
        if derivation["unmapped_references"]:
            review_reasons.append(
                f"unmapped_references:{len(derivation['unmapped_references'])}"
            )
        if derivation["local_references"]:
            review_reasons.append(
                f"local_actions_not_scanned:{derivation['local_references']}"
            )
        second: dict[str, Any] = {
            "method": "PUT",
            "endpoint": selected_endpoint,
            "body": {
                "github_owned_allowed": True,
                "verified_allowed": False,
                "patterns_allowed": derivation["patterns_allowed"],
            },
            "body_basis": "derived_from_default_branch_workflows",
            "derivation": {
                key: derivation[key]
                for key in (
                    "workflow_files",
                    "github_owned_used",
                    "local_references",
                    "unmapped_references",
                )
            },
            "note": (
                "allowed_actions が all の間は現在値が無いため、workflow から導いた"
                "許可 list をそのまま設定する"
            ),
        }
    else:
        second = {
            "method": "PUT",
            "endpoint": selected_endpoint,
            "body": None,
            "body_basis": "derivation_unavailable",
            "derivation": {"reason": derivation["reason"]},
        }
    operation: dict[str, Any] = {
        "method": "SEQUENCE",
        "order_reason": (
            "allowed_actions が all の間は selected-actions endpoint が 409 を返すため、"
            "selected へ切り替えてから許可 list を設定する"
        ),
        "steps": [first, second],
        "ready": derivation["state"] == "ok" and not review_reasons,
    }
    if derivation["state"] != "ok":
        operation["blocked_reason"] = (
            f"allow_list_derivation_unavailable:{derivation['reason']}"
        )
    elif review_reasons:
        operation["blocked_reason"] = "allow_list_needs_review:" + ",".join(
            review_reasons
        )
    return operation


def _github_token_pr_creation_exception(evidence: _WorkflowEvidence) -> dict[str, Any]:
    result = evidence.read()
    not_detected = (
        "run step の gh pr create、REST API の直接呼び出し、composite action 内の"
        "PR 作成は検出しない"
    )
    if result["state"] == "ok":
        found: dict[str, set[str]] = {}
        for path, reference in result["references"]:
            match = _ACTION_REFERENCE.match(reference)
            name = f"{match['owner']}/{match['repo']}".lower() if match else None
            if name in PR_CREATING_ACTIONS:
                found.setdefault(name, set()).add(path)
        detection: dict[str, Any] = {
            "state": "ok",
            "source": "default branch の .github/workflows の uses",
            "detected_actions": [
                {"action": action, "workflow_files": sorted(paths)}
                for action, paths in sorted(found.items())
            ],
            "not_detected": not_detected,
        }
    else:
        detection = {
            "state": "unavailable",
            "reason": result["reason"],
            "not_detected": not_detected,
        }
    return {
        "id": "github_token_pull_request_creation",
        "applies_when": (
            "workflow が GITHUB_TOKEN で PR を作成している "
            "(release-please / create-pull-request など)"
        ),
        "why": (
            "この設定は GITHUB_TOKEN による PR の承認だけでなく作成も許可するため、"
            "OFF にすると PR を作成する workflow が止まる"
        ),
        "choices": [
            {
                "id": "keep_on_and_record_reason",
                "effect": "ON を維持し、理由を判断記録へ残す",
            },
            {
                "id": "migrate_to_github_app_token_then_off",
                "effect": "PR 作成を GitHub App token へ移し、動作を確認してから OFF にする",
            },
        ],
        "detection": detection,
    }


MAX_MERGED_PRS_FOR_CHECK_EVIDENCE = 5
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
CLASSIC_SOURCE = "classic_branch_protection"


@dataclass(frozen=True)
class _ProtectionSources:
    rulesets: tuple[dict[str, Any], ...]
    ruleset_unavailable: str | None
    classic: dict[str, Any] | None
    classic_state: str
    classic_unavailable: str | None
    classic_endpoint: str | None

    def candidate_endpoints(self, repository: str) -> list[str]:
        endpoints = [
            f"repos/{repository}/rulesets/{item['id']}" for item in self.rulesets
        ]
        if self.classic_state == "present" and self.classic_endpoint:
            endpoints.append(self.classic_endpoint)
        return endpoints

    @property
    def unavailable(self) -> list[str]:
        sources = []
        if self.ruleset_unavailable:
            sources.append("rulesets")
        if self.classic_state == "unavailable":
            sources.append(CLASSIC_SOURCE)
        return sources

    def unavailable_detail(self, labels: list[str]) -> str:
        reasons = {
            "rulesets": self.ruleset_unavailable,
            CLASSIC_SOURCE: self.classic_unavailable,
        }
        return ";".join(
            f"{label}:{reasons.get(label) or 'field_not_returned'}" for label in labels
        )


def _read_rulesets(
    repository: str, api_get: Callable[[str], Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """default branch を対象にした active な ruleset の詳細と、読めなかった理由を返す。

    対象 branch の解釈(`~DEFAULT_BRANCH` 以外の include、exclude、glob)は
    ここでは行わない。解釈できない ruleset は数えず、理由に残す。
    """
    summaries, list_error = _fetch(
        api_get, f"repos/{repository}/rulesets?per_page={RULESETS_PAGE_SIZE}"
    )
    if list_error is not None:
        return [], list_error.reason
    if not isinstance(summaries, list):
        return [], "invalid_rulesets_response"
    if len(summaries) >= RULESETS_PAGE_SIZE:
        return [], "rulesets_listing_may_be_truncated"
    details: list[dict[str, Any]] = []
    reasons: list[str] = []
    for summary in summaries:
        ruleset_id = summary.get("id") if isinstance(summary, dict) else None
        if ruleset_id is None:
            continue
        detail, error = _fetch(api_get, f"repos/{repository}/rulesets/{ruleset_id}")
        if error is not None:
            if error.reason not in reasons:
                reasons.append(error.reason)
        elif isinstance(detail, dict):
            details.append(detail)
    candidates: list[dict[str, Any]] = []
    not_interpreted: list[str] = []
    for item in details:
        if item.get("target") != "branch" or item.get("enforcement") != "active":
            continue
        ref_name = (item.get("conditions") or {}).get("ref_name") or {}
        include = ref_name.get("include")
        if (
            isinstance(include, list)
            and all(isinstance(entry, str) for entry in include)
            and set(include) == {"~DEFAULT_BRANCH"}
            and not ref_name.get("exclude")
        ):
            candidates.append(item)
        else:
            not_interpreted.append(str(item.get("id")))
    if not_interpreted:
        reasons.append("ruleset_scope_not_interpreted:" + ",".join(not_interpreted))
    return candidates, ";".join(reasons) or None


def _read_classic_protection(
    repository: str, api_get: Callable[[str], Any], default_branch: str | None
) -> tuple[dict[str, Any] | None, str, str | None, str | None]:
    if not default_branch:
        return None, "unavailable", "default_branch_unknown", None
    endpoint = (
        f"repos/{repository}/branches/{quote(default_branch, safe='/')}/protection"
    )
    data, error = _fetch(api_get, endpoint)
    if error is not None:
        if error.reason == "branch_not_protected":
            return None, "absent", None, endpoint
        return None, "unavailable", error.reason, endpoint
    if not isinstance(data, dict):
        return None, "unavailable", "invalid_classic_protection_response", endpoint
    return data, "present", None, endpoint


def _verdict(
    sources: _ProtectionSources,
    ruleset_has: Callable[[dict[str, Any]], bool],
    classic_flag: Callable[[dict[str, Any]], bool | None],
) -> dict[str, list[str]]:
    enforced = [
        f"ruleset:{item.get('id')}" for item in sources.rulesets if ruleset_has(item)
    ]
    unavailable = [source for source in sources.unavailable if source != CLASSIC_SOURCE]
    if sources.classic_state == "present":
        flag = classic_flag(sources.classic or {})
        if flag is True:
            enforced.append(CLASSIC_SOURCE)
        elif flag is None:
            unavailable.append(CLASSIC_SOURCE)
    elif sources.classic_state == "unavailable":
        unavailable.append(CLASSIC_SOURCE)
    return {"enforced_by": enforced, "sources_unavailable": unavailable}


def _verdict_matches(verdict: dict[str, list[str]]) -> bool | None:
    if verdict["enforced_by"]:
        return True
    return None if verdict["sources_unavailable"] else False


def _verdict_extra(verdict: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        "enforced_by": verdict["enforced_by"],
        "sources_unavailable": verdict["sources_unavailable"],
    }


def _rules(item: dict[str, Any], rule_type: str) -> list[dict[str, Any]]:
    return [
        rule
        for rule in item.get("rules") or []
        if isinstance(rule, dict) and rule.get("type") == rule_type
    ]


def _rule_params(rule: dict[str, Any]) -> dict[str, Any]:
    params = rule.get("parameters")
    return params if isinstance(params, dict) else {}


def _classic_enabled(classic: dict[str, Any], key: str) -> bool | None:
    node = classic.get(key)
    if isinstance(node, dict) and isinstance(node.get("enabled"), bool):
        return node["enabled"]
    return None


def _classic_not_allowed(classic: dict[str, Any], key: str) -> bool | None:
    allowed = _classic_enabled(classic, key)
    return None if allowed is None else not allowed


def _classic_status_checks(classic: dict[str, Any]) -> dict[str, Any]:
    node = classic.get("required_status_checks")
    return node if isinstance(node, dict) else {}


def _classic_contexts(classic: dict[str, Any]) -> set[str]:
    node = _classic_status_checks(classic)
    names = {str(name) for name in node.get("contexts") or [] if name}
    names.update(
        str(item.get("context"))
        for item in node.get("checks") or []
        if isinstance(item, dict) and item.get("context")
    )
    return names


def _classic_reviews(classic: dict[str, Any]) -> dict[str, Any] | None:
    node = classic.get("required_pull_request_reviews")
    return node if isinstance(node, dict) else None


def _classic_strict(classic: dict[str, Any]) -> bool:
    return _classic_status_checks(classic).get("strict") is True


def _check_names(check_runs: Any, statuses: Any) -> set[str]:
    names = {
        str(item.get("name"))
        for item in (check_runs or {}).get("check_runs") or []
        if isinstance(item, dict) and item.get("name")
    }
    names.update(
        str(item.get("context"))
        for item in (statuses or {}).get("statuses") or []
        if isinstance(item, dict) and item.get("context")
    )
    return names


def _merged_pr_check_evidence(
    api_get: Callable[[str], Any],
    repository: str,
    default_branch: str | None,
    required: set[str],
) -> tuple[set[str], list[dict[str, Any]], list[str]]:
    """直近にmergeされたPRのhead commitが出したcheck名を集める。

    PRでしか走らないcheckがあるため、default branchのHEADは根拠にしない。
    """
    if not default_branch:
        return set(), [], ["default_branch_unknown"]
    pulls, error = _fetch(
        api_get,
        f"repos/{repository}/pulls?state=closed&base={quote(default_branch, safe='')}"
        "&sort=updated&direction=desc&per_page=30",
    )
    if error is not None:
        return set(), [], [error.reason]
    if not isinstance(pulls, list):
        return set(), [], ["invalid_pull_list_response"]
    merged = sorted(
        (
            pull
            for pull in pulls
            if isinstance(pull, dict)
            and isinstance(pull.get("merged_at"), str)
            and pull["merged_at"]
            and isinstance(pull.get("head"), dict)
            and isinstance(pull["head"].get("sha"), str)
            and _FULL_SHA.fullmatch(pull["head"]["sha"])
        ),
        key=lambda pull: pull["merged_at"],
        reverse=True,
    )[:MAX_MERGED_PRS_FOR_CHECK_EVIDENCE]
    if not merged:
        return set(), [], ["no_recent_merged_pull_request"]
    observed: set[str] = set()
    evidence: list[dict[str, Any]] = []
    errors: list[str] = []
    for pull in merged:
        sha = pull["head"]["sha"]
        evidence.append({"number": pull.get("number"), "head_sha": sha})
        check_runs, runs_error = _fetch(
            api_get, f"repos/{repository}/commits/{sha}/check-runs?per_page=100"
        )
        statuses, statuses_error = _fetch(
            api_get, f"repos/{repository}/commits/{sha}/status?per_page=100"
        )
        errors.extend(
            item.reason for item in (runs_error, statuses_error) if item is not None
        )
        observed.update(
            _check_names(
                None if runs_error else check_runs,
                None if statuses_error else statuses,
            )
        )
        if required <= observed:
            break
    return observed, evidence, errors


def _branch_protection_observations(
    repository: str,
    profile: str,
    api_get: Callable[[str], Any],
    default_branch: str | None,
) -> list[dict[str, Any]]:
    """rulesetとclassic branch protectionを累積して評価する。

    GitHubは両方が掛かると全ての規則を強制し、同じ規則は厳しい方が効く。
    """
    rulesets_endpoint = f"repos/{repository}/rulesets"
    rulesets, ruleset_unavailable = _read_rulesets(repository, api_get)
    classic, classic_state, classic_unavailable, classic_endpoint = (
        _read_classic_protection(repository, api_get, default_branch)
    )
    sources = _ProtectionSources(
        rulesets=tuple(rulesets),
        ruleset_unavailable=ruleset_unavailable,
        classic=classic,
        classic_state=classic_state,
        classic_unavailable=classic_unavailable,
        classic_endpoint=classic_endpoint,
    )
    source = ";".join(item for item in (rulesets_endpoint, classic_endpoint) if item)
    endpoints = sources.candidate_endpoints(repository)

    def protection_setting(
        name: str,
        tier: str,
        observed: Any,
        recommended: Any,
        matches: bool | None,
        reason: str,
        effect: str = "default branch の更新条件を変更",
        unavailable_reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _setting(
            name=name,
            tier=tier,
            observed=observed,
            recommended=recommended,
            source=source,
            reason=reason,
            effect=effect,
            proposed_operation=(
                {
                    "method": "REVIEW_THEN_PUT",
                    "candidate_endpoints": endpoints,
                    "body_basis": "fresh_all_effective_protection_bodies_required",
                    "change": {name: recommended},
                }
                if endpoints
                else None
            ),
            rollback=(
                {
                    "requirement": "capture_each_fresh_protection_body_before_change",
                    "candidate_endpoints": endpoints,
                }
                if endpoints
                else None
            ),
            matches=matches,
            unavailable_reason=(
                (
                    unavailable_reason
                    or sources.unavailable_detail(sources.unavailable)
                    or "unavailable_without_reason"
                )
                if matches is None
                else None
            ),
            extra=extra,
        )

    base = _verdict(sources, lambda item: True, lambda classic: True)
    base_matches = _verdict_matches(base)
    base_setting = protection_setting(
        "default_branch_ruleset",
        "required",
        ("active" if sources.rulesets else CLASSIC_SOURCE) if base_matches else None,
        "ruleset_or_classic_branch_protection_on_default_branch",
        base_matches,
        "default branch を ruleset または classic branch protection で保護する",
        "default branch の変更経路を制限",
        extra=_verdict_extra(base),
    )
    if base_matches is False:
        return [base_setting]

    def family(
        name: str,
        reason: str,
        ruleset_has: Callable[[dict[str, Any]], bool],
        classic_flag: Callable[[dict[str, Any]], bool | None],
        tier: str = "required",
    ) -> dict[str, Any]:
        verdict = _verdict(sources, ruleset_has, classic_flag)
        matches = _verdict_matches(verdict)
        return protection_setting(
            name,
            tier,
            bool(verdict["enforced_by"]),
            True,
            matches,
            reason,
            unavailable_reason=sources.unavailable_detail(
                verdict["sources_unavailable"]
            ),
            extra=_verdict_extra(verdict),
        )

    def thread_resolution(item: dict[str, Any]) -> bool:
        return any(
            _rule_params(rule).get("required_review_thread_resolution") is True
            for rule in _rules(item, "pull_request")
        )

    def strict_policy(item: dict[str, Any]) -> bool:
        return any(
            _rule_params(rule).get("strict_required_status_checks_policy") is True
            for rule in _rules(item, "required_status_checks")
        )

    pull_request = family(
        "ruleset_pull_request",
        "default branch への変更をPR経由にする",
        lambda item: bool(_rules(item, "pull_request")),
        lambda classic: _classic_reviews(classic) is not None,
        tier="recommended",
    )

    approvals_values: list[int] = [
        int(_rule_params(rule).get("required_approving_review_count") or 0)
        for item in rulesets
        for rule in _rules(item, "pull_request")
    ]
    reviews = _classic_reviews(classic or {})
    if reviews is not None:
        approvals_values.append(
            int(reviews.get("required_approving_review_count") or 0)
        )
    approvals = max(approvals_values or [0])
    minimum = 0 if profile == "solo_public" else 1
    approvals_matches: bool | None = (
        True
        if approvals >= minimum and (approvals_values or not sources.unavailable)
        else (None if sources.unavailable else False)
    )

    required_contexts = {
        str(entry.get("context"))
        for item in rulesets
        for rule in _rules(item, "required_status_checks")
        for entry in _rule_params(rule).get("required_status_checks") or []
        if isinstance(entry, dict) and entry.get("context")
    }
    if classic_state == "present":
        required_contexts |= _classic_contexts(classic or {})
    checks_reason: str | None = None
    observed_names: set[str] = set()
    evidence: list[dict[str, Any]] = []
    checks_matches: bool | None
    if not required_contexts:
        checks_matches = None if sources.unavailable else False
    else:
        observed_names, evidence, evidence_errors = _merged_pr_check_evidence(
            api_get, repository, default_branch, required_contexts
        )
        if required_contexts <= observed_names:
            checks_matches = None if sources.unavailable else True
            checks_reason = (
                sources.unavailable_detail(sources.unavailable)
                if sources.unavailable
                else None
            )
        elif evidence_errors:
            checks_matches = None
            checks_reason = ";".join(sorted(set(evidence_errors)))
        else:
            checks_matches = False
    checks_observed: dict[str, Any] = {
        "required": sorted(required_contexts),
        "observed_on_recent_merged_pr_heads": sorted(observed_names),
        "evidence_pull_requests": evidence,
    }
    if sources.unavailable:
        checks_observed["sources_unavailable"] = sources.unavailable

    bypass_actors: list[dict[str, Any]] = []
    bypass_unavailable = list(sources.unavailable)
    bypass_reasons = (
        [sources.unavailable_detail(sources.unavailable)] if bypass_unavailable else []
    )
    for item in rulesets:
        if "bypass_actors" not in item:
            bypass_reasons.append("rulesets:bypass_actors_not_returned")
            if "rulesets" not in bypass_unavailable:
                bypass_unavailable.append("rulesets")
        for actor in item.get("bypass_actors") or []:
            if not isinstance(actor, dict):
                continue
            bypass_actors.append(
                {
                    "ruleset_id": item.get("id"),
                    "actor_type": actor.get("actor_type"),
                    "actor_id": actor.get("actor_id"),
                    "bypass_mode": actor.get("bypass_mode"),
                }
            )
    if classic_state == "present":
        admins_enforced = _classic_enabled(classic or {}, "enforce_admins")
        if admins_enforced is None:
            if CLASSIC_SOURCE not in bypass_unavailable:
                bypass_unavailable.append(CLASSIC_SOURCE)
                bypass_reasons.append(f"{CLASSIC_SOURCE}:field_not_returned")
        elif admins_enforced is False:
            bypass_actors.append(
                {"source": CLASSIC_SOURCE, "actor": "repository_administrators"}
            )
        allowances = (reviews or {}).get("bypass_pull_request_allowances") or {}
        for kind, label, key in (
            ("users", "user", "login"),
            ("teams", "team", "slug"),
            ("apps", "app", "slug"),
        ):
            for entry in allowances.get(kind) or []:
                if isinstance(entry, dict) and entry.get(key):
                    bypass_actors.append(
                        {"source": CLASSIC_SOURCE, "actor": f"{label}:{entry[key]}"}
                    )
    bypass_matches: bool | None = (
        False if bypass_actors else (None if bypass_unavailable else True)
    )
    bypass_tier = "required" if profile == "high_risk_public" else "recommended"

    return [
        base_setting,
        family(
            "ruleset_deletion_protection",
            "default branch の削除を禁止する",
            lambda item: bool(_rules(item, "deletion")),
            lambda classic: _classic_not_allowed(classic, "allow_deletions"),
        ),
        family(
            "ruleset_non_fast_forward_protection",
            "force push を禁止する",
            lambda item: bool(_rules(item, "non_fast_forward")),
            lambda classic: _classic_not_allowed(classic, "allow_force_pushes"),
        ),
        pull_request,
        family(
            "required_review_thread_resolution",
            "未解決review threadを残したmergeを防ぐ",
            thread_resolution,
            lambda classic: _classic_enabled(
                classic, "required_conversation_resolution"
            ),
            tier="recommended",
        ),
        protection_setting(
            "required_status_checks",
            "required",
            checks_observed,
            "all_required_check_contexts_emitted_on_recent_merged_pr_heads",
            checks_matches,
            "required check名を直近にmergeされたPRのhead commitのcheck/statusと照合する",
            unavailable_reason=checks_reason,
        ),
        family(
            "strict_required_status_checks_policy",
            "最新baseとの整合を確認してからmergeする",
            strict_policy,
            _classic_strict,
            tier="recommended",
        ),
        protection_setting(
            "required_approving_review_count",
            "recommended",
            approvals,
            {"minimum": minimum},
            approvals_matches,
            "maintainer人数に応じたreview承認数を要求する",
        ),
        _setting(
            name="ruleset_bypass_actors",
            tier=bypass_tier,
            observed=bypass_actors,
            recommended="none_or_explicitly_reviewed",
            source=source,
            reason="保護を常時bypassできるactorを明示的に確認する",
            effect="admin・team・integration等が保護を迂回できる範囲を変更",
            proposed_operation=(
                {
                    "method": "REVIEW_THEN_PUT",
                    "candidate_endpoints": endpoints,
                    "body_basis": "fresh_all_effective_protection_bodies_required",
                    "change": {"ruleset_bypass_actors": "none_or_explicitly_reviewed"},
                }
                if bypass_actors and endpoints
                else None
            ),
            rollback=(
                {
                    "requirement": "capture_each_fresh_protection_body_before_change",
                    "candidate_endpoints": endpoints,
                }
                if bypass_actors and endpoints
                else None
            ),
            matches=bypass_matches,
            unavailable_reason=(
                ";".join(dict.fromkeys(bypass_reasons))
                if bypass_matches is None
                else None
            ),
        ),
    ]


def _missing_allow_list_patterns(patterns: list[str], missing: list[str]) -> list[str]:
    """足りない参照だけを追加する。既存の pattern は消さず、SHA 固定は緩めない。"""
    additions: set[str] = set()
    for reference in missing:
        action = reference.split("@", 1)[0]
        pinned_before = any(
            not pattern.startswith("!") and pattern.split("@", 1)[0] == action
            for pattern in patterns
        )
        additions.add(reference if pinned_before else f"{action}@*")
    return patterns + [
        pattern for pattern in sorted(additions) if pattern not in patterns
    ]


def _selected_patterns_setting(
    selected_endpoint: str,
    tier: str,
    selected: Any,
    selected_error: ApiUnavailable | None,
    evidence: _WorkflowEvidence,
) -> dict[str, Any]:
    if selected_error is not None:
        return _setting(
            name="selected_actions_patterns",
            tier=tier,
            observed=None,
            recommended="repository_specific_least_privilege",
            source=selected_endpoint,
            reason="selected policyの許可list詳細まで確認する",
            effect="許可list外actionの実行可否に影響",
            proposed_operation=None,
            rollback=None,
            matches=None,
            unavailable_reason=selected_error.reason,
        )
    patterns = [str(item) for item in (selected or {}).get("patterns_allowed") or []]
    derivation = evidence.allow_list()
    operation: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    unavailable_reason: str | None = None
    matches: bool | None
    if derivation["state"] == "ok":
        missing: list[str] = []
        conflicts: list[str] = []
        third_party = set(derivation["third_party_references"])
        for reference in derivation["used_references"]:
            covered, blocked_by = allow_list_covers(patterns, reference)
            if blocked_by:
                conflicts.extend(item for item in blocked_by if item not in conflicts)
            elif not covered and reference in third_party:
                missing.append(reference)
        positives = [item for item in patterns if not item.startswith("!")]
        unused = list(
            dict.fromkeys(
                item
                for item in positives
                if not any(
                    allow_list_covers([item], reference)[0]
                    for reference in derivation["used_references"]
                )
            )
        )
        broad = list(dict.fromkeys(item for item in positives if _overly_broad(item)))
        matches = not (missing or conflicts or unused or broad)
        extra: dict[str, Any] = {
            "derived_patterns": derivation["patterns_allowed"],
            "missing_references": missing,
            "unused_patterns": unused,
            "overly_broad_patterns": broad,
            "derivation": {
                key: derivation[key]
                for key in (
                    "workflow_files",
                    "github_owned_used",
                    "local_references",
                    "unmapped_references",
                )
            },
        }
        if conflicts:
            extra["conflicting_negated_patterns"] = conflicts
        elif missing:
            operation = _fresh_overlay_put(
                selected_endpoint,
                SELECTED_ACTIONS_FIELDS,
                {"patterns_allowed": _missing_allow_list_patterns(patterns, missing)},
            )
            rollback = _fresh_overlay_put(
                selected_endpoint,
                SELECTED_ACTIONS_FIELDS,
                {"patterns_allowed": patterns},
            )
    else:
        matches = None
        unavailable_reason = derivation["reason"]
        extra = {"derivation_unavailable_reason": derivation["reason"]}
    return _setting(
        name="selected_actions_patterns",
        tier=tier,
        observed=patterns,
        recommended="repository_specific_least_privilege",
        source=selected_endpoint,
        reason="workflowで使う第三者actionだけを覆う許可listにする",
        effect="許可list外actionの実行可否に影響",
        proposed_operation=operation,
        rollback=rollback,
        matches=matches,
        unavailable_reason=unavailable_reason,
        extra=extra,
    )


def _actions_policy_settings(
    repository: str,
    profile: str,
    api_get: Callable[[str], Any],
    evidence: _WorkflowEvidence,
    actions: Any,
    actions_error: ApiUnavailable | None,
    workflow: Any,
    workflow_error: ApiUnavailable | None,
) -> list[dict[str, Any]]:
    actions_endpoint = f"repos/{repository}/actions/permissions"
    workflow_endpoint = f"repos/{repository}/actions/permissions/workflow"
    selected_endpoint = f"repos/{repository}/actions/permissions/selected-actions"
    hardening_tier = "required" if profile == "high_risk_public" else "recommended"

    def actions_item(
        field: str,
        name: str,
        tier: str,
        recommended: Any,
        reason: str,
        effect: str,
        satisfied_values: tuple[Any, ...] = (),
    ) -> dict[str, Any]:
        return _complete_put_setting(
            endpoint=actions_endpoint,
            data=actions,
            error=actions_error,
            field=field,
            name=name,
            tier=tier,
            recommended=recommended,
            required_fields=("enabled",),
            preserved_fields=ACTIONS_PERMISSION_FIELDS,
            reason=reason,
            effect=effect,
            satisfied_values=satisfied_values,
        )

    def workflow_item(
        field: str,
        tier: str,
        recommended: Any,
        reason: str,
        effect: str,
    ) -> dict[str, Any]:
        return _complete_put_setting(
            endpoint=workflow_endpoint,
            data=workflow,
            error=workflow_error,
            field=field,
            name=field,
            tier=tier,
            recommended=recommended,
            required_fields=("default_workflow_permissions",),
            preserved_fields=WORKFLOW_PERMISSION_FIELDS,
            reason=reason,
            effect=effect,
        )

    allowed_actions = actions_item(
        "allowed_actions",
        "allowed_actions",
        hardening_tier,
        "selected",
        "実行可能な第三者actionを明示的に制限する",
        "許可list外のaction実行を拒否",
        satisfied_values=("local_only",),
    )
    if allowed_actions["proposed_operation"] is not None:
        allowed_actions = {
            **allowed_actions,
            "proposed_operation": _switch_to_selected_operation(
                actions_endpoint, selected_endpoint, evidence.allow_list()
            ),
        }
    can_approve = workflow_item(
        "can_approve_pull_request_reviews",
        "recommended",
        False,
        "workflow自身によるreview承認を禁止する",
        CAN_APPROVE_EFFECT,
    )
    if can_approve["proposed_operation"] is not None:
        can_approve = {
            **can_approve,
            "exception": _github_token_pr_creation_exception(evidence),
        }
    settings = [
        actions_item(
            "enabled",
            "actions_enabled",
            "required",
            True,
            "required CIを実行可能にする",
            "repositoryのGitHub Actions実行可否を変更",
        ),
        actions_item(
            "sha_pinning_required",
            "sha_pinning_required",
            hardening_tier,
            True,
            "workflow action参照の供給網リスクを抑える",
            "full commit SHAでないaction参照を拒否",
        ),
        allowed_actions,
        workflow_item(
            "default_workflow_permissions",
            "required",
            "read",
            "GITHUB_TOKENの既定権限を最小化する",
            "明示permissionsのないworkflow tokenをread-only化",
        ),
        can_approve,
    ]
    if actions_error or (actions or {}).get("allowed_actions") != "selected":
        return settings
    selected, selected_error = _fetch(api_get, selected_endpoint)

    def selected_item(
        field: str,
        name: str,
        tier: str,
        recommended: Any,
        reason: str,
        effect: str,
    ) -> dict[str, Any]:
        return _complete_put_setting(
            endpoint=selected_endpoint,
            data=selected,
            error=selected_error,
            field=field,
            name=name,
            tier=tier,
            recommended=recommended,
            required_fields=(),
            preserved_fields=SELECTED_ACTIONS_FIELDS,
            reason=reason,
            effect=effect,
        )

    settings.extend(
        [
            selected_item(
                "github_owned_allowed",
                "selected_actions_github_owned_allowed",
                "required",
                True,
                "CIで利用するGitHub公式actionを許可する",
                "GitHub-owned actionの実行可否を変更",
            ),
            selected_item(
                "verified_allowed",
                "selected_actions_verified_allowed",
                hardening_tier,
                False,
                "verified publisher全体ではなく必要なactionだけを許可する",
                "Marketplace verified creatorのaction一括許可を無効化",
            ),
            _selected_patterns_setting(
                selected_endpoint, hardening_tier, selected, selected_error, evidence
            ),
        ]
    )
    return settings


def review_repository(
    repository: str,
    profile: str,
    *,
    api_get: Callable[[str], Any] = gh_api_get,
    observed_at: datetime | None = None,
    expected_login: str | None = None,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must be OWNER/REPO")
    timestamp = observed_at or datetime.now(timezone.utc)

    root_endpoint = f"repos/{repository}"
    root, root_error = _fetch(api_get, root_endpoint)
    if (
        root_error is None
        and isinstance(root, dict)
        and str(root.get("full_name") or "").lower() != repository.lower()
    ):
        root_error = ApiUnavailable(
            status_code=None, reason="repository_identity_mismatch"
        )
    user_endpoint = "user"
    user, user_error = _fetch(api_get, user_endpoint)
    actions_endpoint = f"repos/{repository}/actions/permissions"
    actions, actions_error = _fetch(api_get, actions_endpoint)
    workflow_endpoint = f"repos/{repository}/actions/permissions/workflow"
    workflow, workflow_error = _fetch(api_get, workflow_endpoint)
    default_branch = str((root or {}).get("default_branch") or "") or None
    workflow_evidence = _WorkflowEvidence(api_get, repository, default_branch)

    settings: list[dict[str, Any]] = []
    owner = (root or {}).get("owner") or {}
    inferred_expected_login = expected_login
    if inferred_expected_login is None and owner.get("type") == "User":
        inferred_expected_login = owner.get("login")
    if user_error or not isinstance(user, dict) or not user.get("login"):
        settings.append(
            _setting(
                name="authenticated_account",
                tier="required",
                observed=None,
                recommended=inferred_expected_login or "explicit_account_confirmation",
                source=user_endpoint,
                reason="設定変更候補を確認するGitHub accountを固定する",
                effect="別accountでのrepository設定操作を防ぐ",
                proposed_operation=None,
                rollback=None,
                matches=None,
                unavailable_reason=(
                    user_error.reason
                    if user_error
                    else "authenticated_login_not_returned"
                ),
            )
        )
    else:
        login = str(user["login"])
        matches = bool(
            inferred_expected_login
            and login.lower() == str(inferred_expected_login).lower()
        )
        settings.append(
            _setting(
                name="authenticated_account",
                tier="required",
                observed=login,
                recommended=(
                    inferred_expected_login
                    if inferred_expected_login
                    else {"confirm_login": login}
                ),
                source=user_endpoint,
                reason="設定変更候補を確認するGitHub accountを固定する",
                effect="別accountでのrepository設定操作を防ぐ",
                proposed_operation=None,
                rollback=None,
                matches=matches,
            )
        )
    settings.append(
        _simple_setting(
            endpoint=root_endpoint,
            data=root,
            error=root_error,
            field="delete_branch_on_merge",
            name="delete_branch_on_merge",
            tier="recommended",
            recommended=True,
            method="PATCH",
            reason="merge済み短期branchを自動整理する",
            effect="今後mergeしたremote head branchを自動削除",
        )
    )
    for field, recommended in (
        ("allow_squash_merge", True),
        ("allow_merge_commit", False),
        ("allow_rebase_merge", False),
        ("allow_auto_merge", False),
    ):
        settings.append(
            _simple_setting(
                endpoint=root_endpoint,
                data=root,
                error=root_error,
                field=field,
                name=field,
                tier="recommended",
                recommended=recommended,
                method="PATCH",
                reason="小さなversion幅と追跡しやすい履歴を維持する",
                effect="GitHub UIで選べるmerge方法を変更",
            )
        )

    settings.extend(
        _actions_policy_settings(
            repository,
            profile,
            api_get,
            workflow_evidence,
            actions,
            actions_error,
            workflow,
            workflow_error,
        )
    )
    settings.extend(
        _branch_protection_observations(repository, profile, api_get, default_branch)
    )
    for name, reason, effect in (
        (
            "dependabot_security_updates",
            "脆弱な依存更新を追跡する",
            "security update PRを有効化",
        ),
        (
            "secret_scanning",
            "公開履歴のsecret候補を検出する",
            "secret scanningを有効化",
        ),
        (
            "secret_scanning_push_protection",
            "secretの新規pushを入口で止める",
            "検出されたsecretを含むpushを拒否",
        ),
    ):
        settings.append(
            _security_setting(
                repository=repository,
                root=root,
                error=root_error,
                name=name,
                tier=(
                    "required"
                    if name != "dependabot_security_updates"
                    or profile == "high_risk_public"
                    else "recommended"
                ),
                reason=reason,
                effect=effect,
            )
        )

    pvr_endpoint = f"repos/{repository}/private-vulnerability-reporting"
    pvr, pvr_error = _fetch(api_get, pvr_endpoint)
    pvr_tier = "required" if profile == "high_risk_public" else "recommended"
    if pvr_error or not isinstance(pvr, dict) or "enabled" not in pvr:
        settings.append(
            _setting(
                name="private_vulnerability_reporting",
                tier=pvr_tier,
                observed=None,
                recommended=True,
                source=pvr_endpoint,
                reason="公開issueを使わず脆弱性を受け付ける",
                effect="外部報告者が非公開で脆弱性を送信可能になる",
                proposed_operation=None,
                rollback=None,
                matches=None,
                unavailable_reason=(
                    pvr_error.reason if pvr_error else "field_not_returned"
                ),
            )
        )
    else:
        observed = bool((pvr or {}).get("enabled"))
        settings.append(
            _setting(
                name="private_vulnerability_reporting",
                tier=pvr_tier,
                observed=observed,
                recommended=True,
                source=pvr_endpoint,
                reason="公開issueを使わず脆弱性を受け付ける",
                effect="外部報告者が非公開で脆弱性を送信可能になる",
                proposed_operation=_operation("PUT", pvr_endpoint),
                rollback=_operation("DELETE", pvr_endpoint),
                matches=observed,
            )
        )

    codeql_endpoint = f"repos/{repository}/code-scanning/default-setup"
    codeql, codeql_error = _fetch(api_get, codeql_endpoint)
    default_state = (codeql or {}).get("state") if isinstance(codeql, dict) else None
    analyses_endpoint = f"repos/{repository}/code-scanning/analyses?per_page=100"
    analyses: Any = None
    analyses_error: ApiUnavailable | None = None
    if default_state != "configured":
        analyses, analyses_error = _fetch(api_get, analyses_endpoint)
    default_ref = f"refs/heads/{(root or {}).get('default_branch')}"

    def recent_default_branch_analysis(item: Any) -> bool:
        if not isinstance(item, dict) or item.get("ref") != default_ref:
            return False
        tool_name = str(((item.get("tool") or {}).get("name") or "")).lower()
        if tool_name not in {"codeql", "github code scanning"}:
            return False
        try:
            created_at = datetime.fromisoformat(
                str(item.get("created_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            return False
        age = timestamp.astimezone(timezone.utc) - created_at.astimezone(timezone.utc)
        # 日数の切り捨てで 30 日 23 時間を「30 日以内」にしない。
        return timedelta(days=-1) <= age <= timedelta(days=30)

    recent_analysis = next(
        (item for item in analyses or [] if recent_default_branch_analysis(item)),
        None,
    )
    if default_state == "configured":
        code_scanning_matches: bool | None = True
        code_scanning_observed: Any = "default_setup_configured"
        code_scanning_unavailable = None
    elif recent_analysis is not None:
        code_scanning_matches = True
        code_scanning_observed = {
            "mode": "advanced_or_external_analysis",
            "analysis_id": recent_analysis.get("id"),
            "ref": recent_analysis.get("ref"),
            "created_at": recent_analysis.get("created_at"),
        }
        code_scanning_unavailable = None
    elif analyses_error is not None:
        code_scanning_matches = None
        code_scanning_observed = None
        code_scanning_unavailable = analyses_error.reason
    else:
        code_scanning_matches = False
        code_scanning_observed = default_state or "no_recent_default_branch_analysis"
        code_scanning_unavailable = None
    settings.append(
        _setting(
            name="code_scanning_default_setup",
            tier="required" if profile == "high_risk_public" else "recommended",
            observed=code_scanning_observed,
            recommended="configured_default_or_recent_advanced_analysis",
            source=f"{codeql_endpoint};{analyses_endpoint}",
            reason="default setupまたはrecent analysisで静的解析を確認する",
            effect="CodeQL default setupを有効化、またはadvanced setupを維持",
            proposed_operation=(
                _operation("PATCH", codeql_endpoint, {"state": "configured"})
                if code_scanning_matches is False
                else None
            ),
            rollback=(
                _operation("PATCH", codeql_endpoint, {"state": default_state})
                if code_scanning_matches is False and default_state
                else None
            ),
            matches=code_scanning_matches,
            unavailable_reason=code_scanning_unavailable,
        )
    )

    required_changes = [item for item in settings if item["blocks_intent"]]
    unknowns = [
        {
            "setting": item["name"],
            "source_endpoint": item["source_endpoint"],
            "reason": item["unavailable_reason"],
        }
        for item in settings
        if item["classification"] == "unavailable"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "needs_human_input" if required_changes else "pass",
        "repository": repository,
        "profile": profile,
        "authenticated_login": (user.get("login") if isinstance(user, dict) else None),
        "observed_at": timestamp.astimezone(timezone.utc).isoformat(),
        "settings": settings,
        "required_change_count": len(required_changes),
        "recommended_change_count": sum(
            item["classification"] == "recommended_change" for item in settings
        ),
        "unknowns": unknowns,
        "external_actions_performed": False,
        "next_step": (
            "設定ごとにpreviewし、現在会話で承認されたものだけ別工程で変更・再測定する"
        ),
    }
