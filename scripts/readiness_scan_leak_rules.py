from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import unquote

SECRET_PATTERNS = (
    re.compile(r"(?:(?<![A-Za-z0-9_-])|(?<=\\[nrt]))sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    re.compile(r"(?im)^[ \t]*claude-session[ \t]*:|claude[.]ai/code/session_"),
)
SECRET_RULE_IDS = (
    "openai_key",
    "github_token",
    "github_pat",
    "aws_access_key",
    "slack_token",
    "private_key",
    "claude_session",
)


class SecretPorts(NamedTuple):
    path_patterns: tuple
    path_rule_ids: tuple
    public_synthetic_path_signatures: frozenset
    macos_shared_parent_matches: Callable
    bytes_pattern_hit: Callable
    personal_path_matches: Callable
    normalized_personal_path_matches: Callable
    trusted_public_fixture_policy: Callable
    load_github_settings_module: Callable


_INSTALLED_PORTS: list = []


def install_ports(ports: SecretPorts) -> None:
    _INSTALLED_PORTS[:] = [ports]


def _ports() -> SecretPorts:
    if not _INSTALLED_PORTS:
        raise RuntimeError("secret_ports_not_installed")
    return _INSTALLED_PORTS[0]


def _zip_exact(first, second):
    first, second = list(first), list(second)
    if len(first) != len(second):
        raise ValueError("_zip_exact: lengths differ")
    return zip(first, second)


def secret_matches(data: bytes) -> Counter:
    matches = Counter()
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        for candidate in (text, unquote(text)):
            counts = Counter(
                (rule, hashlib.sha256(match.group().encode("utf-8")).hexdigest())
                for rule, pattern in _zip_exact(SECRET_RULE_IDS, SECRET_PATTERNS)
                for match in pattern.finditer(candidate)
            )
            matches |= counts
    matches |= Counter(
        (rule, hashlib.sha256(match.group()).hexdigest())
        for rule, pattern in _zip_exact(SECRET_RULE_IDS, SECRET_PATTERNS)
        for match in re.finditer(pattern.pattern.encode("ascii"), data)
    )
    return matches


def _reviewed_policy_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def text_has(patterns: tuple[re.Pattern, ...], data: bytes) -> bool:
    ports = _ports()
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        decoded_text = unquote(text)
        if ports.path_patterns[1] in patterns and any(
            ports.macos_shared_parent_matches(value) for value in (text, decoded_text)
        ):
            return True
        if any(
            pattern.search(candidate)
            for candidate in (text, decoded_text)
            for pattern in patterns
        ):
            return True
    return ports.bytes_pattern_hit(patterns, data)


class ReviewedSecretExceptions:
    def __init__(self, policy_path: Path, remote: str):
        ports = _ports()
        path_patterns = ports.path_patterns
        path_rule_ids = ports.path_rule_ids
        try:
            raw_policy = policy_path.read_bytes()
            if len(raw_policy) > 1_000_000:
                raise ValueError("size")
            policy = json.loads(
                raw_policy.decode("utf-8"), object_pairs_hook=_reviewed_policy_object
            )
            if (
                not isinstance(policy, dict)
                or set(policy) != {"version", "entries"}
                or type(policy["version"]) is not int
                or policy["version"] not in {1, 2, 3}
                or not isinstance(policy["entries"], list)
            ):
                raise ValueError("schema")
            match = re.fullmatch(
                r"(?:https://github[.]com/|git@github[.]com:|ssh://git@github[.]com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:[.]git)?",
                remote,
            )
            if not match:
                raise ValueError("origin")
            identity = match.group(1)
            if len(policy["entries"]) > 1000:
                raise ValueError("entry_limit")
            if policy["version"] == 3:
                if json.dumps(policy, sort_keys=True, allow_nan=False) != json.dumps(
                    ports.trusted_public_fixture_policy(),
                    sort_keys=True,
                    allow_nan=False,
                ):
                    raise ValueError("public_fixture_policy_not_trusted")
            self.entries = []
            ids = set()
            bindings = set()
            keys = {
                "id",
                "repo",
                "path",
                "content_sha256",
                "rule",
                "match_sha256",
                "occurrence_count",
                "review_reason",
            }
            if policy["version"] in {2, 3}:
                keys |= {"expires_at", "review_reference"}
            for entry in policy["entries"]:
                if not isinstance(entry, dict) or set(entry) != keys:
                    raise ValueError("entry")
                if any(
                    not isinstance(entry[key], str)
                    for key in keys - {"occurrence_count"}
                ):
                    raise ValueError("type")
                if (
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", entry["id"])
                    or entry["id"] in ids
                    or text_has(SECRET_PATTERNS + path_patterns, entry["id"].encode())
                ):
                    raise ValueError("id")
                if any(
                    text_has(SECRET_PATTERNS, entry[key].encode("utf-8"))
                    for key in ("repo", "path")
                ):
                    raise ValueError("credential_in_binding")
                rel = entry["path"]
                if (
                    not rel
                    or rel.startswith("/")
                    or "\\" in rel
                    or ":" in rel
                    or any(part in {"", ".", ".."} for part in rel.split("/"))
                    or any(ord(char) < 32 for char in rel)
                ):
                    raise ValueError("path")
                if entry["repo"] != identity or entry["rule"] not in (
                    (
                        path_rule_ids
                        if policy["version"] == 3
                        else SECRET_RULE_IDS
                        + (path_rule_ids if policy["version"] == 2 else ())
                    )
                ):
                    raise ValueError("identity_or_rule")
                if any(
                    not re.fullmatch(r"[0-9a-f]{64}", entry[key])
                    for key in ("content_sha256", "match_sha256")
                ):
                    raise ValueError("digest")
                if (
                    policy["version"] == 3
                    and (entry["rule"], entry["match_sha256"])
                    not in ports.public_synthetic_path_signatures
                ):
                    raise ValueError("public_synthetic_fixture_required")
                if (
                    type(entry["occurrence_count"]) is not int
                    or entry["occurrence_count"] <= 0
                    or not entry["review_reason"].strip()
                ):
                    raise ValueError("count_or_reason")
                if text_has(SECRET_PATTERNS, entry["review_reason"].encode()):
                    raise ValueError("credential_in_reason")
                if policy["version"] in {2, 3}:
                    if (
                        not re.fullmatch(
                            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["expires_at"]
                        )
                        or not entry["review_reference"].strip()
                        or text_has(SECRET_PATTERNS, entry["review_reference"].encode())
                    ):
                        raise ValueError("expiry_or_review")
                    expires = datetime.strptime(
                        entry["expires_at"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    if expires <= now or expires > now + timedelta(days=90):
                        raise ValueError("expired_or_excessive_validity")
                binding = tuple(
                    entry[key]
                    for key in (
                        "repo",
                        "path",
                        "content_sha256",
                        "rule",
                        "match_sha256",
                    )
                )
                if binding in bindings:
                    raise ValueError("duplicate_entry")
                bindings.add(binding)
                ids.add(entry["id"])
                self.entries.append(entry)
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise RuntimeError("reviewed_secret_exceptions_invalid") from exc
        self.applied = Counter()
        self.path_applied = Counter()
        self.normalized_path_applied = Counter()
        self.has_path_entries = any(
            entry["rule"] in path_rule_ids for entry in self.entries
        )
        try:
            settings = ports.load_github_settings_module()
            live = settings.gh_api_get("https://api.github.com/repos/" + identity)
            if (
                not isinstance(live, dict)
                or live.get("full_name") != identity
                or live.get("private") is not (policy["version"] != 3)
                or live.get("visibility")
                != ("public" if policy["version"] == 3 else "private")
            ):
                raise ValueError("reviewed_origin_visibility_required")
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            raise RuntimeError("reviewed_secret_exceptions_invalid") from exc

    def _unreviewed(
        self, path: str, data: bytes, matches: Counter, applied: Counter
    ) -> bool:
        content_digests = {hashlib.sha256(data).hexdigest()}
        if b"\r\n" in data:
            content_digests.add(
                hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
            )
        for entry in self.entries:
            key = (entry["rule"], entry["match_sha256"])
            if (
                entry["path"] == path
                and entry["content_sha256"] in content_digests
                and matches.get(key) == entry["occurrence_count"]
            ):
                applied[entry["id"]] += matches.pop(key)
        return bool(matches)

    def has_unreviewed(self, path: str, data: bytes) -> bool:
        return self._unreviewed(path, data, secret_matches(data), self.applied)

    def has_unreviewed_paths(
        self, path: str, data: bytes, scanned_data: bytes | None = None
    ) -> bool:
        return self._unreviewed(
            path,
            data,
            _ports().personal_path_matches(
                data if scanned_data is None else scanned_data
            ),
            self.path_applied,
        )

    def all_paths_reviewed(
        self,
        path: str,
        data: bytes,
        *,
        record: bool = True,
        normalized: bool = False,
        normalizer=None,
    ) -> bool:
        ports = _ports()
        matches = (
            ports.normalized_personal_path_matches(data, normalizer)
            if normalized
            else ports.personal_path_matches(data)
        )
        if not matches:
            return False
        applied = self.path_applied if record and not normalized else Counter()
        unreviewed = self._unreviewed(path, data, matches, applied)
        if normalized and not unreviewed:
            self.normalized_path_applied |= applied
        return not unreviewed

    def report(self, *, personal_paths: bool = False):
        return [
            {"id": key, "occurrence_count": count}
            for key, count in sorted(
                (self.path_applied if personal_paths else self.applied).items()
            )
        ]
