from __future__ import annotations

import argparse
import types
import io
import copy
import contextlib
import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, NamedTuple, TextIO
from urllib.parse import unquote, urlsplit, urlunsplit

_BUNDLE_PART_PREFIX = "readiness_scan_"
_BUNDLE_PART_NAME = re.compile(r"readiness_scan_[a-z0-9_]+")
_BUNDLE_FACADE = Path(__file__).resolve()
_BUNDLE_FACADE_SHA256 = hashlib.sha256(_BUNDLE_FACADE.read_bytes()).hexdigest()
_BUNDLE_PARTS: dict[str, tuple[types.ModuleType, str]] = {}


def _part(name: str) -> types.ModuleType:
    if not _BUNDLE_PART_NAME.fullmatch(name):
        raise ImportError("scanner_part_name_rejected:" + name)
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location("_" + name, path)
    if spec is None:
        raise ImportError("scanner_part_unavailable:" + name)
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    filename = path.name
    loaded = _BUNDLE_PARTS.get(filename)
    if loaded is not None and loaded[1] == digest:
        return loaded[0]
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    except BaseException:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
        raise
    _BUNDLE_PARTS[filename] = (module, digest)
    return module


def _scanner_bundle_digests() -> dict[str, str]:
    unloaded = sorted(
        candidate.name
        for candidate in _BUNDLE_FACADE.parent.glob(_BUNDLE_PART_PREFIX + "*.py")
        if candidate.name not in _BUNDLE_PARTS
    )
    if unloaded:
        raise RuntimeError("scanner_part_not_loaded:" + ",".join(unloaded))
    digests = {name: digest for name, (_, digest) in _BUNDLE_PARTS.items()}
    digests[_BUNDLE_FACADE.name] = _BUNDLE_FACADE_SHA256
    return digests


def scanner_bundle_names() -> tuple[str, ...]:
    return tuple(sorted(_scanner_bundle_digests()))


def scanner_bundle_sha256() -> str:
    lines = "".join(
        name + "\0" + digest + "\n"
        for name, digest in sorted(_scanner_bundle_digests().items())
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


_report = _part("readiness_scan_report")
SCHEMA = _report.SCHEMA
GUARANTEES = _report.GUARANTEES
NON_GUARANTEES = _report.NON_GUARANTEES
boundary_sections = _report.boundary_sections
format_boundary_text = _report.format_boundary_text
dialogue_exit_code = _report.dialogue_exit_code
format_check_line = _report.format_check_line
format_human_report = _report.format_human_report
_MODULE_NAME_SAFE = _report._MODULE_NAME_SAFE
_FRAME_BASENAME_UNSAFE = _report._FRAME_BASENAME_UNSAFE
unexpected_exception_issue = _report.unexpected_exception_issue
report_exit_code = _report.report_exit_code

_cli = _part("readiness_scan_cli")
DEFAULT_CONSISTENCY_BASE_REF = _cli.DEFAULT_CONSISTENCY_BASE_REF
AUDIENCE_CHOICES = _cli.AUDIENCE_CHOICES
MODE_CHOICES = _cli.MODE_CHOICES
ScanOptions = _cli.ScanOptions
valid_initial_private_scope = _cli.valid_initial_private_scope
_emit = _cli._emit
prompt_from_stdin = _cli.prompt_from_stdin
prompt_text = _cli.prompt_text
prompt_choice = _cli.prompt_choice
prompt_yes_no = _cli.prompt_yes_no

_media = _part("readiness_scan_media")
CMO3_SUFFIX = _media.CMO3_SUFFIX
CMO3_ENTRY_NAME = _media.CMO3_ENTRY_NAME
_CMO3_XOR_TABLE = _media._CMO3_XOR_TABLE
_ZIP_LOCAL_HEADER = _media._ZIP_LOCAL_HEADER
media_kind = _media.media_kind
media_candidate = _media.media_candidate
_metadata_media_names = _media._metadata_media_names

_identity = _part("readiness_scan_identity")
_is_coauthor_line = _identity._is_coauthor_line
effective_identity = _identity.effective_identity
_guard_worktree_parent_components = _identity._guard_worktree_parent_components
IdentityScanCache = _identity.IdentityScanCache
IDENTITY_SCAN_CACHE_VERSION = _identity.IDENTITY_SCAN_CACHE_VERSION
_IDENTITY_SCAN_CACHE_DISABLED = _identity._IDENTITY_SCAN_CACHE_DISABLED
_IDENTITY_SCAN_CACHE_MAX_ENTRIES = _identity._IDENTITY_SCAN_CACHE_MAX_ENTRIES
_IDENTITY_SCAN_CACHE_MAX_BYTES = _identity._IDENTITY_SCAN_CACHE_MAX_BYTES

_history = _part("readiness_scan_history")
_decode_scannable_text = _history._decode_scannable_text
_HEX64 = _identity._HEX64

REQUIRED = ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "PREFLIGHT.md")
# PREFLIGHT.md は一般的な語のため、deployment preflight 手順書のような無関係な
# 同名fileが存在しうる。ファイル名の一致だけでreview記録とみなすと、検査記録が
# 無いrepositoryがpassする。テンプレートが埋め込むmarkerの実在まで確認する。
REVIEW_RECORD = "PREFLIGHT.md"
REVIEW_RECORD_MARKER = "<!-- repo-preflight:review-record -->"
DEPENDENCY_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
)
_secrets = _part("readiness_scan_leak_rules")
SECRET_PATTERNS = _secrets.SECRET_PATTERNS
SECRET_RULE_IDS = _secrets.SECRET_RULE_IDS
RETIRED_RULE_IDS = _secrets.RETIRED_RULE_IDS
_zip_exact = _secrets._zip_exact
secret_matches = _secrets.secret_matches


def _reviewed_policy_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def trusted_public_fixture_policy() -> dict:
    """固定した検査器のcommitに同梱された台帳を読む。対象treeからは採らない。"""
    tool = Path(__file__).resolve().parents[1]
    rc, root = run(tool, "git", "rev-parse", "--show-toplevel")
    if rc or Path(root).resolve() != tool:
        raise ValueError("public_fixture_tool_unverified")
    rc, content = run(
        tool, "git", "show", "HEAD:.repo-preflight/reviewed-synthetic-fixtures.json"
    )
    if rc or len(content) > 1_000_000:
        raise ValueError("public_fixture_policy_unavailable")
    return json.loads(content, object_pairs_hook=_reviewed_policy_object)


class ReviewedSecretExceptions:
    """明示指定された、内容と検出を完全束縛するレビュー済み例外。"""

    def __init__(self, policy_path: Path, remote: str):
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
            # 生の認証値やquery/fragment付きremoteはidentity根拠として使わない。
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
                # Pythonの辞書比較はtrue==1、3==3.0も等しい。型を含むJSONで比較する。
                if json.dumps(policy, sort_keys=True, allow_nan=False) != json.dumps(
                    trusted_public_fixture_policy(), sort_keys=True, allow_nan=False
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
                if entry["rule"] in RETIRED_RULE_IDS:
                    continue  # 廃止した規則の例外は読み捨てる（もう検出しない）
                if (
                    not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", entry["id"])
                    or entry["id"] in ids
                    or text_has(SECRET_PATTERNS + PATH_PATTERNS, entry["id"].encode())
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
                        PATH_RULE_IDS
                        if policy["version"] == 3
                        else SECRET_RULE_IDS
                        + (PATH_RULE_IDS if policy["version"] == 2 else ())
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
                    not in PUBLIC_SYNTHETIC_PATH_SIGNATURES
                ):
                    raise ValueError("public_synthetic_fixture_required")
                if (
                    type(entry["occurrence_count"]) is not int
                    or entry["occurrence_count"] <= 0
                    or not entry["review_reason"].strip()
                ):
                    raise ValueError("count_or_reason")
                # 理由にもcredentialを保存しない。値や理由はreportへ出さない。
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
            entry["rule"] in PATH_RULE_IDS for entry in self.entries
        )
        # secretだけの旧形式も照合する。path項目の削除でpublic制約を外さない。
        try:
            settings = load_github_settings_module()
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
        self,
        path: str,
        data: bytes,
        matches: Counter,
        applied: Counter,
        *,
        normalized_matches: Callable[[], Counter] | None = None,
    ) -> bool:
        # Windows の autocrlf で作業ツリーだけ CRLF になっても、LF で登録した digest と照合する。
        content_digests = {hashlib.sha256(data).hexdigest()}
        if b"\r\n" in data:
            content_digests.add(
                hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()
            )
        normalized_counts = None
        for entry in self.entries:
            key = (entry["rule"], entry["match_sha256"])
            if entry["path"] == path and entry["content_sha256"] in content_digests:
                count = matches.get(key, 0)
                exact = count == entry["occurrence_count"]
                if (
                    not exact
                    and normalized_matches is not None
                    and 0 < count < entry["occurrence_count"]
                ):
                    if normalized_counts is None:
                        normalized_counts = normalized_matches()
                    # 同じ本文の正規化で再構成された分だけ増えた場合に限る。
                    # 「件数以下」では許可せず、正規化後も完全一致を要求する。
                    exact = normalized_counts.get(key) == entry["occurrence_count"]
                if exact:
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
            personal_path_matches(data if scanned_data is None else scanned_data),
            self.path_applied,
            normalized_matches=lambda: normalized_personal_path_matches(data),
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
        # 正規化後もrepo/path/raw content hash/検出値hash/件数/期限の全束縛を維持する。
        matches = (
            normalized_personal_path_matches(data, normalizer)
            if normalized
            else personal_path_matches(data)
        )
        if not matches:
            return False
        applied = self.path_applied if record and not normalized else Counter()
        unreviewed = self._unreviewed(
            path,
            data,
            matches,
            applied,
            normalized_matches=(
                None
                if normalized
                else lambda: normalized_personal_path_matches(data, normalizer)
            ),
        )
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


# path文法の正本を同梱generic moduleから読む。CLI直接/任意のimportlib名でも同じsourceを使う。
_PATH_GRAMMAR_SPEC = importlib.util.spec_from_file_location(
    "_repo_preflight_path_grammar",
    Path(__file__).resolve().with_name("identity_normalization.py"),
)
_PATH_GRAMMAR = importlib.util.module_from_spec(_PATH_GRAMMAR_SPEC)
_PATH_GRAMMAR_SPEC.loader.exec_module(_PATH_GRAMMAR)
PATH_PATTERNS = _PATH_GRAMMAR.PATH_PATTERNS
PATH_RULE_IDS = _PATH_GRAMMAR.PATH_RULE_IDS

# 公開fixtureの予約値。任意の個人pathをpolicyだけで登録する入口は設けない。
# これらのhashでも、明示policyのrepo/path/blob/件数/期限/review束縛が必要。
PUBLIC_SYNTHETIC_PATH_SIGNATURES = frozenset(
    (rule, hashlib.sha256(value.encode("utf-8")).hexdigest())
    for rule, value in (
        ("windows_user_path", PureWindowsPath("C:/", "Users", "My").as_posix()),
        ("macos_user_path", str(PurePosixPath("/", "Users", "My"))),
        ("linux_home_path", str(PurePosixPath("/", "home", "me"))),
    )
)
# ADR-0017: four reviewed negative-fixture signatures. These are candidates only:
# no exclusion occurs without the existing exact v3 policy and live public check.
PUBLIC_SYNTHETIC_PATH_SIGNATURES |= frozenset(
    ("linux_home_path", digest)
    for digest in (
        "2ce1dda1084edaefc6f33285f9259285b887f0eae16957495c6413f70ce72c85",
        "01c9f309dc1e667a5404ad9d98fb3e96c4d9c109d5b6d63d83adaf41448c38ec",
        "6d06e07163cde8fac80c1941aed3c558499ef602b963201c2d43c681e76257df",
        "b1cbc5f347543a03701354923cd95344068142e4aa08c74d259fbfe7c62984fc",
    )
)


def personal_path_matches(data: bytes) -> Counter:
    """検出値を保持せず、encoding・URL復号・binary間の重複は最大件数で束縛する。"""
    matches = Counter()
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        for candidate in (text, unquote(text)):
            matches |= Counter(
                (
                    rule,
                    hashlib.sha256(
                        (
                            match.groupdict().get("personal_path") or match.group()
                        ).encode("utf-8")
                    ).hexdigest(),
                )
                for rule, match in _PATH_GRAMMAR.iter_private_path_matches(candidate)
            )
    matches |= Counter(
        (
            rule,
            hashlib.sha256(
                match.groupdict().get("personal_path") or match.group()
            ).hexdigest(),
        )
        for rule, match in _PATH_GRAMMAR.iter_private_path_matches(data)
    )
    return matches


# .cmo3 の展開上限。試験が差し替えるので窓口に置き、呼ぶたびに部品へ渡す。
CMO3_MAX_XML_BYTES = 64 * 1024 * 1024

# 親プロセスの GIT_DIR / GIT_WORK_TREE 等は、--repo で指した path と別の
# repository へ判定対象を飛ばす。subdirectory を囲っている repo へ広げるのと
# 同じ取り違えなので、git 呼び出しには継承しない。
_GIT_REPO_OVERRIDE_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_INDEX_FILE",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_isolation_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """cwd から見える repository 以外へ、判定対象を飛ばさない環境を返す。"""
    env = dict(os.environ if base is None else base)
    for key in _GIT_REPO_OVERRIDE_VARS:
        env.pop(key, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


GIT_UNTRUSTED_CONFIG_OVERRIDES = ("-c", "core.fsmonitor=")


def git_hardened_command(cmd):
    return [cmd[0], *GIT_UNTRUSTED_CONFIG_OVERRIDES, *cmd[1:]]


def run_subprocess(*args, **kwargs):
    """subprocess.run 互換。git 呼び出しから repository 上書き環境を除く。"""
    call_args = list(args)
    cmd = call_args[0] if call_args else kwargs.get("args")
    if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
        kwargs["env"] = git_isolation_env(kwargs.get("env"))
        if call_args:
            call_args[0] = git_hardened_command(cmd)
        else:
            kwargs["args"] = git_hardened_command(cmd)
    return subprocess.run(*call_args, **kwargs)


class GitPort:
    """部品が git を呼ぶための口。呼ばれた時点の窓口の run_subprocess へ転送する。

    試験が窓口の run_subprocess を差し替えても、部品の奥まで届く。
    """

    PIPE = subprocess.PIPE

    @staticmethod
    def run_subprocess(*args, **kwargs):
        return run_subprocess(*args, **kwargs)


class HistoryPort(GitPort):
    """部品の履歴検査が窓口の名前を読むための口。

    属性を読んだ時点の窓口の値を返すので、試験が窓口の名前を差し替えても部品の奥まで届く。
    """

    SubprocessError = subprocess.SubprocessError
    _NAMES = frozenset(
        {
            "run",
            "fixed_history_revisions",
            "media_candidate",
            "_HISTORY_BATCH_BUDGET_BYTES",
            "load_identity_gate",
            "PATH_PATTERNS",
            "SECRET_PATTERNS",
            "_inspect_history_media",
            "_metadata_media_names",
            "_zip_exact",
            "normalized_personal_path_matches",
            "personal_path_matches",
            "sanitized_evidence_label",
            "secret_matches",
            "text_has",
            "_bytes_pattern_hit",
            "_guard_worktree_parent_components",
        }
    )

    def __getattr__(self, name):
        if name in self._NAMES:
            return globals()[name]
        raise AttributeError(name)


def run(repo: Path, *args: str) -> tuple[int, str]:
    # git側の出力encodingをUTF-8に固定してから読む。i18n.logOutputEncodingが
    # 非UTF-8のrepositoryでも作者名を壊さない。不正byteはfail-closed比較に残す
    if args and args[0] == "git":
        args = (args[0], "-c", "i18n.logOutputEncoding=UTF-8", *args[1:])
    result = run_subprocess(
        args,
        cwd=repo,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        capture_output=True,
        shell=False,
    )
    return result.returncode, result.stdout.strip()


def redact_remote(remote: str) -> str | None:
    if not remote:
        return None
    if "://" not in remote:
        sanitized = remote.split("?", 1)[0].split("#", 1)[0]
        scp_match = re.match(r"^(?:[^@/:]+@)?([^/:]+):(.+)$", sanitized)
        if scp_match and ("@" in sanitized or "." in scp_match.group(1)):
            return f"{scp_match.group(1)}:{scp_match.group(2)}"
        return sanitized
    parts = urlsplit(remote)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def text_has(patterns: tuple[re.Pattern, ...], data: bytes) -> bool:
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        decoded_text = unquote(text)
        candidates = (text,) if decoded_text == text else (text, decoded_text)
        if PATH_PATTERNS[1] in patterns and any(
            _PATH_GRAMMAR.macos_shared_parent_matches(value) for value in candidates
        ):
            return True
        if any(
            pattern.search(candidate)
            for candidate in candidates
            for pattern in patterns
        ):
            return True
    return _bytes_pattern_hit(patterns, data)


def _evidence_label_is_sensitive(data: bytes) -> bool:
    return text_has(SECRET_PATTERNS + PATH_PATTERNS, data)


def sanitized_evidence_label(label: str, *, private_name: bool = False) -> str:
    return _report.sanitized_evidence_label(
        label, sensitive=_evidence_label_is_sensitive, private_name=private_name
    )


def repository_evidence_label(repo: Path) -> str:
    return sanitized_evidence_label(repo.name or "<repository>")


def same_directory(left: Path, right: Path) -> bool:
    # Windows では大文字小文字・8.3短縮名・junction で文字列が食い違うため、
    # 文字列比較ではなく inode 相当で突き合わせる。stat できない場合だけ
    # normcase による文字列比較へ落とす
    try:
        return left.samefile(right)
    except OSError:
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _identity_git_raw(repo_path, *args):
    # Raw commit framing and trailing controls must survive the caller unchanged.
    result = run_subprocess(
        ["git", "-c", "log.showSignature=false", *args],
        cwd=Path(repo_path),
        capture_output=True,
        shell=False,
    )
    if result.returncode or not isinstance(result.stdout, bytes):
        raise RuntimeError("identity_history_unavailable")
    return result.stdout.decode("utf-8")


def _identity_batch_command(repo_path, *args):
    # The streaming reader uses Popen; apply run_subprocess's same Git hardening.
    cmd = ["git", "-C", str(repo_path), "-c", "log.showSignature=false", *args]
    return git_hardened_command(cmd), git_isolation_env()


def load_identity_gate():
    """同梱 identity_gate.py を file から読む。sys.path と cwd では選ばない。"""
    script = Path(__file__).resolve().with_name("identity_gate.py")
    existing = sys.modules.get("identity_gate")
    origin = getattr(existing, "__file__", None) if existing is not None else None
    if origin is not None and Path(origin).resolve() == script:
        return existing
    spec = importlib.util.spec_from_file_location("identity_gate", script)
    if spec is None or spec.loader is None:
        raise ImportError("identity_gate_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def diagnostic_evidence_label(label, private_identity=None):
    if label == "<redacted-path>":
        return label
    history = re.fullmatch(r"history:([0-9a-f]{12})(?::.*)?", label, re.DOTALL)
    if history:
        return "history:" + history[1]
    if label in {
        "target-diff:working-tree",
        "target-diff:untracked-worktree",
    } or re.fullmatch(r"target-diff:commit:[0-9a-f]{12}", label):
        return label
    try:
        if (
            private_identity is not None
            and private_identity.label_status(label) != "public"
        ):
            return "<redacted-path>"
        return load_identity_gate().Diagnostics().safe_path(label) or "<redacted-path>"
    except (
        OSError,
        ValueError,
        ImportError,
        AttributeError,
        RuntimeError,
        SyntaxError,
    ):
        return "<redacted-path>"


def _identity_gate_result(
    repo, base_ref, identity_base_ref=None, *, history_roots=("--all",), head_ref="HEAD"
):
    """名義の許可リスト・コミット本文（共著者行は名簿・道具の noreply のメールだけ通す）を検査し必須判定へ返す。
    非公開の型設定が無ければ fail（任意にしない）。"""
    try:
        _ig = load_identity_gate()
        pol = _ig.load_policy()
    except ImportError:
        return {"status": "tool_error", "reason": "identity_gate_unavailable"}
    except SystemExit:
        return {"status": "fail", "reason": "private_patterns_not_configured"}
    except (OSError, UnicodeError, ValueError, TypeError, re.error):
        return {"status": "tool_error", "reason": "private_patterns_invalid"}

    rng = f"{base_ref}..{head_ref}" if base_ref else history_roots
    try:
        diagnostics = _ig.Diagnostics(pol)
        issues = _ig.check_range(
            pol,
            str(repo),
            rng,
            identity_rng=(
                f"{identity_base_ref}..{head_ref}" if identity_base_ref else None
            ),
            git_fn=_identity_git_raw,
            diagnostics=diagnostics,
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return {"status": "tool_error", "reason": "identity_history_unavailable"}
    return {
        "status": "fail" if issues else "pass",
        "issue_count": len(issues),
        **(diagnostics.as_report() if issues else {}),
    }


def public_attribution_result(
    repo, base_ref, allowed_identities, *, history_roots=("--all",)
):
    if allowed_identities is None:
        return {"status": "not_evaluated", "reason": "public_allowlist_not_configured"}
    try:
        identity_gate = load_identity_gate()
        diagnostics = identity_gate.Diagnostics(allowed_identities)
        issues = identity_gate.check_public_range(
            allowed_identities,
            str(repo),
            f"{base_ref}..HEAD" if base_ref else history_roots,
            git_fn=_identity_git_raw,
            batch_command=_identity_batch_command,
            diagnostics=diagnostics,
        )
    except ImportError:
        return {"status": "tool_error", "reason": "public_attribution_unavailable"}
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return {"status": "tool_error", "reason": "public_attribution_unavailable"}
    return {
        "status": "fail" if issues else "pass",
        "issue_count": len(issues),
        **(diagnostics.as_report() if issues else {}),
    }


def load_allowed_identities(path: Path):
    """許可名義の一覧を identity_gate の同じ読み手で読む（判定の中身を1つにする）。"""
    try:
        return load_identity_gate().parse_public_policy(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, ImportError) as exc:
        raise RuntimeError("allowed_identities_invalid") from exc


def _coauthor_line_blocked(
    line: str, policy=None, expected_identity: str | None = None
) -> bool:
    return _identity._coauthor_line_blocked(
        line, policy, expected_identity, load_gate=load_identity_gate
    )


def _identity_line_mismatch(
    line: str, expected_identity: str | None, policy=None
) -> bool:
    return _identity._identity_line_mismatch(
        line, expected_identity, policy, load_gate=load_identity_gate
    )


def _identity_line_allowed(policy, line: str) -> bool:
    return _identity._identity_line_allowed(policy, line, load_gate=load_identity_gate)


def _load_path_normalizer():
    source = Path(__file__).resolve().with_name("identity_normalization.py")
    spec = importlib.util.spec_from_file_location(
        "_canonical_path_normalization", source
    )
    normalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(normalizer)
    return normalizer


def normalized_personal_path_matches(data: bytes, normalizer=None) -> Counter:
    return _identity.normalized_personal_path_matches(
        data,
        normalizer,
        load_normalizer=_load_path_normalizer,
        personal_path_matches=personal_path_matches,
    )


class IdentityPort:
    """部品の名義検査が窓口の判定を呼ぶための口。呼ばれた時点の窓口の名前へ転送する。"""

    @staticmethod
    def media_kind(data, source_names=()):
        return media_kind(data, source_names)

    @staticmethod
    def media_candidate(data, source_names=()):
        return media_candidate(data, source_names)

    @staticmethod
    def secret_matches(data):
        return secret_matches(data)

    @staticmethod
    def secret_text_has(data):
        return text_has(SECRET_PATTERNS, data)

    @staticmethod
    def diagnostic_evidence_label(label, private_identity=None):
        return diagnostic_evidence_label(label, private_identity)


class PrivateIdentityScan(_identity.PrivateIdentityScan):
    """明示された既存private providerを同一bytesから読み、値なしの実検査証拠を返す。

    環境変数・同梱file・providerの実行は窓口に置く。本文の判定は部品の基底が持つ。
    """

    port = IdentityPort()

    def __init__(self, repo: Path, required: bool):
        super().__init__(required)
        source = os.environ.get("PUBLIC_IDENTITY_GUARD_SCRIPT")
        bundled_source = Path(__file__).resolve().with_name("identity_normalization.py")
        policy = os.environ.get("PUBLIC_IDENTITY_DENYLIST")
        if not source and not policy and not required:
            return
        self.report["status"] = "tool_error"
        try:
            if not policy:
                raise ValueError("missing_private_identity_input")
            paths = [Path(source) if source else bundled_source, Path(policy)]
            bundled = paths[0].resolve() == bundled_source
            if bundled:
                # candidate内のaliasを再度開かず、実行中scannerの固定同梱pathを読む。
                paths[0] = bundled_source
            for index, path in enumerate(paths):
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("invalid_private_identity_input")
                # 実行中scannerと同じ採用済み同梱moduleだけはself-scanでも直接使える。
                # repo内の任意pluginやpolicyを実行・読込する許可には広げない。
                if not (index == 0 and bundled) and (
                    path.is_relative_to(repo)
                    or path.parent.resolve().is_relative_to(repo)
                    or path.resolve().is_relative_to(repo)
                ):
                    raise ValueError("invalid_private_identity_input")
            provider_bytes, policy_bytes = (path.read_bytes() for path in paths)
            provider = types.ModuleType("_explicit_private_identity_provider")
            provider.__file__ = str(paths[0])
            # Operatorが明示したproviderのみ実行。検査対象の本文はコードとして評価しない。
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exec(compile(provider_bytes, str(paths[0]), "exec"), provider.__dict__)
                patterns = provider.parse_denylist_text(policy_bytes.decode("utf-8"))
            if (
                not isinstance(patterns, list)
                or not patterns
                or not all(isinstance(v, str) and v.strip() for v in patterns)
            ):
                raise ValueError("invalid_private_identity_policy")
            if not callable(provider.scan_private_text):
                raise ValueError("invalid_private_identity_provider")
            self.jpeg_checker = (
                getattr(provider, "scan_private_jpeg", None) if bundled else None
            )
            if bundled:
                self.path_normalizer = provider
                path_source_bytes = provider_bytes
            else:
                path_source_bytes = bundled_source.read_bytes()
                self.path_normalizer = types.ModuleType("_canonical_path_normalizer")
                self.path_normalizer.__file__ = str(bundled_source)
                exec(
                    compile(path_source_bytes, str(bundled_source), "exec"),
                    self.path_normalizer.__dict__,
                )
            if not callable(self.path_normalizer.static_string_views):
                raise ValueError("canonical_path_normalizer_unavailable")
            self.path_normalizer_sha256 = hashlib.sha256(path_source_bytes).hexdigest()
            self.patterns = patterns
            self.checker = provider.scan_private_text
            self.repository_name_checker = (
                getattr(provider, "scan_private_repository_name", None)
                if bundled
                else None
            )
            self.gif_checker = (
                getattr(provider, "scan_private_gif", None) if bundled else None
            )
            self.report["gif_decoder_version"] = _optional_distribution_version(
                "Pillow"
            )
            try:
                from PIL import ImageFile, GifImagePlugin

                self.report["gif_decoder_settings"] = {
                    "truncated_images": bool(ImageFile.LOAD_TRUNCATED_IMAGES),
                    "palette_strategy": int(GifImagePlugin.LOADING_STRATEGY),
                }
            except ImportError:
                self.report["gif_decoder_settings"] = None

            self.caff_checker = (
                getattr(provider, "scan_private_caff", None) if bundled else None
            )
            self.caff_image_cache = {}
            self.report["caff_decoder_sha256"] = getattr(
                provider, "CAFF_DECODER_SHA256", None
            )
            self.psd_checker = (
                getattr(provider, "scan_private_psd", None) if bundled else None
            )
            if self.psd_checker is not None:
                from importlib.metadata import PackageNotFoundError, version

                try:
                    self.report["psd_decoder_version"] = version("psd-tools")
                except PackageNotFoundError:
                    self.report["psd_decoder_version"] = "unavailable"

            self.png_checker = (
                getattr(provider, "scan_private_png", None) if bundled else None
            )
            self.media_checker = None
            if bundled:
                media_source = bundled_source.with_name("media_identity.py")
                media_bytes = media_source.read_bytes()
                media_provider = types.ModuleType("_bundled_media_adapter")
                media_provider.__file__ = str(media_source)
                exec(
                    compile(media_bytes, str(media_source), "exec"),
                    media_provider.__dict__,
                )
                self.media_checker = media_provider.scan_private_media
                self.report["media_adapter_sha256"] = hashlib.sha256(
                    media_bytes
                ).hexdigest()
                self.report["media_clean_cache"] = (
                    "disabled_decoder_environment_unbound"
                )
            self.report.update(
                status="pass",
                provider_sha256=hashlib.sha256(provider_bytes).hexdigest(),
                policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
                provider_kind="bundled_canonical" if bundled else "explicit_external",
                normalized_path_provider_sha256=self.path_normalizer_sha256,
            )
        except (Exception, SystemExit):
            self.report["reason"] = "private_identity_input_unverified"


IDENTITY_SCAN_CACHE_ENV = "REPO_PREFLIGHT_IDENTITY_SCAN_CACHE"


def _scanner_source_sha256() -> str:
    return scanner_bundle_sha256()


def _optional_distribution_version(name: str) -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return "absent"


def _identity_cache_setting() -> str:
    return os.environ.get(IDENTITY_SCAN_CACHE_ENV, "")


def _identity_cache_home() -> str:
    return os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")


def sanitized_ref_report(
    report: dict,
    refs: tuple[str | None, ...],
    private_identity: PrivateIdentityScan | None,
) -> dict:
    return _report.sanitized_ref_report(
        report, refs, private_identity, sensitive=_evidence_label_is_sensitive
    )


def fixed_history_revisions(repo: Path, rev_args: tuple[str, ...]) -> tuple[str, ...]:
    return _history.fixed_history_revisions(repo, rev_args, port=HistoryPort())


# 通常objectのbatch予算。超過は原則として未検査に残す。
# 同じbyteを既にCAFF形式の上限内で検証したblobだけ、単独batchで読み直す。
_HISTORY_BATCH_BUDGET_BYTES = 16_000_000
_MEDIA_METADATA_DECODED_BUDGET_BYTES = 16_000_000


def _load_media_providers(identity):
    source = Path(__file__).resolve().with_name("media_metadata.py")
    source_bytes = source.read_bytes()
    module = types.ModuleType("_bundled_media_metadata")
    module.__file__ = str(source)
    exec(compile(source_bytes, str(source), "exec"), module.__dict__)
    media_source = Path(__file__).resolve().with_name("media_identity.py")
    media_bytes = media_source.read_bytes()
    if hashlib.sha256(media_bytes).hexdigest() != identity.report.get(
        "media_adapter_sha256"
    ):
        raise RuntimeError("media_metadata_provider_changed")
    media = types.ModuleType("_metadata_probe_runner")
    media.__file__ = str(media_source)
    exec(compile(media_bytes, str(media_source), "exec"), media.__dict__)
    return types.SimpleNamespace(
        metadata=module,
        metadata_bytes=source_bytes,
        probe=media,
        unchanged=lambda: source.read_bytes() == source_bytes
        and media_source.read_bytes() == media_bytes,
    )


def _inspect_history_media(repo, object_id, size, names, identity):
    return _media._inspect_history_media(
        repo,
        object_id,
        size,
        names,
        identity,
        git=GitPort(),
        load_providers=_load_media_providers,
        secret_matches=secret_matches,
        personal_path_matches=personal_path_matches,
        decoded_budget_bytes=_MEDIA_METADATA_DECODED_BUDGET_BYTES,
    )


def changed_committed_entries(repo: Path, revisions: tuple[str, ...]) -> list[tuple]:
    return _history.changed_committed_entries(repo, revisions, port=HistoryPort())


def changed_committed_media_entries(
    repo: Path, revisions: tuple[str, ...]
) -> dict[str, str]:
    return _media.changed_committed_media_entries(
        changed_committed_entries(repo, revisions)
    )


def submission_text(data: bytes, names, opaque_suffixes=frozenset()) -> str | None:
    return _history.submission_text(data, names, opaque_suffixes, port=HistoryPort())


def verified_parent_blob(repo: Path, oid: str) -> bytes | None:
    return _history.verified_parent_blob(repo, oid, port=HistoryPort())


def private_commit_identity_view(data: bytes, expected_identity: str) -> bytes:
    return _history.private_commit_identity_view(
        data, expected_identity, port=HistoryPort()
    )


def history_hits(
    repo: Path,
    rev_args: tuple[str, ...] = ("--all",),
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    private_identity: PrivateIdentityScan | None = None,
    *,
    check_repository_names: bool = False,
    approved_private_commit_identity: str | None = None,
    check_raw_commit_payloads: bool = False,
    check_full_history_metadata: bool = False,
    initial_private_save: bool = False,
) -> tuple[list[str], list[str]]:
    return _history.history_hits(
        repo,
        rev_args,
        reviewed_exceptions,
        private_identity,
        check_repository_names=check_repository_names,
        approved_private_commit_identity=approved_private_commit_identity,
        check_raw_commit_payloads=check_raw_commit_payloads,
        check_full_history_metadata=check_full_history_metadata,
        initial_private_save=initial_private_save,
        port=HistoryPort(),
    )


def diff_added_lines_have(
    repo: Path,
    patterns: tuple[re.Pattern, ...],
    *diff_args: str,
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    scan_path: str | None = None,
) -> bool:
    return _history.diff_added_lines_have(
        repo,
        patterns,
        *diff_args,
        reviewed_exceptions=reviewed_exceptions,
        scan_path=scan_path,
        port=HistoryPort(),
    )


def _bytes_pattern_hit(patterns: tuple[re.Pattern, ...], data: bytes) -> bool:
    """Search ``patterns`` directly against raw bytes, without decoding.

    ``text_has``/``_decode_scannable_text`` require the *entire* buffer to
    decode cleanly as UTF-8 or UTF-16 before searching it. A genuinely binary
    blob (e.g. an image or executable) with non-UTF-8 noise elsewhere fails
    that whole-buffer decode even when it also contains an ASCII personal
    path, so the match was silently skipped. All patterns here are ASCII-only,
    so matching their pattern text against raw bytes finds embedded hits
    without needing the surrounding noise to decode at all.
    """
    if PATH_PATTERNS[1] in patterns and _PATH_GRAMMAR.macos_shared_parent_matches(data):
        return True
    return any(re.search(pattern.pattern.encode("ascii"), data) for pattern in patterns)


def _load_caff_decoder():
    import runpy

    return runpy.run_path(str(Path(__file__).with_name("caff_archive.py")))


def cmo3_contents_xml(data: bytes) -> bytes | None:
    return _media.cmo3_contents_xml(
        data,
        max_xml_bytes=CMO3_MAX_XML_BYTES,
        load_caff_decoder=_load_caff_decoder,
    )


def comparison_parent(repo: Path, base_ref: str, commit: str) -> str:
    return _history.comparison_parent(repo, base_ref, commit, port=HistoryPort())


def target_diff_personal_path_hits(
    repo: Path,
    base_ref: str,
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
) -> list[str]:
    return _history.target_diff_personal_path_hits(
        repo, base_ref, reviewed_exceptions, port=HistoryPort()
    )


def working_tree_files(repo: Path) -> list[Path]:
    tracked = run_subprocess(
        ["git", "ls-files", "-z", "-v", "--stage"],
        cwd=repo,
        capture_output=True,
    )
    untracked = run_subprocess(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
    )
    if tracked.returncode or untracked.returncode:
        raise RuntimeError("git_worktree_inventory_failed")
    paths: list[Path] = []
    for entry in tracked.stdout.split(b"\0"):
        if not entry:
            continue
        tag, marker, entry = entry.partition(b" ")
        if not marker or len(tag) != 1:
            raise RuntimeError("git_worktree_inventory_failed")
        metadata, separator, raw_name = entry.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise RuntimeError("git_worktree_inventory_failed")
        mode = fields[0]
        if mode in {b"120000", b"160000"}:
            continue
        if not mode.startswith(b"100"):
            raise RuntimeError("git_worktree_inventory_failed")
        path = repo / raw_name.decode("utf-8", errors="surrogateescape")
        _guard_worktree_parent_components(repo, path)
        if tag.upper() == b"S" and not path.exists():
            continue
        paths.append(path)
    for raw_name in untracked.stdout.split(b"\0"):
        if raw_name:
            path = repo / raw_name.decode("utf-8", errors="surrogateescape")
            _guard_worktree_parent_components(repo, path)
            paths.append(path)
    return paths


def deleted_working_tree_files(repo: Path) -> set[Path]:
    result = run_subprocess(
        ["git", "ls-files", "-z", "--deleted"], cwd=repo, capture_output=True
    )
    if result.returncode:
        raise RuntimeError("git_worktree_inventory_failed")
    return {
        repo / name.decode("utf-8", errors="surrogateescape")
        for name in result.stdout.split(b"\0")
        if name
    }


def submission_files(repo: Path, diff_paths: list[Path]) -> list[Path]:
    """差分のfileに、未commitの追加・変更を足す。差分に無い追跡fileは含めない。"""
    untracked = run_subprocess(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
    )
    staged = run_subprocess(
        ["git", "diff", "--name-only", "-z", "--cached"],
        cwd=repo,
        capture_output=True,
    )
    unstaged = run_subprocess(
        ["git", "diff", "--name-only", "-z"],
        cwd=repo,
        capture_output=True,
    )
    if untracked.returncode or staged.returncode or unstaged.returncode:
        raise RuntimeError("git_worktree_inventory_failed")
    paths = list(diff_paths)
    seen = {path.resolve() for path in diff_paths}
    for raw in (untracked.stdout, staged.stdout, unstaged.stdout):
        for raw_name in raw.split(b"\0"):
            if not raw_name:
                continue
            path = repo / raw_name.decode("utf-8", errors="surrogateescape")
            _guard_worktree_parent_components(repo, path)
            if path.resolve() in seen or not path.exists() or path.is_symlink():
                continue
            seen.add(path.resolve())
            paths.append(path)
    return paths


def changed_working_tree_files(
    repo: Path, base_ref: str
) -> tuple[list[Path], set[Path]]:
    result = run_subprocess(
        ["git", "diff", "--name-only", "-z", f"{base_ref}...HEAD"],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    paths: list[Path] = []
    deleted: set[Path] = set()
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        rel = raw_name.decode("utf-8", errors="surrogateescape")
        path = repo / rel
        _guard_worktree_parent_components(repo, path)
        if path.exists():
            mode_probe = run_subprocess(
                ["git", "ls-files", "--stage", "--", rel],
                cwd=repo,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                capture_output=True,
            )
            if mode_probe.returncode:
                raise RuntimeError("git_target_diff_inventory_failed")
            entries = [line for line in mode_probe.stdout.splitlines() if line]
            if not entries:
                # A committed deletion may retain an ignored local original.
                # Prove absence from HEAD before excluding its nondelivery copy.
                head_probe = run_subprocess(
                    ["git", "--literal-pathspecs", "ls-tree", "-z", "HEAD", "--", rel],
                    cwd=repo,
                    capture_output=True,
                )
                if head_probe.returncode:
                    raise RuntimeError("git_target_diff_inventory_failed")
                if not head_probe.stdout:
                    deleted.add(path)
                    continue
            if len(entries) != 1:
                raise RuntimeError("git_target_diff_inventory_failed")
            mode = entries[0].split(maxsplit=1)[0]
            if mode in {"120000", "160000"}:
                continue
            if not mode.startswith("100"):
                raise RuntimeError("git_target_diff_inventory_failed")
            paths.append(path)
        else:
            deleted.add(path)
    return paths, deleted


def run_readme_release_gate(repo: Path) -> dict:
    script = Path(__file__).with_name("readme_release_gate.py")
    spec = importlib.util.spec_from_file_location("readme_release_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module.review(repo / "README.md")


def load_dialogue_gate():
    script = Path(__file__).with_name("dialogue_gate.py")
    spec = importlib.util.spec_from_file_location("dialogue_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    # importlib 経由でもモジュール属性参照できるよう登録する
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_preferences_module():
    script = Path(__file__).with_name("preferences.py")
    spec = importlib.util.spec_from_file_location("preferences", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_github_settings_module():
    script = Path(__file__).with_name("github_settings_gate.py")
    spec = importlib.util.spec_from_file_location("github_settings_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_consistency_gate(
    repo: Path, base_ref: str | None, *, initial_private_save: bool = False
) -> dict:
    script = Path(__file__).with_name("consistency_gate.py")
    spec = importlib.util.spec_from_file_location("consistency_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if initial_private_save:
        return module.check(repo, initial_private_save=True)
    return module.check(repo, base_ref=base_ref)


def probe_consistency_base_ref(
    repo: Path, consistency_base_ref: str, *, release: bool = False
) -> tuple[str, str] | None:
    """使用可能なconsistency baseなら(symbolic, oid)、不可ならNone。

    条件は明示指定時と同じ: commit として解決でき、symbolic-full-name が
    refs/remotes/origin/配下、release時だけrefs/tags/も許可し、HEADの祖先であること。
    """
    consistency_probe = run(
        repo, "git", "rev-parse", "--verify", f"{consistency_base_ref}^{{commit}}"
    )
    consistency_symbolic = run(
        repo, "git", "rev-parse", "--symbolic-full-name", consistency_base_ref
    )
    consistency_ancestor = run(
        repo, "git", "merge-base", "--is-ancestor", consistency_base_ref, "HEAD"
    )
    if (
        consistency_probe[0]
        or consistency_symbolic[0]
        or not (
            consistency_symbolic[1].startswith("refs/remotes/origin/")
            or (release and consistency_symbolic[1].startswith("refs/tags/"))
        )
        or consistency_ancestor[0]
    ):
        return None
    return consistency_symbolic[1], consistency_probe[1]


def check_release_readme_freshness(
    repo: Path, *, baseline: tuple[str, str] | None = None
) -> dict:
    """直近の到達可能なsemver tagからREADMEの実内容更新を検査する。"""
    error = {"status": "tool_error", "reason": "release_readme_baseline_unavailable"}
    code, shallow = run(repo, "git", "rev-parse", "--is-shallow-repository")
    if code or shallow != "false":
        return error
    if baseline is not None:
        ref, oid = baseline
        if not ref.startswith("refs/tags/"):
            return error
        code, current_oid = run(
            repo, "git", "rev-parse", "--verify", f"{ref}^{{commit}}"
        )
        ancestor_code, _ = run(repo, "git", "merge-base", "--is-ancestor", oid, "HEAD")
        if code or current_oid != oid or ancestor_code:
            return error
    else:
        code, tags = run(
            repo, "git", "for-each-ref", "--format=%(refname)", "refs/tags/"
        )
        if code:
            return error
        reachable: list[str] = []
        for ref in tags.splitlines():
            if not re.fullmatch(
                r"refs/tags/v?(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
                r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
                ref,
            ):
                continue
            code, _ = run(repo, "git", "merge-base", "--is-ancestor", ref, "HEAD")
            if code not in (0, 1):
                return error
            if code == 0:
                reachable.append(ref)
        if not reachable:
            return {"status": "pass", "reason": "first_release", "baseline": None}
        # git describeは履歴上で最も近いtagを選び、古い系列の高い番号と混同しない。
        args = ["git", "describe", "--tags", "--abbrev=0"]
        for ref in reachable:
            args.extend(("--match", ref.removeprefix("refs/tags/")))
        code, tag = run(repo, *args, "HEAD")
        if code:
            return error
        ref = f"refs/tags/{tag}"
        if ref not in reachable:
            return error
        code, oid = run(repo, "git", "rev-parse", "--verify", f"{ref}^{{commit}}")
        if code:
            return error
    before = run_subprocess(
        ["git", "show", f"{oid}:README.md"], cwd=repo, capture_output=True
    )
    path = repo / "README.md"
    try:
        if before.returncode or path.is_symlink():
            return error
        previous_text = before.stdout.decode("utf-8")
        current_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return error
    changed = "".join(previous_text.split()) != "".join(current_text.split())
    return {
        "status": "pass" if changed else "fail",
        "reason": "readme_updated" if changed else "release_readme_update_missing",
        "baseline": {"resolved_base_ref": ref, "base_oid": oid},
        "content_changed": changed,
    }


def repository_root_error(repo: Path) -> dict | None:
    """repo が repository root でなければ tool_error を返す。root なら None。

    --show-toplevel は「そのpathを含むrepo」を返す。囲っているrepoへ黙って
    広げると、利用者が指したのと別のrepositoryの判定を、指したpathの判定と
    して返してしまう。公開判定では取り違えが致命的なので閉じる側で止める。
    """
    repo = repo.resolve()
    if not repo.is_dir():
        return {"status": "tool_error", "issues": ["not_git_repository"]}
    git_code, top = run(repo, "git", "rev-parse", "--show-toplevel")
    if git_code:
        return {"status": "tool_error", "issues": ["not_git_repository"]}
    root = Path(top).resolve()
    if not same_directory(repo, root):
        return {
            "status": "tool_error",
            "issues": ["repo_path_is_not_repository_root"],
            "repository_root": repository_evidence_label(root),
        }
    return None


def _private_submission_repository(remote: str) -> str | None:
    """Credential-free, unambiguous GitHub.com transport only; never echo input."""
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9_.-]+?)(?:\.git)?",
        remote,
    )
    if not match or match[2] in {".", ".."}:
        return None
    return f"{match[1]}/{match[2]}"


def initial_save_branch(repo: Path) -> str | None:
    result = run_subprocess(
        ["git", "symbolic-ref", "--quiet", "HEAD"], cwd=repo, capture_output=True
    )
    try:
        ref = result.stdout.removesuffix(b"\n").decode("utf-8")
    except UnicodeDecodeError:
        return None
    return ref if result.returncode == 0 and ref.startswith("refs/heads/") else None


def github_push_origin(repo: Path, expected: str | None) -> tuple[str, str]:
    for flags in (("--all",), ("--all", "--push")):
        result = run_subprocess(
            ["git", "remote", "get-url", *flags, "origin"],
            cwd=repo,
            capture_output=True,
        )
        # Preserve whitespace and additional URLs: neither identifies a single canonical destination.
        url = result.stdout.removesuffix(b"\n").decode("ascii", errors="replace")
        destination = _private_submission_repository(url)
        if (
            result.returncode
            or destination is None
            or (expected is not None and destination.lower() != expected.lower())
        ):
            raise RuntimeError("push_destination_origin_unverified")
        expected = destination.lower()
    return expected, url


def live_push_destination(
    repo: Path, expected: str | None = None, *, base_ref: str | None = None
) -> dict:
    """Bind GitHub state to one effective origin without executing repository transports."""
    expected, push_url = github_push_origin(repo, expected)
    owner, name = expected.split("/")
    base_variable = ""
    base_selection = ""
    base_args = []
    if base_ref is not None:
        prefix = "refs/remotes/origin/"
        if not base_ref.startswith(prefix) or base_ref == prefix + "HEAD":
            raise RuntimeError("push_destination_base_unverified")
        branch = "refs/heads/" + base_ref[len(prefix) :]
        base_variable = ",$base:String!"
        base_selection = " baseline:ref(qualifiedName:$base){target{oid}}"
        base_args = ["-f", "base=" + branch]
    query = (
        "query($owner:String!,$name:String!"
        + base_variable
        + "){repository(owner:$owner,name:$name)"
        '{id nameWithOwner visibility isEmpty heads:refs(refPrefix:"refs/heads/",first:1)'
        '{totalCount} tags:refs(refPrefix:"refs/tags/",first:1){totalCount}'
        + base_selection
        + "}}"
    )
    try:
        result = run_subprocess(
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "graphql",
                "-f",
                "query=" + query,
                "-f",
                "owner=" + owner,
                "-f",
                "name=" + name,
                *base_args,
            ],
            capture_output=True,
            timeout=20,
            check=True,
        )
        response = json.loads(result.stdout)
        live = response["data"]["repository"]
        counts = [live["heads"]["totalCount"], live["tags"]["totalCount"]]
        if (
            not isinstance(live["id"], str)
            or not live["id"]
            or any(type(count) is not int or count < 0 for count in counts)
            or response.get("errors")
            or not isinstance(live["nameWithOwner"], str)
            or live["nameWithOwner"].lower() != expected
            or live["visibility"] not in {"PRIVATE", "PUBLIC", "INTERNAL"}
            or type(live["isEmpty"]) is not bool
        ):
            raise ValueError("repository_state")
        live_base = live["baseline"]["target"]["oid"] if base_ref is not None else None
        if base_ref is not None and (
            not isinstance(live_base, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", live_base)
        ):
            raise ValueError("repository_base")
    except (
        OSError,
        subprocess.SubprocessError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise RuntimeError("push_destination_live_verification_failed") from exc
    if github_push_origin(repo, expected) != (expected, push_url):
        raise RuntimeError("push_destination_origin_changed")
    return {
        "repository_sha256": hashlib.sha256(expected.encode("ascii")).hexdigest(),
        "repository_id_sha256": hashlib.sha256(live["id"].encode("utf-8")).hexdigest(),
        "push_url_sha256": hashlib.sha256(push_url.encode("ascii")).hexdigest(),
        "visibility": live["visibility"].lower(),
        "empty": live["isEmpty"],
        "branch_count": counts[0],
        "tag_count": counts[1],
        "push_repository_verified": True,
        "verification": "live_github_api",
        **({"live_base_oid": live_base} if live_base is not None else {}),
    }


def verify_initial_private_save(repo: Path, expected: str) -> dict:
    proof = live_push_destination(repo, expected)
    if (
        proof["visibility"] != "private"
        or not proof["empty"]
        or proof["branch_count"]
        or proof["tag_count"]
    ):
        raise RuntimeError("initial_private_save_requires_empty_private_repository")
    return proof


def verify_private_submission(
    repo: Path, remote: str, base_ref: str, base_oid: str
) -> dict:
    """Bind a committed submission to live private origin and its exact base."""
    identity = _private_submission_repository(remote)
    code, push_urls = run(repo, "git", "remote", "get-url", "--all", "--push", "origin")
    urls = push_urls.splitlines()
    if (
        identity is None
        or code
        or len(urls) != 1
        or _private_submission_repository(urls[0]) != identity
    ):
        raise RuntimeError("private_submission_origin_unverified")
    prefix = "refs/remotes/origin/"
    branch = base_ref.removeprefix(prefix)
    if not base_ref.startswith(prefix) or branch == "HEAD":
        raise RuntimeError("private_submission_base_unverified")
    try:
        from urllib.parse import quote

        settings = load_github_settings_module()
        endpoint = "https://api.github.com/repos/" + identity
        live = settings.gh_api_get(endpoint)
        if (
            not isinstance(live, dict)
            or live.get("full_name") != identity
            or live.get("private") is not True
            or live.get("visibility") != "private"
        ):
            raise RuntimeError("private_submission_visibility_unverified")
        ref = settings.gh_api_get(endpoint + "/git/ref/heads/" + quote(branch, safe=""))
        if (
            not isinstance(ref, dict)
            or ref.get("ref") != "refs/heads/" + branch
            or not isinstance(ref.get("object"), dict)
            or ref["object"].get("type") != "commit"
            or ref["object"].get("sha") != base_oid
        ):
            raise RuntimeError("private_submission_base_unverified")
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        # API failures and raw remote data must never become proof or diagnostics.
        raise RuntimeError("private_submission_live_verification_failed") from exc
    return {
        "repository": "<private-repository>",
        "repository_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "visibility": "private",
        "verification": "live_github_api",
        "push_repository_verified": True,
        "base_oid": base_oid,
        "live_base_oid": base_oid,
    }


class _ScanTarget(NamedTuple):
    """git で確かめた走査の対象。_resolve_scan_target が作り、以後の段は書き換えない。"""

    repo: Path
    report_repo_label: str
    history_roots: tuple[str, ...]
    identity_base: tuple[str, str] | None
    head: str
    dirty: str
    identities: str
    historical_identities: str
    resolved_base_ref: str | None
    base_oid: str | None
    consistency_base_ref: str | None
    resolved_consistency_base_ref: str | None
    consistency_base_oid: str | None
    remote: str


def scan(
    repo: Path,
    expected_identity: str | None = None,
    *,
    release: bool = False,
    base_ref: str | None = None,
    private_submission: bool = False,
    consistency_base_ref: str | None = None,
    identity_base_ref: str | None = None,
    reviewed_secret_exceptions: Path | None = None,
    require_private_identity: bool = False,
    allowed_identities: Path | None = None,
    media_inspection: str = "strict",
    allow_expected_private_commit_identity: bool = False,
    initial_private_save: str | None = None,
) -> dict:
    scope_error = _scan_scope_error(
        initial_private_save=initial_private_save,
        base_ref=base_ref,
        consistency_base_ref=consistency_base_ref,
        identity_base_ref=identity_base_ref,
        private_submission=private_submission,
        release=release,
        expected_identity=expected_identity,
        allow_expected_private_commit_identity=allow_expected_private_commit_identity,
        media_inspection=media_inspection,
        reviewed_secret_exceptions=reviewed_secret_exceptions,
    )
    if scope_error is not None:
        return scope_error
    if initial_private_save is not None:
        require_private_identity = True
    target = _resolve_scan_target(
        repo,
        expected_identity=expected_identity,
        release=release,
        base_ref=base_ref,
        private_submission=private_submission,
        consistency_base_ref=consistency_base_ref,
        identity_base_ref=identity_base_ref,
        initial_private_save=initial_private_save,
    )
    if isinstance(target, dict):
        return target
    inputs = _load_scan_inputs(
        target,
        initial_private_save=initial_private_save,
        private_submission=private_submission,
        reviewed_secret_exceptions=reviewed_secret_exceptions,
        allowed_identities=allowed_identities,
    )
    if isinstance(inputs, dict):
        return inputs
    (
        initial_proof,
        push_ref,
        private_submission_proof,
        reviewed_exceptions,
        allowed_identity_set,
    ) = inputs
    missing, invalid_documents = _required_documents(target, base_ref=base_ref)
    worktree = _scan_worktree(
        target,
        base_ref=base_ref,
        private_submission=private_submission,
        initial_private_save=initial_private_save,
        require_private_identity=require_private_identity,
        media_inspection=media_inspection,
        push_ref=push_ref,
        reviewed_exceptions=reviewed_exceptions,
        reviewed_secret_exceptions=reviewed_secret_exceptions,
    )
    if isinstance(worktree, dict):
        return worktree
    (
        private_identity,
        credential_finding_count,
        credential_locations,
        path_hits,
        unscanned_path_files,
        private_worktree_enabled,
        private_worktree_inventory_count,
        private_worktree_scanned_count,
        private_worktree_cache_reused,
        private_worktree_symlinks_excluded,
        private_worktree_sensitive_names,
    ) = worktree
    history = _scan_history(
        target,
        base_ref=base_ref,
        private_submission=private_submission,
        initial_private_save=initial_private_save,
        expected_identity=expected_identity,
        allow_expected_private_commit_identity=allow_expected_private_commit_identity,
        private_identity=private_identity,
        reviewed_exceptions=reviewed_exceptions,
        credential_finding_count=credential_finding_count,
        credential_locations=credential_locations,
        path_hits=path_hits,
    )
    if isinstance(history, dict):
        return history
    (
        credential_finding_count,
        credential_locations,
        path_hits,
        identity_gate_report,
    ) = history
    checks = _collect_checks(
        target,
        base_ref=base_ref,
        private_submission=private_submission,
        initial_private_save=initial_private_save,
        release=release,
        expected_identity=expected_identity,
        allowed_identity_set=allowed_identity_set,
        initial_proof=initial_proof,
        reviewed_exceptions=reviewed_exceptions,
        missing=missing,
        invalid_documents=invalid_documents,
        private_identity=private_identity,
        credential_finding_count=credential_finding_count,
        credential_locations=credential_locations,
        path_hits=path_hits,
        unscanned_path_files=unscanned_path_files,
        private_worktree_enabled=private_worktree_enabled,
        private_worktree_inventory_count=private_worktree_inventory_count,
        private_worktree_scanned_count=private_worktree_scanned_count,
        private_worktree_cache_reused=private_worktree_cache_reused,
        private_worktree_symlinks_excluded=private_worktree_symlinks_excluded,
        private_worktree_sensitive_names=private_worktree_sensitive_names,
        identity_gate_report=identity_gate_report,
    )
    report = _aggregate_report(
        target,
        checks,
        base_ref=base_ref,
        private_submission=private_submission,
        release=release,
        require_private_identity=require_private_identity,
        allowed_identity_set=allowed_identity_set,
        private_identity=private_identity,
        private_submission_proof=private_submission_proof,
    )
    refs_error = _recheck_refs(
        target,
        report,
        initial_private_save=initial_private_save,
        initial_proof=initial_proof,
        push_ref=push_ref,
        private_submission=private_submission,
    )
    if refs_error is not None:
        return refs_error
    return sanitized_ref_report(
        report,
        (
            base_ref,
            target.resolved_base_ref,
            target.consistency_base_ref,
            target.resolved_consistency_base_ref,
            identity_base_ref,
            target.identity_base[0] if target.identity_base else None,
        ),
        private_identity,
    )


def _scan_scope_error(
    *,
    initial_private_save: str | None,
    base_ref: str | None,
    consistency_base_ref: str | None,
    identity_base_ref: str | None,
    private_submission: bool,
    release: bool,
    expected_identity: str | None,
    allow_expected_private_commit_identity: bool,
    media_inspection: str,
    reviewed_secret_exceptions: Path | None,
) -> dict | None:
    if initial_private_save is not None:
        if not valid_initial_private_scope(
            initial_private_save,
            base_ref,
            consistency_base_ref,
            identity_base_ref,
            private_submission,
            release,
        ):
            return {
                "status": "tool_error",
                "issues": ["invalid_initial_private_save_scope"],
            }
    if allow_expected_private_commit_identity and (
        not private_submission or not base_ref or release or not expected_identity
    ):
        return {
            "status": "tool_error",
            "issues": ["invalid_private_commit_identity_scope"],
        }
    if allow_expected_private_commit_identity:
        try:
            load_identity_gate().parse_commit_identity(expected_identity + " 0 +0000")
        except (TypeError, ValueError):
            return {
                "status": "tool_error",
                "issues": ["invalid_private_commit_identity_scope"],
            }
    if media_inspection not in {"strict", "metadata"} or (
        media_inspection == "metadata"
        and (
            not private_submission
            or not base_ref
            or release
            or reviewed_secret_exceptions is not None
        )
    ):
        return {"status": "tool_error", "issues": ["invalid_media_inspection_scope"]}
    if private_submission and (
        not base_ref or release or consistency_base_ref or identity_base_ref
    ):
        return {"status": "tool_error", "issues": ["invalid_private_submission_scope"]}
    return None


def _resolve_scan_target(
    repo: Path,
    *,
    expected_identity: str | None,
    release: bool,
    base_ref: str | None,
    private_submission: bool,
    consistency_base_ref: str | None,
    identity_base_ref: str | None,
    initial_private_save: str | None,
) -> _ScanTarget | dict:
    repo = repo.resolve()
    root_error = repository_root_error(repo)
    if root_error is not None:
        if initial_private_save and "repository_root" in root_error:
            root_error["repository_root"] = "<private-repository>"
        if private_submission or initial_private_save:
            root_error["repo"] = "<private-repository>"
        return root_error
    repo = Path(run(repo, "git", "rev-parse", "--show-toplevel")[1]).resolve()
    report_repo_label = (
        "<private-repository>"
        if private_submission or initial_private_save
        else repository_evidence_label(repo)
    )
    history_roots = ()
    snapshot_head = None
    if not base_ref:
        head_code, snapshot_head = run(repo, "git", "rev-parse", "HEAD")
        if head_code:
            return {
                "status": "tool_error",
                "repo": report_repo_label,
                "issues": ["git_probe_failed"],
            }
        try:
            history_roots = fixed_history_revisions(repo, ("--all",))
        except RuntimeError as exc:
            return {
                "status": "tool_error",
                "repo": report_repo_label,
                "issues": [
                    (
                        "shallow_history_unavailable"
                        if str(exc) == "shallow_history_unavailable"
                        else "git_history_inventory_failed"
                    )
                ],
            }
    if identity_base_ref:
        if not release or base_ref or not expected_identity:
            return {"status": "tool_error", "issues": ["invalid_identity_base_ref"]}
        identity_base = probe_consistency_base_ref(repo, identity_base_ref)
        if identity_base is None:
            return {"status": "tool_error", "issues": ["invalid_identity_base_ref"]}
    else:
        identity_base = None
    probes = {
        "head": run(repo, "git", "rev-parse", "HEAD"),
        "dirty": run(repo, "git", "status", "--porcelain"),
    }
    resolved_consistency_base_ref: str | None = None
    consistency_base_oid: str | None = None
    # plain / publish / release で base 未指定のとき、impact_map 付きでも
    # 最短コマンドが change_sensitive_scope_unavailable で落ちないよう
    # origin/main を既定候補にする。usable でなければ従来どおり未指定のまま。
    consistency_base_was_defaulted = False
    if consistency_base_ref is None and base_ref is None and not initial_private_save:
        defaulted = probe_consistency_base_ref(repo, DEFAULT_CONSISTENCY_BASE_REF)
        if defaulted is not None:
            consistency_base_ref = DEFAULT_CONSISTENCY_BASE_REF
            resolved_consistency_base_ref, consistency_base_oid = defaulted
            consistency_base_was_defaulted = True
    if consistency_base_ref and not base_ref and not consistency_base_was_defaulted:
        probed = probe_consistency_base_ref(repo, consistency_base_ref, release=release)
        if probed is None:
            return {"status": "tool_error", "issues": ["invalid_consistency_base_ref"]}
        resolved_consistency_base_ref, consistency_base_oid = probed
    if base_ref:
        base_probe = run(repo, "git", "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        symbolic_probe = run(repo, "git", "rev-parse", "--symbolic-full-name", base_ref)
        ancestor_probe = run(
            repo, "git", "merge-base", "--is-ancestor", base_ref, "HEAD"
        )
        if (
            base_probe[0]
            or symbolic_probe[0]
            or not symbolic_probe[1].startswith("refs/remotes/origin/")
            or ancestor_probe[0]
        ):
            return {
                "status": "tool_error",
                "repo": report_repo_label,
                "issues": ["invalid_non_remote_or_non_ancestor_base_ref"],
            }
        identity_range = (
            f"{base_probe[1]}..{probes['head'][1]}"
            if private_submission
            else f"{base_ref}..HEAD"
        )
        probes["identities"] = run(
            repo,
            "git",
            "log",
            "--format=%an <%ae>|%cn <%ce>%n%(trailers:key=Co-authored-by,valueonly)",
            identity_range,
        )
    elif identity_base is not None:
        probes["identities"] = run(
            repo,
            "git",
            "log",
            "--format=%an <%ae>|%cn <%ce>%n%(trailers:key=Co-authored-by,valueonly)",
            f"{identity_base[1]}..{probes['head'][1]}",
        )
        probes["historical_identities"] = run(
            repo,
            "git",
            "log",
            "--format=%an <%ae>|%cn <%ce>%n%(trailers:key=Co-authored-by,valueonly)",
            *history_roots,
        )
    else:
        probes["identities"] = run(
            repo,
            "git",
            "log",
            "--format=%an <%ae>|%cn <%ce>%n%(trailers:key=Co-authored-by,valueonly)",
            *history_roots,
        )
    if any(code for code, _ in probes.values()):
        return {
            "status": "tool_error",
            "repo": report_repo_label,
            "issues": ["git_probe_failed"],
        }
    head = probes["head"][1]
    if snapshot_head is not None and (
        head != snapshot_head or snapshot_head not in history_roots
    ):
        return {
            "status": "tool_error",
            "repo": report_repo_label,
            "issues": ["git_history_head_changed"],
        }
    dirty = probes["dirty"][1]
    identities = probes["identities"][1]
    resolved_base_ref: str | None = None
    base_oid: str | None = None
    if base_ref:
        resolved_base_ref = symbolic_probe[1]
        base_oid = base_probe[1]
    _, remote = run(repo, "git", "remote", "get-url", "origin")
    return _ScanTarget(
        repo=repo,
        report_repo_label=report_repo_label,
        history_roots=history_roots,
        identity_base=identity_base,
        head=head,
        dirty=dirty,
        identities=identities,
        historical_identities=probes.get("historical_identities", probes["identities"])[
            1
        ],
        resolved_base_ref=resolved_base_ref,
        base_oid=base_oid,
        consistency_base_ref=consistency_base_ref,
        resolved_consistency_base_ref=resolved_consistency_base_ref,
        consistency_base_oid=consistency_base_oid,
        remote=remote,
    )


def _load_scan_inputs(
    target: _ScanTarget,
    *,
    initial_private_save: str | None,
    private_submission: bool,
    reviewed_secret_exceptions: Path | None,
    allowed_identities: Path | None,
) -> dict | tuple:
    initial_proof = None
    push_ref = None
    if initial_private_save:
        push_ref = initial_save_branch(target.repo)
        if push_ref is None:
            return {
                "status": "tool_error",
                "issues": ["initial_private_save_requires_current_branch"],
            }
        try:
            initial_proof = verify_initial_private_save(
                target.repo, initial_private_save
            )
        except RuntimeError as exc:
            return {"status": "tool_error", "issues": [str(exc)]}
    private_submission_proof = None
    if private_submission:
        try:
            private_submission_proof = verify_private_submission(
                target.repo, target.remote, target.resolved_base_ref, target.base_oid
            )
        except RuntimeError as exc:
            return {"status": "tool_error", "issues": [str(exc)]}
    reviewed_exceptions = None
    if reviewed_secret_exceptions is not None:
        try:
            reviewed_exceptions = ReviewedSecretExceptions(
                Path(reviewed_secret_exceptions), target.remote
            )
        except RuntimeError:
            return {
                "status": "tool_error",
                "issues": ["reviewed_secret_exceptions_invalid"],
            }
    allowed_identity_set = None
    if allowed_identities is not None:
        try:
            allowed_identity_set = load_allowed_identities(Path(allowed_identities))
        except RuntimeError:
            return {
                "status": "tool_error",
                "issues": ["allowed_identities_invalid"],
            }
    return (
        initial_proof,
        push_ref,
        private_submission_proof,
        reviewed_exceptions,
        allowed_identity_set,
    )


def _required_documents(
    target: _ScanTarget, *, base_ref: str | None
) -> tuple[list[str], list[str]]:
    missing = (
        []
        if base_ref
        else [name for name in REQUIRED if not (target.repo / name).is_file()]
    )
    invalid_documents: list[str] = []
    review_record = target.repo / REVIEW_RECORD
    if not base_ref and REVIEW_RECORD not in missing:
        try:
            record_text = review_record.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 読めない記録を「存在するからpass」にしない
            invalid_documents.append(REVIEW_RECORD)
        else:
            if REVIEW_RECORD_MARKER not in record_text:
                invalid_documents.append(REVIEW_RECORD)
    return missing, invalid_documents


def _scan_worktree(
    target: _ScanTarget,
    *,
    base_ref: str | None,
    private_submission: bool,
    initial_private_save: str | None,
    require_private_identity: bool,
    media_inspection: str,
    push_ref: str | None,
    reviewed_exceptions: ReviewedSecretExceptions | None,
    reviewed_secret_exceptions: Path | None,
) -> dict | tuple:
    private_identity = PrivateIdentityScan(target.repo, require_private_identity)
    private_identity.media_inspection = media_inspection
    if media_inspection == "metadata":
        private_identity.report["media_inspection"] = "metadata_private_only"
        private_identity.report["media_payload_inspected"] = False
        private_identity.report["media_content_privacy_verified"] = False
    credential_finding_count = 0
    credential_locations: list[str] = []
    path_hits: list[str] = []
    if initial_private_save and private_identity.checker is not None:
        for name in (initial_private_save, push_ref.removeprefix("refs/heads/")):
            raw = name.encode("utf-8", errors="surrogateescape")
            before = private_identity.findings
            private_identity.check(raw, repository_name=True)
            if private_identity.findings > before:
                private_identity.record_location("push_destination", field="name")
            if secret_matches(raw):
                credential_finding_count += 1
                credential_locations.append("<redacted-path>")
            if personal_path_matches(raw) or normalized_personal_path_matches(
                raw, private_identity.path_normalizer
            ):
                path_hits.append("<redacted-path>")
    decoded_media_secrets: set[str] = set()
    unscanned_path_files: list[str] = []
    try:
        if private_submission:
            paths, deleted_paths = [], set()
        elif base_ref:
            paths, deleted_paths = changed_working_tree_files(target.repo, base_ref)
        else:
            paths = working_tree_files(target.repo)
            deleted_paths = deleted_working_tree_files(target.repo)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": target.report_repo_label,
            "issues": ["git_worktree_inventory_failed"],
        }
    # 明示private送信以外は個人識別子を作業treeの在庫全体で見る。
    # 差分に無い既存JPEGを復号できないときだけ、
    # そのfileを未展開として続け、Pull Request全体は止めない。差分と未commit変更のJPEGは止める。
    private_worktree_enabled = (
        private_identity.checker is not None and not private_submission
    )
    private_worktree_inventory_count = 0
    private_worktree_scanned_count = 0
    private_worktree_cache_reused = 0
    private_worktree_symlinks_excluded = 0
    private_worktree_sensitive_names: set[str] = set()
    submission_rels: set[str] | None = None
    if private_worktree_enabled:
        try:
            if base_ref:
                private_paths = working_tree_files(target.repo)
                submission_rels = {
                    path.relative_to(target.repo).as_posix()
                    for path in submission_files(target.repo, paths)
                }
            else:
                private_paths = paths
            private_deleted = deleted_paths
        except RuntimeError:
            return {
                "status": "tool_error",
                "repo": target.report_repo_label,
                "issues": ["git_worktree_inventory_failed"],
            }
        current_paths = []
        for path in private_paths:
            if path in private_deleted:
                continue
            # inventoryが返す未追跡symlinkの参照先をprivate原資料として読まない。
            if path.is_symlink():
                private_worktree_symlinks_excluded += 1
                continue
            current_paths.append(path)
        private_worktree_inventory_count = len(current_paths)
        identity_cache = IdentityScanCache.open(
            target.repo,
            private_identity,
            reviewed_secret_exceptions,
            target.remote,
            setting=_identity_cache_setting,
            cache_home=_identity_cache_home,
            scanner_sha256=_scanner_source_sha256,
            distribution_version=_optional_distribution_version,
        )
        for path in current_paths:
            relative = path.relative_to(target.repo)
            rel = relative.as_posix()
            name_views = [rel]
            # filename metadataだけ最後のsuffixを外す。username文法は変更しない。
            stem = relative.with_suffix("").as_posix()
            if stem != rel:
                name_views.append(stem)
            before_name_findings = private_identity.findings
            for name_view in name_views:
                private_identity.check(name_view.encode("utf-8"), repository_name=True)
                if private_identity.checker is None:
                    break
            if private_identity.findings > before_name_findings:
                private_worktree_sensitive_names.add(rel)
                private_identity.record_location("filename", field="filename")
            if private_identity.checker is None:
                break
            label = sanitized_evidence_label(
                rel,
                private_name=(
                    initial_private_save is not None
                    or rel in private_worktree_sensitive_names
                    or private_identity.report.get("status") == "tool_error"
                ),
            )
            try:
                if not path.is_file():
                    raise OSError("not_regular_file")
                data = path.read_bytes()
            except OSError:
                return {
                    "status": "tool_error",
                    "repo": target.report_repo_label,
                    "issues": [f"worktree_file_unreadable:{label}"],
                }
            content_sha256 = None
            if identity_cache is not None and not media_candidate(data, (rel,)):
                # 同じ鍵で合格済みの内容なら本文検査を省く。ファイル名の検査は毎回行う。
                content_sha256 = hashlib.sha256(data).hexdigest()
                cached = identity_cache.lookup(content_sha256)
                if cached is not None and private_identity.reuse_clean_pass(cached):
                    # 同じhash・デコーダ・方針を確認した控えから形式証拠も引き継ぐ。
                    private_identity._remember_verified_caff(data)
                    private_worktree_scanned_count += 1
                    private_worktree_cache_reused += 1
                    continue
            reviewed_raw = (
                private_identity.checker is not None
                and reviewed_exceptions is not None
                and reviewed_exceptions.all_paths_reviewed(
                    rel,
                    data,
                    record=False,
                    normalized=True,
                    normalizer=private_identity.path_normalizer,
                )
            )
            before_content_findings = private_identity.findings
            private_identity.check(
                data,
                reviewed_raw_paths=reviewed_raw,
                source_names=(rel,),
                defer_unverified_jpeg=(
                    submission_rels is not None and rel not in submission_rels
                ),
            )
            if private_identity.findings > before_content_findings:
                private_identity.record_location("worktree", path=label)
            if private_identity.last_media_secret_matches:
                decoded_media_secrets.add(rel)
            if private_identity.checker is None:
                break
            if (
                identity_cache is not None
                and content_sha256 is not None
                and private_identity.last_clean_pass_digests is not None
            ):
                identity_cache.record(
                    content_sha256, private_identity.last_clean_pass_digests
                )
            private_worktree_scanned_count += 1
        if identity_cache is not None:
            identity_cache.save()
    for path in paths:
        if path in deleted_paths:
            continue
        rel = path.relative_to(target.repo).as_posix()
        label = sanitized_evidence_label(
            rel,
            private_name=(
                initial_private_save is not None
                or rel in private_worktree_sensitive_names
                or private_identity.report.get("status") == "tool_error"
            ),
        )
        if not path.is_file():
            return {
                "status": "tool_error",
                "repo": target.report_repo_label,
                "issues": [f"worktree_file_unreadable:{label}"],
            }
        try:
            data = path.read_bytes()
        except OSError:
            return {
                "status": "tool_error",
                "repo": target.report_repo_label,
                "issues": [f"worktree_file_unreadable:{label}"],
            }
        if (
            reviewed_exceptions.has_unreviewed(rel, data)
            if reviewed_exceptions is not None
            else text_has(SECRET_PATTERNS, data)
        ) or rel in decoded_media_secrets:
            credential_finding_count += 1
            credential_locations.append(
                diagnostic_evidence_label(label, private_identity)
            )
        if base_ref:
            continue
        if (
            reviewed_exceptions.has_unreviewed_paths(rel, data)
            if reviewed_exceptions is not None
            else text_has(PATH_PATTERNS, data)
        ):
            path_hits.append(label)
        elif path.suffix.lower() == CMO3_SUFFIX:
            # 展開できない .cmo3 を「個人pathなし」と数えない (unknown で止める)
            xml = cmo3_contents_xml(data)
            if xml is None:
                unscanned_path_files.append(label)
            elif (
                reviewed_exceptions.has_unreviewed_paths(rel, data, xml)
                if reviewed_exceptions is not None
                else text_has(PATH_PATTERNS, xml)
                or _bytes_pattern_hit(PATH_PATTERNS, xml)
            ):
                path_hits.append(label)
    return (
        private_identity,
        credential_finding_count,
        credential_locations,
        path_hits,
        unscanned_path_files,
        private_worktree_enabled,
        private_worktree_inventory_count,
        private_worktree_scanned_count,
        private_worktree_cache_reused,
        private_worktree_symlinks_excluded,
        private_worktree_sensitive_names,
    )


def _scan_history(
    target: _ScanTarget,
    *,
    base_ref: str | None,
    private_submission: bool,
    initial_private_save: str | None,
    expected_identity: str | None,
    allow_expected_private_commit_identity: bool,
    private_identity: PrivateIdentityScan,
    reviewed_exceptions: ReviewedSecretExceptions | None,
    credential_finding_count: int,
    credential_locations: list[str],
    path_hits: list[str],
) -> dict | tuple:
    identity_gate_report = None
    if allow_expected_private_commit_identity:
        identity_gate_report = _identity_gate_result(
            target.repo, target.base_oid, head_ref=target.head
        )
    approved_private_commit_identity = None
    if (
        allow_expected_private_commit_identity
        and identity_gate_report["status"] == "pass"
    ):
        approved_private_commit_identity = expected_identity
        private_identity.report.update(
            approved_private_commit_identity_fields=0,
            approved_private_commit_identity_sha256=hashlib.sha256(
                expected_identity.encode("utf-8")
            ).hexdigest(),
            approved_private_commit_identity_scope="live_private_committed_submission_author_committer_only",
        )
    try:
        history_credential_findings, history_path_hits = history_hits(
            target.repo,
            (
                (f"{target.base_oid}..{target.head}",)
                if private_submission
                else (f"{base_ref}..HEAD",) if base_ref else target.history_roots
            ),
            reviewed_exceptions,
            private_identity if private_identity.checker is not None else None,
            **({"check_repository_names": True} if private_submission else {}),
            **({"initial_private_save": True} if initial_private_save else {}),
            check_full_history_metadata=not private_submission and not base_ref,
            **(
                {"approved_private_commit_identity": approved_private_commit_identity}
                if approved_private_commit_identity is not None
                else {}
            ),
            **(
                {"check_raw_commit_payloads": True}
                if allow_expected_private_commit_identity
                else {}
            ),
        )
        credential_finding_count += len(history_credential_findings)
        credential_locations.extend(
            diagnostic_evidence_label(label) for label in history_credential_findings
        )
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": target.report_repo_label,
            "issues": ["git_history_inventory_failed"],
        }
    if private_submission:
        path_hits.extend(history_path_hits)
    elif base_ref:
        try:
            path_hits.extend(
                target_diff_personal_path_hits(
                    target.repo, base_ref, reviewed_exceptions
                )
            )
        except RuntimeError:
            return {
                "status": "tool_error",
                "repo": target.report_repo_label,
                "issues": ["git_target_diff_inventory_failed"],
            }
    else:
        path_hits.extend(history_path_hits)
    return (
        credential_finding_count,
        credential_locations,
        path_hits,
        identity_gate_report,
    )


def _collect_checks(
    target: _ScanTarget,
    *,
    base_ref: str | None,
    private_submission: bool,
    initial_private_save: str | None,
    release: bool,
    expected_identity: str | None,
    allowed_identity_set,
    initial_proof: dict | None,
    reviewed_exceptions: ReviewedSecretExceptions | None,
    missing: list[str],
    invalid_documents: list[str],
    private_identity: PrivateIdentityScan,
    credential_finding_count: int,
    credential_locations: list[str],
    path_hits: list[str],
    unscanned_path_files: list[str],
    private_worktree_enabled: bool,
    private_worktree_inventory_count: int,
    private_worktree_scanned_count: int,
    private_worktree_cache_reused: int,
    private_worktree_symlinks_excluded: int,
    private_worktree_sensitive_names: set[str],
    identity_gate_report: dict | None,
) -> dict:
    identity_lines = {line for line in target.identities.splitlines() if line}
    identity_mismatches = {
        line
        for line in identity_lines
        if _identity_line_mismatch(line, expected_identity, allowed_identity_set)
    }
    if allowed_identity_set is not None:
        identity_mismatches |= {
            line
            for line in identity_lines
            if not _identity_line_allowed(allowed_identity_set, line)
        }
    historical_identity_lines = {
        line for line in target.historical_identities.splitlines() if line
    }
    historical_identity_mismatches = {
        line
        for line in historical_identity_lines
        if _identity_line_mismatch(line, expected_identity, allowed_identity_set)
    }
    # 現在設定の名義はexpected_identity指定時だけ測る。identity未設定環境
    # (CI containerなど) をtool_errorにせず、判定はunknownでfail-closedに保つ
    effective_status = "not_evaluated"
    effective_mismatches: set[str] = set()
    if expected_identity:
        effective_probes = (
            run(target.repo, "git", "var", "GIT_AUTHOR_IDENT"),
            run(target.repo, "git", "var", "GIT_COMMITTER_IDENT"),
        )
        # probeは個別に評価する。片方が失敗しても、成功した側が示すmismatchは
        # 捨てない (失敗を理由にunknownへ丸めると既知の不一致が隠れる)
        probe_failed = False
        for code, value in effective_probes:
            if code:
                probe_failed = True
                continue
            identity = effective_identity(value)
            if identity != expected_identity:
                effective_mismatches.add(identity)
        if effective_mismatches:
            effective_status = "fail"
        elif probe_failed:
            effective_status = "unknown"
        else:
            effective_status = "pass"
    workflows = sorted(
        list((target.repo / ".github" / "workflows").glob("*.y*ml"))
        if (target.repo / ".github" / "workflows").is_dir()
        else []
    )
    invalid_workflows: list[str] = []
    for workflow in workflows:
        try:
            workflow_text = workflow.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            invalid_workflows.append(
                sanitized_evidence_label(
                    workflow.relative_to(target.repo).as_posix(),
                    private_name=(
                        private_submission
                        or initial_private_save is not None
                        or workflow.relative_to(target.repo).as_posix()
                        in private_worktree_sensitive_names
                        or private_identity.report.get("status") == "tool_error"
                    ),
                )
            )
            continue
        if not re.search(r"(?m)^jobs\s*:", workflow_text):
            invalid_workflows.append(
                sanitized_evidence_label(
                    workflow.relative_to(target.repo).as_posix(),
                    private_name=(
                        private_submission
                        or initial_private_save is not None
                        or workflow.relative_to(target.repo).as_posix()
                        in private_worktree_sensitive_names
                        or private_identity.report.get("status") == "tool_error"
                    ),
                )
            )
    dependency_files = sorted(
        name for name in DEPENDENCY_FILES if (target.repo / name).is_file()
    )
    private_identity_report = private_identity.result()
    private_identity_report.update(
        worktree_scope=(
            "excluded_unsent_worktree"
            if private_submission
            else "git_inventory_regular_files_excluding_ignored_and_links"
        ),
        worktree_name_scope="repo_relative_path_and_last_filename_suffix_stem",
        worktree_scan_status=(
            "not_scanned"
            if not private_worktree_enabled
            else (
                "complete"
                if private_worktree_scanned_count == private_worktree_inventory_count
                else "incomplete"
            )
        ),
        worktree_inventory_files=private_worktree_inventory_count,
        worktree_files_scanned=private_worktree_scanned_count,
        worktree_files_cache_reused=private_worktree_cache_reused,
        worktree_files_unscanned=private_worktree_inventory_count
        - private_worktree_scanned_count,
        worktree_symlinks_excluded=private_worktree_symlinks_excluded,
        git_object_scope=(
            sanitized_evidence_label(f"{base_ref}..HEAD")
            if base_ref
            else "all_reachable_refs"
        ),
    )
    if private_submission:
        private_identity_report.update(
            text_scope="added_text_and_changed_normalized_context_candidates",
            filename_scope="changed_committed_entries",
            baseline_text_scope="same_path_comparison_parent_only",
            text_delta_full_fallbacks=private_identity_report.get(
                "text_delta_full_fallbacks", 0
            ),
        )
    if reviewed_exceptions is not None and private_identity.checker is not None:
        private_identity_report["reviewed_path_exceptions"] = [
            {"id": key, "occurrence_count": count}
            for key, count in sorted(
                reviewed_exceptions.normalized_path_applied.items()
            )
        ]
    checks = {
        "clean_worktree": {"status": "pass" if not target.dirty else "fail"},
        "required_documents": {
            "status": (
                "not_evaluated"
                if base_ref
                else "pass" if not missing and not invalid_documents else "fail"
            ),
            "missing": missing,
            "invalid": invalid_documents,
        },
        "secret_scan": {
            "status": "pass" if credential_finding_count == 0 else "fail",
            "finding_count": credential_finding_count,
            "locations": sorted(set(credential_locations))[:50],
            "locations_truncated": max(0, len(set(credential_locations)) - 50),
            "reviewed_exceptions": (
                reviewed_exceptions.report() if reviewed_exceptions is not None else []
            ),
        },
        "personal_path_scan": {
            "status": (
                "fail" if path_hits else "unknown" if unscanned_path_files else "pass"
            ),
            "files": [
                diagnostic_evidence_label(label, private_identity)
                for label in path_hits
            ],
            "unscanned_files": unscanned_path_files,
            **(
                {"reviewed_exceptions": reviewed_exceptions.report(personal_paths=True)}
                if reviewed_exceptions is not None
                else {}
            ),
        },
        "public_attribution": public_attribution_result(
            target.repo,
            base_ref,
            allowed_identity_set,
            history_roots=target.history_roots,
        ),
        "private_identity_scan": private_identity_report,
        "identity_gate": (
            identity_gate_report
            if identity_gate_report is not None
            else _identity_gate_result(
                target.repo,
                base_ref,
                target.identity_base[1] if target.identity_base is not None else None,
                history_roots=target.history_roots,
                head_ref=target.head,
            )
        ),
        "commit_identity": {
            "status": (
                "fail"
                if identity_mismatches or effective_status == "fail"
                else "unknown" if effective_status == "unknown" else "pass"
            ),
            "policy": (
                "expected_identity"
                if expected_identity
                else (
                    "allowed_identities"
                    if allowed_identity_set is not None
                    else "not_configured"
                )
            ),
            "identity_count": len(identity_lines),
            "mismatch_count": len(identity_mismatches),
            "effective_identity": effective_status,
            "effective_mismatch_count": len(effective_mismatches),
            "scope": (
                "pending_commits"
                if target.identity_base
                else "target_diff" if base_ref else "repository_history"
            ),
            "base_ref": target.identity_base[0] if target.identity_base else None,
            "base_oid": target.identity_base[1] if target.identity_base else None,
        },
        "dependency_configuration": {
            "status": "pass" if dependency_files else "not_applicable",
            "files": dependency_files,
        },
        "dependency_vulnerability_audit": {
            "status": "unknown" if dependency_files else "not_applicable",
            "reason": "requires_ecosystem_specific_current_audit",
        },
        "ci_configuration": {
            "status": (
                "fail" if invalid_workflows else "pass" if workflows else "unknown"
            ),
            "workflow_count": len(workflows),
            "invalid_files": invalid_workflows,
        },
        "ci_runtime_result": {
            "status": "unknown" if workflows else "not_applicable",
            "reason": "requires_current_remote_ci_evidence",
        },
        "human_visual_review": {
            "status": "unknown",
            "reason": "explicit_human_review_required",
        },
        "origin": {
            "status": "pass" if target.remote else "unknown",
            "url": (
                "<private-origin>"
                if private_submission or initial_private_save
                else redact_remote(target.remote)
            ),
        },
    }
    if initial_private_save:
        for name in ("identity_gate", "public_attribution"):
            for finding in checks[name].get("findings", []):
                if finding.get("path"):
                    finding["path"] = "<redacted-path>"
        for name, key in (
            ("secret_scan", "reviewed_exceptions"),
            ("personal_path_scan", "reviewed_exceptions"),
            ("private_identity_scan", "reviewed_path_exceptions"),
        ):
            for exception in checks[name].get(key, []):
                exception["id"] = "<reviewed-exception>"
    if target.identity_base:
        checks["historical_identity_audit"] = {
            "status": "observed",
            "identity_count": len(historical_identity_lines),
            "mismatch_count": len(historical_identity_mismatches),
            "scope": "all_refs",
        }
    checks["repository_consistency"] = run_consistency_gate(
        target.repo,
        target.consistency_base_ref or base_ref,
        **({"initial_private_save": True} if initial_proof else {}),
    )
    if release:
        release_baseline = (
            (target.resolved_consistency_base_ref, target.consistency_base_oid)
            if target.resolved_consistency_base_ref
            and target.resolved_consistency_base_ref.startswith("refs/tags/")
            and target.consistency_base_oid
            else None
        )
        checks["readme_release_freshness"] = check_release_readme_freshness(
            target.repo, baseline=release_baseline
        )
        if (target.repo / "README.md").is_file():
            readme_report = run_readme_release_gate(target.repo)
            checks["readme_release_design"] = {
                "status": "pass" if readme_report["status"] == "pass" else "fail",
                "design_status": readme_report["status"],
                "release_gate": readme_report["release_gate"],
                "metrics": readme_report["metrics"],
                "findings": readme_report["findings"],
                "recommended_capabilities": readme_report["recommended_capabilities"],
                "human_visual_review_required": True,
            }
        else:
            checks["readme_release_design"] = {
                "status": "fail",
                "design_status": "blocked",
                "release_gate": "blocked_readme_missing",
                "findings": [
                    {
                        "code": "readme_missing",
                        "severity": "error",
                        "message": "README.mdがありません。",
                    }
                ],
                "recommended_capabilities": ["Template Creator"],
                "human_visual_review_required": True,
            }
    return checks


def _aggregate_report(
    target: _ScanTarget,
    checks: dict,
    *,
    base_ref: str | None,
    private_submission: bool,
    release: bool,
    require_private_identity: bool,
    allowed_identity_set,
    private_identity: PrivateIdentityScan,
    private_submission_proof: dict | None,
) -> dict:
    gate = _report.gating(
        identity_gate=checks["identity_gate"],
        allowed_identities_configured=allowed_identity_set is not None,
        require_private_identity=require_private_identity,
        private_identity_report=private_identity.report,
        target_diff=bool(base_ref),
        release=release,
    )
    if gate.identity_gate_override is not None:
        checks["identity_gate"] = gate.identity_gate_override
    report = {
        "status": _report.aggregate_status(checks, gate.names),
        "publication_decision": "blocked_human_review_required",
        "repo": target.report_repo_label,
        "head": target.head,
        "scan_scope": (
            {
                "mode": (
                    "private_committed_submission"
                    if private_submission
                    else "target_diff"
                ),
                **(
                    {
                        "private_submission": private_submission_proof,
                        "head_oid": target.head,
                    }
                    if private_submission
                    else {}
                ),
                "base_ref": sanitized_evidence_label(base_ref),
                "resolved_base_ref": sanitized_evidence_label(target.resolved_base_ref),
                "base_oid": target.base_oid,
            }
            if base_ref
            else {"mode": "repository"}
        ),
        "consistency_scope": (
            {
                "mode": "target_diff",
                "base_ref": sanitized_evidence_label(target.consistency_base_ref),
                "resolved_base_ref": sanitized_evidence_label(
                    target.resolved_consistency_base_ref
                ),
                "base_oid": target.consistency_base_oid,
            }
            if target.consistency_base_ref
            else {"mode": "same_as_scan"}
        ),
        "checks": checks,
    }
    return report


def _recheck_refs(
    target: _ScanTarget,
    report: dict,
    *,
    initial_private_save: str | None,
    initial_proof: dict | None,
    push_ref: str | None,
    private_submission: bool,
) -> dict | None:
    if initial_proof:
        try:
            if (
                verify_initial_private_save(target.repo, initial_private_save)
                != initial_proof
            ):
                raise RuntimeError("initial_private_save_destination_changed")
        except RuntimeError as exc:
            return {"status": "tool_error", "issues": [str(exc)]}
        try:
            current_roots = fixed_history_revisions(target.repo, ("--all",))
        except RuntimeError:
            current_roots = ()
        if (
            initial_save_branch(target.repo) != push_ref
            or current_roots != target.history_roots
            or run(target.repo, "git", "rev-parse", "HEAD") != (0, target.head)
        ):
            return {
                "status": "tool_error",
                "issues": ["initial_private_save_local_refs_changed"],
            }
        report["scan_scope"].update(
            mode="initial_private_save",
            initial_private_save=initial_proof,
            destination_ref_sha256=hashlib.sha256(push_ref.encode("utf-8")).hexdigest(),
            head_oid=target.head,
        )
        report["consistency_scope"] = {"mode": "initial_private_save"}
    if private_submission and (
        run(target.repo, "git", "rev-parse", "HEAD") != (0, target.head)
        or run(target.repo, "git", "rev-parse", target.resolved_base_ref)
        != (0, target.base_oid)
    ):
        return {
            "status": "tool_error",
            "issues": ["private_submission_local_refs_changed"],
        }
    return None


def enrich_report(report: dict, options: ScanOptions) -> dict:
    label_checker = None
    if options.repo is not None:
        expected = report.get("checks", {}).get("private_identity_scan", {})
        candidate = PrivateIdentityScan(options.repo.resolve(), False)
        if (
            expected.get("status") != "tool_error"
            and expected.get("provider_sha256")
            and expected.get("policy_sha256")
            and all(
                expected.get(key) == candidate.report.get(key)
                for key in (
                    "provider_sha256",
                    "policy_sha256",
                    "normalized_path_provider_sha256",
                )
            )
        ):
            label_checker = candidate
    return _report.enrich_report(
        report,
        options,
        label_checker=label_checker,
        sensitive=_evidence_label_is_sensitive,
    )


def build_intent_dialogue(options: ScanOptions) -> dict:
    """AI が外部操作直前に使う質問パケットを作る。"""
    if options.initial_private_save is not None and (
        options.intent != "push"
        or not valid_initial_private_scope(
            options.initial_private_save,
            options.base_ref,
            options.consistency_base_ref,
            options.identity_base_ref,
            options.private_submission,
            options.release,
        )
    ):
        raise ValueError("invalid_initial_private_save_scope")
    if options.allow_expected_private_commit_identity and (
        not options.private_submission
        or not options.expected_identity
        or not options.base_ref
        or options.release
        or options.intent not in {"push", "open_pr", "merge"}
    ):
        raise ValueError("invalid_private_commit_identity_scope")
    if options.media_inspection == "metadata" and not options.private_submission:
        raise ValueError("invalid_media_inspection_scope")
    if options.private_submission and (
        options.intent not in {"push", "open_pr", "merge"}
        or not options.base_ref
        or options.release
    ):
        raise ValueError("invalid_private_submission_scope")
    gate = load_dialogue_gate()
    prefs_mod = load_preferences_module()
    scan_report: dict | None = None
    needs_scan = gate.intent_needs_scan(options.intent) or (
        options.intent == "create_repo" and options.repo is not None
    )
    if needs_scan and options.repo is not None:
        release = options.release or gate.intent_uses_release_gate(options.intent)
        scan_kwargs = {
            "expected_identity": options.expected_identity,
            "release": release,
        }
        if options.initial_private_save is not None:
            scan_kwargs["initial_private_save"] = options.initial_private_save
        if options.private_submission:
            scan_kwargs["private_submission"] = True
        if options.allow_expected_private_commit_identity:
            scan_kwargs["allow_expected_private_commit_identity"] = True
        if options.media_inspection != "strict":
            scan_kwargs["media_inspection"] = options.media_inspection
        if options.base_ref:
            scan_kwargs["base_ref"] = options.base_ref
        if options.consistency_base_ref:
            scan_kwargs["consistency_base_ref"] = options.consistency_base_ref
        if options.identity_base_ref:
            scan_kwargs["identity_base_ref"] = options.identity_base_ref
        if options.reviewed_secret_exceptions is not None:
            scan_kwargs["reviewed_secret_exceptions"] = (
                options.reviewed_secret_exceptions
            )
        scan_kwargs["require_private_identity"] = True
        if options.allowed_identities is not None:
            scan_kwargs["allowed_identities"] = options.allowed_identities
        scan_report = scan(options.repo, **scan_kwargs)
        if options.intent == "push" and scan_report.get("scan_scope", {}).get(
            "mode"
        ) in {"repository", "target_diff"}:
            scope = scan_report["scan_scope"]
            target_diff = scope["mode"] == "target_diff"
            resolved_base = scope.get("resolved_base_ref") if target_diff else None
            try:
                if target_diff and not resolved_base:
                    raise RuntimeError("push_destination_base_unverified")
                proof = live_push_destination(options.repo, base_ref=resolved_base)
            except RuntimeError:
                proof = None
            if (
                proof
                and not proof["empty"]
                and (proof["branch_count"] or proof["tag_count"])
                and (
                    not target_diff
                    or (
                        proof.get("live_base_oid") == scope.get("base_oid")
                        and run(options.repo, "git", "rev-parse", resolved_base)
                        == (0, scope["base_oid"])
                    )
                )
            ):
                scope["existing_push_destination"] = proof
        report_options = copy.copy(options)
        report_options.release = release
        scan_report = enrich_report(scan_report, report_options)
    elif needs_scan and options.repo is None:
        scan_report = None
    preferences = prefs_mod.load_preferences(options.repo)
    github_baseline = prefs_mod.github_baseline_status(
        prefs_mod.default_github_baseline_path()
    )
    github_settings_review: dict | None = None
    if options.intent == "configure_settings" and options.repo is not None:
        # origin 解決は repo path から git に委ねるため、root でない path を渡すと
        # 囲っている repository へ広がる。解決の前に閉じる側で止める。
        root_error = repository_root_error(options.repo)
        if root_error is not None:
            scan_report = root_error
    if (
        options.intent == "configure_settings"
        and options.repo is not None
        and scan_report is None
    ):
        settings_mod = load_github_settings_module()
        repository = settings_mod.repository_from_repo(options.repo)
        if repository is None:
            github_settings_review = {
                "schema_version": settings_mod.SCHEMA_VERSION,
                "status": "needs_human_input",
                "repository": None,
                "profile": options.github_settings_profile,
                "settings": [],
                "unknowns": [{"reason": "github_origin_owner_name_unavailable"}],
                "external_actions_performed": False,
            }
        else:
            github_settings_review = settings_mod.review_repository(
                repository, options.github_settings_profile
            )
    boundaries = boundary_sections()
    return gate.build_dialogue(
        intent=options.intent,
        scan=scan_report,
        audience=options.audience,
        guarantees=boundaries["guarantees"],
        non_guarantees=boundaries["non_guarantees"],
        preferences=preferences,
        github_baseline=github_baseline,
        preferences_module=prefs_mod,
        github_settings_review=github_settings_review,
    )


def collect_interactive_options(
    *,
    initial_repo: Path | None = None,
    initial_release: bool = False,
    initial_identity: str | None = None,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    return _cli.collect_interactive_options(
        format_boundary_text=format_boundary_text,
        initial_repo=initial_repo,
        initial_release=initial_release,
        initial_identity=initial_identity,
        input_fn=input_fn,
        output_fn=output_fn,
    )


def build_parser() -> argparse.ArgumentParser:
    return _cli.build_parser(load_dialogue_gate())


def resolve_options(
    args: argparse.Namespace,
    *,
    stdin_is_tty: bool,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    return _cli.resolve_options(
        args,
        load_gate=load_dialogue_gate,
        format_boundary_text=format_boundary_text,
        stdin_is_tty=stdin_is_tty,
        input_fn=input_fn,
        output_fn=output_fn,
    )


def main(
    argv: list[str] | None = None,
    *,
    stdin_is_tty: bool | None = None,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    console: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

    out = stdout if stdout is not None else sys.stdout
    err = console if console is not None else sys.stderr
    tty = sys.stdin.isatty() if stdin_is_tty is None else stdin_is_tty

    parser = build_parser()
    args = parser.parse_args(argv)

    # dismiss 記録専用パス (対話の回答を機械反映)
    if args.record_dismissal:
        if args.repo is None or not args.dismissal_mode:
            _emit(
                err,
                "error: --record-dismissal requires --repo and --dismissal-mode",
            )
            return 2
        prefs_mod = load_preferences_module()
        try:
            prefs = prefs_mod.load_preferences(args.repo)
            prefs = prefs_mod.record_dismissal(
                prefs,
                args.record_dismissal,
                mode=args.dismissal_mode,
                reason=args.dismissal_reason or "",
            )
            path = prefs_mod.save_preferences(args.repo, prefs)
        except Exception as exc:
            _emit(
                err,
                f"error: failed to record dismissal ({type(exc).__name__})",
            )
            return 2
        _emit(
            out,
            json.dumps(
                {
                    "schema": prefs_mod.PREFERENCES_SCHEMA,
                    "status": "recorded",
                    "path": path.name,
                    "proposal_id": args.record_dismissal,
                    "dismissal": prefs["dismissals"][args.record_dismissal],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return 0

    try:
        options = resolve_options(
            args,
            stdin_is_tty=tty,
            input_fn=input_fn,
            output_fn=lambda text: _emit(err, text),
        )
    except EOFError:
        _emit(err, "error: interactive input ended before options were complete")
        return 2
    except SystemExit as exc:
        # argparse / 対話キャンセル。message付きは人間向けに stderr へ
        if exc.code not in (0, None) and not isinstance(exc.code, int):
            _emit(err, str(exc.code))
            return 2
        if isinstance(exc.code, int):
            return exc.code
        return 0

    # AI 操作直前ゲート: 不足設定を質問パケットとして返す
    if options.intent:
        try:
            dialogue = build_intent_dialogue(options)
        except Exception as exc:
            dialogue = {
                "schema": "repo-preflight.dialogue/v3",
                "intent": options.intent,
                "status": "blocked",
                "publication_decision": "blocked_human_review_required",
                "issues": [unexpected_exception_issue(exc)],
                "proposals": [],
                "confirmations": [],
            }
            _emit(out, json.dumps(dialogue, ensure_ascii=False, indent=2))
            return 2
        gate = load_dialogue_gate()
        if args.human or options.interactive:
            _emit(err, gate.format_dialogue_for_agent(dialogue))
        _emit(out, json.dumps(dialogue, ensure_ascii=False, indent=2))
        return dialogue_exit_code(dialogue)

    try:
        scan_kwargs = {
            "expected_identity": options.expected_identity,
            "release": options.release,
        }
        if options.private_submission:
            scan_kwargs["private_submission"] = True
        if options.allow_expected_private_commit_identity:
            scan_kwargs["allow_expected_private_commit_identity"] = True
        if options.media_inspection != "strict":
            scan_kwargs["media_inspection"] = options.media_inspection
        if options.base_ref:
            scan_kwargs["base_ref"] = options.base_ref
        if options.consistency_base_ref:
            scan_kwargs["consistency_base_ref"] = options.consistency_base_ref
        if options.identity_base_ref:
            scan_kwargs["identity_base_ref"] = options.identity_base_ref
        if options.reviewed_secret_exceptions is not None:
            scan_kwargs["reviewed_secret_exceptions"] = (
                options.reviewed_secret_exceptions
            )
        scan_kwargs["require_private_identity"] = args.require_private_identity
        if options.allowed_identities is not None:
            scan_kwargs["allowed_identities"] = options.allowed_identities
        report = scan(options.repo, **scan_kwargs)
    except Exception as exc:  # 予期しない例外もexit 2の検査失敗として扱う
        # message と path は path/secret を含み得る。型名に加え、
        # ModuleNotFoundError の安全な name と frame basename だけを残す。
        report = {
            "status": "tool_error",
            "issues": [unexpected_exception_issue(exc)],
        }
    report = enrich_report(report, options)

    human_wanted = options.interactive or bool(args.human)
    if human_wanted:
        _emit(err, format_human_report(report))
        if options.interactive and not options.show_json:
            return report_exit_code(report)
        if options.interactive:
            _emit(err, "")
            _emit(err, "## JSON")

    # 機械可読の正本は常に stdout の JSON (対話で JSON 非表示を選んだ場合のみ省略)
    if not options.interactive or options.show_json:
        _emit(out, json.dumps(report, ensure_ascii=False, indent=2))
    return report_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
