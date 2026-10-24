from __future__ import annotations

import argparse
import types
import stat
import io
import copy
import contextlib
import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
import difflib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import traceback
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, TextIO
from urllib.parse import unquote, urlsplit, urlunsplit

SCHEMA = "repo-preflight.scan/v3"

# CLIが担当する範囲の境界。対話・非対話のどちらでも同じ文言を出す。
GUARANTEES = (
    "ローカルGitの現在treeと履歴を読み取り専用で検査する",
    "選択した検査scopeでsecret候補・個人path・作者名義を機械判定し、repo全体modeでは必須文書とCI設定も確認する",
    "repo全体modeでは作業treeの Live2D Cubism .cmo3 を展開して個人pathを判定し、展開できないfileは unknown にする",
    "status は CLI が担当する自動検査の結果だけを表す (pass / blocked / tool_error)",
    "publication_decision は常に人間レビュー要求とし、自動で公開承認しない",
    "検出結果に秘密値そのものを出力しない",
    "推奨質問の dismiss/snooze を採用先 .repo-preflight.json に記録し、次回から抑止する",
    "同梱 GitHub 設定ガイドの last_reviewed 期限切れを検知し、更新確認の質問を出す",
    "configure_settings intent ではGitHub APIをread-only取得し、profile差分と個別previewを返す",
    "宣言設定がある repo では Markdown・README契約・影響マップ・SSOT生成物の整合性を検査する",
)

NON_GUARANTEES = (
    "秘密情報が存在しないことの完全保証 (独自形式・符号化・大容量blob・バイナリ内、英数字・_・- に直結した sk- 形式の鍵は見逃し得る)",
    "個人pathが存在しないことの完全保証 (.cmo3 の展開はrepo全体modeの作業treeだけ。履歴・--base-ref差分内の .cmo3、他の独自形式・圧縮・バイナリ内は見逃し得る)",
    "依存ライブラリの既知脆弱性",
    "第三者素材を公開する権利・ライセンス判断",
    "通常scanにおけるGitHub remote設定全体の現在状態 (configure_settings intentで別途取得。個人path例外のv2はprivate・v3はpublic合成fixture限定をlive確認)",
    "GitHub製品変更・公式推奨のリアルタイム自動追従 (鮮度検知と更新確認までは行う)",
    "CIが remote で実際に成功したか",
    "障害通知先・復旧手順が実運用で機能すること",
    "README・個人情報・公開範囲の目視確認",
    "repo 固有の影響マップが宣言していない docs 更新要否の推測",
    "公開・push・merge・visibility変更・投稿の実行",
    "dismiss した推奨項目が将来も安全であること (期限切れ snooze や再発火があり得る)",
)

AUDIENCE_CHOICES = (
    ("public", "Web全体 (public化)"),
    ("team", "team / organization 共有"),
    ("client", "客先納品"),
    ("collaborator", "外部協力者 (期限付き)"),
    ("local", "ローカル確認のみ (見せる相手はまだ決めない)"),
)

MODE_CHOICES = (
    ("standard", "標準 preflight (文書・secret・path・identity・CI設定)"),
    ("release", "release準備 (標準 + README情報設計ゲート)"),
)

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
SECRET_PATTERNS = (
    # "sk-" は英単語の語尾 (task- / desk- / risk- / musk- …) にそのまま現れ、
    # 後続の kebab-case も本体の文字種に収まるため、前側の区切りを要求する。
    # 直前が英数字・"_"・"-" なら一致させない。escape された空白 (\n \r \t) の
    # 直後は、literal 上は英字が前にあっても区切りとして扱う。
    # 他の規則の接頭辞は語の途中に現れにくいので、この制限を持たせない。
    re.compile(r"(?:(?<![A-Za-z0-9_-])|(?<=\\[nrt]))sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY"),
    # 開発道具のセッション行・セッションURL。全履歴の数を比べるので型は広げない（新しく足すものは identity_gate.SESSION_TRACE_RES がより広く見る）。
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


def _zip_exact(first, second):
    """zip(strict=True) と同じ（長さが違えば ValueError）。Mac 標準の Python 3.9 でも動く。"""
    first, second = list(first), list(second)
    if len(first) != len(second):
        raise ValueError("_zip_exact: lengths differ")
    return zip(first, second)


def secret_matches(data: bytes) -> Counter:
    """同じ検出のraw/URL復号/文字encoding間の重複は最大件数にまとめる。"""
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


class ReviewedSecretExceptions:
    """明示指定された、内容と検出を完全束縛するレビュー済み例外。"""

    def __init__(self, policy_path: Path, remote: str):
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result

        try:
            raw_policy = policy_path.read_bytes()
            if len(raw_policy) > 1_000_000:
                raise ValueError("size")
            policy = json.loads(
                raw_policy.decode("utf-8"), object_pairs_hook=unique_object
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
        if self.has_path_entries:
            # REST originをlive取得。v2はprivate、v3はpublicの合成fixture限定。
            # local設定、保存済みsnapshotやunknownは根拠にしない。
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
        self, path: str, data: bytes, matches: Counter, applied: Counter
    ) -> bool:
        # Windows の autocrlf で作業ツリーだけ CRLF になっても、LF で登録した digest と照合する。
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
            personal_path_matches(data if scanned_data is None else scanned_data),
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
        # 正規化後もrepo/path/raw content hash/検出値hash/件数/期限の全束縛を維持する。
        matches = (
            normalized_personal_path_matches(data, normalizer)
            if normalized
            else personal_path_matches(data)
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
                for rule, pattern in _zip_exact(PATH_RULE_IDS, PATH_PATTERNS)
                for match in pattern.finditer(candidate)
            )
    matches |= Counter(
        (
            rule,
            hashlib.sha256(
                match.groupdict().get("personal_path") or match.group()
            ).hexdigest(),
        )
        for rule, pattern in _zip_exact(PATH_RULE_IDS, PATH_PATTERNS)
        for match in re.finditer(pattern.pattern.encode("ascii"), data)
    )
    return matches


# Live2D Cubism の編集file (.cmo3) は XOR した zip entry に XML を deflate で
# 入れている。生byteには個人pathが現れないので、展開した XML を判定する。
# 形式は実物からの実測で、公開仕様ではない。
CMO3_SUFFIX = ".cmo3"
CMO3_ENTRY_NAME = b"contents"
CMO3_MAX_XML_BYTES = 64 * 1024 * 1024
_CMO3_XOR_TABLE = bytes(value ^ 0xE3 for value in range(256))
_ZIP_LOCAL_HEADER = re.compile(re.escape(b"PK\x03\x04"))

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
        if any(
            pattern.search(candidate)
            for candidate in (text, decoded_text)
            for pattern in patterns
        ):
            return True
    return _bytes_pattern_hit(patterns, data)


def sanitized_evidence_label(label: str, *, private_name: bool = False) -> str:
    data = label.encode("utf-8", errors="surrogatepass")
    if private_name or text_has(SECRET_PATTERNS + PATH_PATTERNS, data):
        return "<redacted-path>"
    return label


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


def _identity_gate_result(repo, base_ref, identity_base_ref=None):
    """名義の許可リスト・共同作者行・コミット本文を検査し必須判定へ返す。
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

    rng = f"{base_ref}..HEAD" if base_ref else "--all"
    try:
        issues = _ig.check_range(
            pol,
            str(repo),
            rng,
            identity_rng=f"{identity_base_ref}..HEAD" if identity_base_ref else None,
            git_fn=_identity_git_raw,
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ):
        return {"status": "tool_error", "reason": "identity_history_unavailable"}
    return {"status": "fail" if issues else "pass", "issue_count": len(issues)}


def public_attribution_result(repo, base_ref, allowed_identities):
    if allowed_identities is None:
        return {"status": "not_evaluated", "reason": "public_allowlist_not_configured"}
    try:
        identity_gate = load_identity_gate()
        issues = identity_gate.check_public_range(
            allowed_identities,
            str(repo),
            f"{base_ref}..HEAD" if base_ref else "--all",
            git_fn=_identity_git_raw,
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
    return {"status": "fail" if issues else "pass", "issue_count": len(issues)}


def load_allowed_identities(path: Path):
    """許可名義の一覧を identity_gate の同じ読み手で読む（判定の中身を1つにする）。"""
    try:
        return load_identity_gate().parse_public_policy(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, ImportError) as exc:
        raise RuntimeError("allowed_identities_invalid") from exc


def _identity_line_allowed(policy, line: str) -> bool:
    """名義の1行を役割ごとに完全一致で見る（identity_gate.check_public_range と同じ規則）。

    `作者|記録者` の行は、作者を「作者」、記録者を「コミッター」として判定する。
    `|` の無い行は Co-authored-by の署名行なので「作者」として判定する。
    committer_only の名義（GitHub の Web 操作の記録者など）は記録者の位置でだけ通る。
    """
    if "|" not in line:
        return policy.allows(line, "作者")
    parts = line.split("|")
    return policy.allows(parts[0], "作者") and policy.allows(parts[-1], "コミッター")


def effective_identity(value: str) -> str:
    return re.sub(r"\s+\d+\s+[+-]\d{4}$", "", value).strip()


def normalized_personal_path_matches(data: bytes, normalizer=None) -> Counter:
    """公開正本の静的viewsを既存path規則へ渡し、追加検出だけを件数へ加える。"""
    if normalizer is None:
        source = Path(__file__).resolve().with_name("identity_normalization.py")
        spec = importlib.util.spec_from_file_location(
            "_canonical_path_normalization", source
        )
        normalizer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(normalizer)
    raw = personal_path_matches(data)
    matches = raw.copy()
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        extra = Counter()
        for value, original in normalizer.static_string_views(text):
            extra += personal_path_matches(
                value.encode("utf-8")
            ) - personal_path_matches(original.encode("utf-8"))
        matches |= raw + extra
    return matches


class PrivateIdentityScan:
    """明示された既存private providerを同一bytesから読み、値なしの実検査証拠を返す。"""

    def __init__(self, repo: Path, required: bool):
        self.findings = 0
        self.objects = 0
        self.skipped = 0
        self.report = {"status": "not_configured", "required": required}
        self.checker = None
        self.path_normalizer = None
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
            self.report.update(
                status="pass",
                provider_sha256=hashlib.sha256(provider_bytes).hexdigest(),
                policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
                provider_kind="bundled_canonical" if bundled else "explicit_external",
                normalized_path_provider_sha256=self.path_normalizer_sha256,
            )
        except (Exception, SystemExit):
            self.report["reason"] = "private_identity_input_unverified"

    def check(
        self,
        data: bytes,
        *,
        literal_only: bool = False,
        repository_name: bool = False,
        reviewed_raw_paths: bool = False,
        defer_unverified_jpeg: bool = False,
    ):
        # 例外の助けなしに合格した本文だけ、使った正規化器のhashを残す (控えの材料)。
        self.last_clean_pass_digests = None
        if self.checker is None:
            return
        seen_normalizer_digests: set[str] = set()
        texts = {data.decode("utf-8", errors="replace")}
        try:
            texts.add(data.decode("utf-16"))
        except UnicodeDecodeError:
            pass
        found = False
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                if data.startswith(b"8BPS") and callable(
                    getattr(self, "psd_checker", None)
                ):
                    results = [self.psd_checker(data, self.patterns)]
                elif data.startswith((b"GIF87a", b"GIF89a")) and callable(
                    getattr(self, "gif_checker", None)
                ):
                    results = [self.gif_checker(data, self.patterns)]
                elif data.startswith(b"\x89PNG") and callable(
                    getattr(self, "png_checker", None)
                ):
                    results = [self.png_checker(data, self.patterns)]
                elif data.startswith(b"\xff\xd8") and callable(
                    getattr(self, "jpeg_checker", None)
                ):
                    results = [self.jpeg_checker(data, self.patterns)]
                else:
                    results = [
                        (
                            self.repository_name_checker
                            if repository_name
                            and callable(getattr(self, "repository_name_checker", None))
                            else self.checker
                        )(candidate, self.patterns)
                        for text in texts
                        for candidate in {text, unquote(text)}
                    ]
                for result in results:
                    if (
                        not isinstance(result, dict)
                        or result.get("status") not in {"pass", "fail"}
                        or type(result.get("literal_match_count")) is not int
                        or result["literal_match_count"] < 0
                        or type(result.get("local_path_match")) is not bool
                    ):
                        raise ValueError("private_identity_provider_result_invalid")
                    if (result["status"] == "pass") != (
                        result["literal_match_count"] == 0
                        and not result["local_path_match"]
                    ):
                        raise ValueError(
                            "private_identity_provider_result_inconsistent"
                        )
                    reconstructed = result.get(
                        "reconstructed_path_match", result["local_path_match"]
                    )
                    if type(reconstructed) is not bool or (
                        reconstructed and not result["local_path_match"]
                    ):
                        raise ValueError(
                            "private_identity_provider_result_inconsistent"
                        )
                    metadata_match = result.get("metadata_path_match", False)
                    if type(metadata_match) is not bool or (
                        metadata_match and not result["local_path_match"]
                    ):
                        raise ValueError(
                            "private_identity_provider_result_inconsistent"
                        )
                    normalizer_digest = result.get("normalizer_sha256")
                    if normalizer_digest is not None:
                        if not isinstance(normalizer_digest, str) or not re.fullmatch(
                            r"[0-9a-f]{64}", normalizer_digest
                        ):
                            raise ValueError("private_identity_normalizer_unverified")
                        if (
                            self.report.get("normalizer_sha256", normalizer_digest)
                            != normalizer_digest
                        ):
                            raise ValueError("private_identity_normalizer_changed")
                        self.report["normalizer_sha256"] = normalizer_digest
                        seen_normalizer_digests.add(normalizer_digest)
                    # literal_onlyは旧caller互換だけ。未承認pathの抑止には使わない。
                    same_normalizer = (
                        self.report["provider_kind"] == "bundled_canonical"
                        or normalizer_digest == self.path_normalizer_sha256
                    )
                    found |= bool(
                        result["literal_match_count"]
                        or (
                            result["local_path_match"]
                            and (
                                not reviewed_raw_paths
                                or not same_normalizer
                                or metadata_match
                            )
                        )
                    )
        except (Exception, SystemExit) as exc:
            code = str(exc)
            # 送信に含まれない既存JPEGの構造不正だけは、そのfileを未展開として続ける。
            # 容量上限は本文照合より前に出るため、mainと同じtool_errorのままにする。
            if defer_unverified_jpeg and code == "private_identity_jpeg_unverified":
                self.report["not_expanded_count"] = (
                    int(self.report.get("not_expanded_count", 0)) + 1
                )
                return
            safe_codes = {
                "private_identity_jpeg_unverified",
                "private_identity_jpeg_image_limit",
                "private_identity_jpeg_text_limit",
                "private_identity_jpeg_decoder_unavailable",
                "private_identity_gif_unverified",
                "private_identity_gif_size_limit",
                "private_identity_gif_decoder_unavailable",
                "private_identity_psd_unverified",
                "private_identity_psd_decoder_unavailable",
                "private_identity_psd_size_limit",
                "private_identity_psd_text_limit",
                "private_identity_png_unverified",
                "private_identity_png_text_limit",
                "private_identity_png_image_limit",
                "private_identity_png_animation_unverified",
                "private_identity_normalization_incomplete",
            }
            reason = (
                str(exc)
                if str(exc) in safe_codes
                else "private_identity_execution_unverified"
            )
            self.report.update(status="tool_error", reason=reason)
            self.checker = None
            return
        self.objects += 1
        self.findings += int(found)
        if not found and not reviewed_raw_paths:
            self.last_clean_pass_digests = tuple(sorted(seen_normalizer_digests))

    def reuse_clean_pass(self, normalizer_digests: tuple[str, ...]) -> bool:
        """控えた合格を、本文検査と同じ数え方・正規化器の一致確認で反映する。"""
        if self.checker is None or self.report.get("status") == "tool_error":
            return False
        for digest in normalizer_digests:
            if self.report.get("normalizer_sha256", digest) != digest:
                # 食い違いは控えを使わず本文検査へ回し、通常の失敗経路に任せる。
                return False
        for digest in normalizer_digests:
            self.report["normalizer_sha256"] = digest
        self.objects += 1
        return True

    def label_status(self, label: str) -> str:
        """同じ検査器で報告名だけを確認し、file/objectの検出件数へ混ぜない。"""
        if self.checker is None or self.report.get("status") == "tool_error":
            return "unknown"
        probe = copy.copy(self)
        probe.report = dict(self.report)
        probe.objects = probe.findings = probe.skipped = 0
        probe.check(label.encode("utf-8", errors="surrogatepass"))
        if probe.checker is None or probe.report.get("status") == "tool_error":
            return "unknown"
        return "private" if probe.findings else "public"

    def result(self):
        report = {
            **self.report,
            "objects_scanned": self.objects,
            "finding_count": self.findings,
            "oversize_objects_unscanned": self.skipped,
            "scope": "worktree_and_reachable_git_objects",
        }
        if self.report["status"] == "tool_error":
            return report
        if self.findings:
            report["status"] = "fail"
        elif self.skipped and self.checker is not None:
            report["status"] = "unknown"
        if self.report.get("not_expanded_count"):
            report["not_expanded_count"] = self.report["not_expanded_count"]
        return report


IDENTITY_SCAN_CACHE_VERSION = "identity-scan-pass-cache-v1"
IDENTITY_SCAN_CACHE_ENV = "REPO_PREFLIGHT_IDENTITY_SCAN_CACHE"
_IDENTITY_SCAN_CACHE_DISABLED = {"0", "off", "false", "no"}
_IDENTITY_SCAN_CACHE_MAX_ENTRIES = 500_000
_IDENTITY_SCAN_CACHE_MAX_BYTES = 128 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _scanner_source_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _optional_distribution_version(name: str) -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return "absent"


class IdentityScanCache:
    """本文の個人識別子検査で合格した内容hashだけを控え、同じ鍵の再検査を省く。

    鍵は検査器・方針・正規化器・reviewed例外・remote・実行環境のhash。1つでも違えば
    別の控えになり、控えが壊れている・所有者や権限が違う・repo内にあるときは使わない。
    控えるのは例外の助けなしの合格だけで、本文・path・検出値は書かない。
    """

    def __init__(self, path: Path, key: str, entries: dict[str, tuple[str, ...]]):
        self.path = path
        self.key = key
        self.entries = entries
        self.dirty = False

    @classmethod
    def open(
        cls,
        repo: Path,
        scan: "PrivateIdentityScan",
        reviewed_secret_exceptions: str | Path | None,
        remote: str,
    ) -> "IdentityScanCache | None":
        if (
            os.environ.get(IDENTITY_SCAN_CACHE_ENV, "").strip().lower()
            in _IDENTITY_SCAN_CACHE_DISABLED
        ):
            return None
        # 同梱の正規化器だけを対象にする。外部providerはimport先の変化をhashで縛れない。
        if (
            os.name != "posix"
            or scan.checker is None
            or scan.report.get("status") != "pass"
            or scan.report.get("provider_kind") != "bundled_canonical"
        ):
            return None
        try:
            reviewed_digest = None
            if reviewed_secret_exceptions is not None:
                reviewed_digest = hashlib.sha256(
                    Path(reviewed_secret_exceptions).read_bytes()
                ).hexdigest()
            material = {
                "version": IDENTITY_SCAN_CACHE_VERSION,
                "scanner_sha256": _scanner_source_sha256(),
                "provider_sha256": scan.report["provider_sha256"],
                "policy_sha256": scan.report["policy_sha256"],
                "provider_kind": scan.report["provider_kind"],
                "normalized_path_provider_sha256": scan.path_normalizer_sha256,
                "gif_checker": callable(getattr(scan, "gif_checker", None)),
                "gif_decoder_version": scan.report.get("gif_decoder_version"),
                "gif_decoder_settings": scan.report.get("gif_decoder_settings"),
                "psd_checker": callable(getattr(scan, "psd_checker", None)),
                "psd_decoder_version": scan.report.get("psd_decoder_version"),
                "png_checker": callable(getattr(scan, "png_checker", None)),
                "jpeg_checker": callable(getattr(scan, "jpeg_checker", None)),
                "reviewed_exceptions_sha256": reviewed_digest,
                "remote_sha256": hashlib.sha256(remote.encode("utf-8")).hexdigest(),
                "python": sys.version,
                "simplejpeg": _optional_distribution_version("simplejpeg"),
            }
            key = hashlib.sha256(
                json.dumps(material, sort_keys=True).encode("utf-8")
            ).hexdigest()
            base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
            base_path = Path(base)
            if not base_path.is_absolute():
                return None
            directory = base_path / "repo-preflight" / "identity-scan"
            repo_root = repo.resolve()
            if directory.resolve().is_relative_to(repo_root) or (
                base_path.resolve().is_relative_to(repo_root)
            ):
                return None
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                return None
            if stat.S_IMODE(info.st_mode) & 0o077:
                # 権限が緩いフォルダは、自動で直さず控えを一切使わない。その場で直して
                # 中身を消す処理は、入れ子や競合で古い控えを残す抜け道になる (fail-closed)。
                return None
            path = directory / f"{key}.json"
            return cls(path, key, cls._load(path, key))
        except Exception:
            return None

    @staticmethod
    def _load(path: Path, key: str) -> dict[str, tuple[str, ...]]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return {}
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > _IDENTITY_SCAN_CACHE_MAX_BYTES
            ):
                return {}
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                payload = json.loads(handle.read().decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "key", "entries"}
                or payload["version"] != IDENTITY_SCAN_CACHE_VERSION
                or payload["key"] != key
                or not isinstance(payload["entries"], dict)
                or len(payload["entries"]) > _IDENTITY_SCAN_CACHE_MAX_ENTRIES
            ):
                return {}
            entries: dict[str, tuple[str, ...]] = {}
            for name, digests in payload["entries"].items():
                if (
                    not _HEX64.fullmatch(name)
                    or not isinstance(digests, list)
                    or len(digests) > 1
                    or not all(
                        isinstance(value, str) and _HEX64.fullmatch(value)
                        for value in digests
                    )
                ):
                    # 1件でも形が違えば控え全体を信用しない。
                    return {}
                entries[name] = tuple(digests)
            return entries
        except Exception:
            return {}
        finally:
            if fd >= 0:
                os.close(fd)

    def lookup(self, content_sha256: str) -> tuple[str, ...] | None:
        return self.entries.get(content_sha256)

    def record(self, content_sha256: str, normalizer_digests: tuple[str, ...]) -> None:
        if len(normalizer_digests) > 1:
            return
        if self.entries.get(content_sha256) != normalizer_digests:
            self.entries[content_sha256] = normalizer_digests
            self.dirty = True

    def save(self) -> None:
        if not self.dirty or len(self.entries) > _IDENTITY_SCAN_CACHE_MAX_ENTRIES:
            return
        temp = None
        try:
            import tempfile

            fd, temp = tempfile.mkstemp(
                prefix=".identity-scan-", suffix=".tmp", dir=self.path.parent
            )
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(
                    json.dumps(
                        {
                            "version": IDENTITY_SCAN_CACHE_VERSION,
                            "key": self.key,
                            "entries": {
                                name: list(digests)
                                for name, digests in sorted(self.entries.items())
                            },
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            os.replace(temp, self.path)
            temp = None
            self.dirty = False
        except Exception:
            pass
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass


def sanitized_ref_report(
    report: dict,
    refs: tuple[str | None, ...],
    private_identity: PrivateIdentityScan | None,
) -> dict:
    """入力refの公開可否を確認し、派生ラベルも同じ判定で秘匿する。"""
    labels = tuple(dict.fromkeys(ref for ref in refs if ref))
    if not labels:
        return report
    replacements = []
    unknown = 0
    for label in labels:
        status = (
            private_identity.label_status(label)
            if private_identity is not None
            else "unknown"
        )
        unknown += status == "unknown"
        safe = sanitized_evidence_label(label, private_name=status != "public")
        if safe != label:
            # 正確なrefだけを置換。annでannualを隠さず、Git rangeの..は境界とする。
            pattern = re.compile(
                r"(?<![\w./-])" + re.escape(label) + r"(?=\.\.|$|[^\w./-])"
            )
            replacements.append((len(label), pattern, safe))
    replacements.sort(key=lambda item: item[0], reverse=True)

    ref_label_fields = {
        "base_ref",
        "resolved_base_ref",
        "consistency_base_ref",
        "identity_base_ref",
        "git_object_scope",
    }

    def scrub(value, field=None):
        if isinstance(value, str) and field in ref_label_fields:
            for _, pattern, safe in replacements:
                value = pattern.sub(lambda match: safe, value)
            return value
        if isinstance(value, dict):
            # schema/status/説明文はref値ではない。ref専用fieldだけを秘匿する。
            return {key: scrub(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item, field) for item in value]
        return value

    sanitized = scrub(report)
    sanitized["ref_label_privacy"] = {
        "status": "unknown" if unknown else "complete",
        "checked_ref_count": len(labels),
        "unverified_ref_count": unknown,
    }
    return sanitized


def fixed_history_revisions(repo: Path, rev_args: tuple[str, ...]) -> tuple[str, ...]:
    """履歴の省略を拒否し、可変refをraw objectの固定OID閉包へ束縛する。"""
    shallow_code, shallow = run(repo, "git", "rev-parse", "--is-shallow-repository")
    graft_code, graft_path = run(repo, "git", "rev-parse", "--git-path", "info/grafts")
    if shallow_code or shallow != "false" or graft_code or not graft_path:
        raise RuntimeError("git_raw_history_context_unverified")
    path = Path(graft_path)
    if not path.is_absolute():
        path = repo / path
    if path.exists():
        raise RuntimeError("git_grafts_history_unverified")
    args = (*rev_args, "HEAD") if "--all" in rev_args else rev_args
    code, values = run(repo, "git", "rev-parse", "--revs-only", *args)
    revisions = tuple(dict.fromkeys(values.splitlines()))
    if (
        code
        or not revisions
        or any(
            not re.fullmatch(r"\^?(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
            for value in revisions
        )
    ):
        raise RuntimeError("git_history_roots_unverified")
    return revisions


def _guard_worktree_parent_components(repo: Path, path: Path) -> None:
    """正規化済みrepo rootより下の全親componentを、参照先を辿らず確認する。"""
    try:
        relative = path.relative_to(repo)
        if ".." in relative.parts:
            raise ValueError("outside_inventory")
        parent = repo
        for component in relative.parts[:-1]:
            parent = parent / component
            try:
                info = parent.lstat()
            except FileNotFoundError:
                # 消えたtracked fileは既存のdeleted inventoryが扱う。
                return
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("linked_parent")
    except (OSError, ValueError) as exc:
        raise RuntimeError("git_worktree_inventory_failed") from exc


# 履歴objectの読取上限は、直下のbatch読取予算と同一の既存値。別の上限は持たない。
# この予算以内は本文を読み、超過だけを未検査として残す。
_HISTORY_BATCH_BUDGET_BYTES = 16_000_000


def history_hits(
    repo: Path,
    rev_args: tuple[str, ...] = ("--all",),
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    private_identity: PrivateIdentityScan | None = None,
) -> tuple[list[str], list[str]]:
    revisions = fixed_history_revisions(repo, rev_args)
    if private_identity is not None:
        private_identity.report["closure_revisions_sha256"] = hashlib.sha256(
            "\n".join(revisions).encode("ascii")
        ).hexdigest()
        private_identity.report["replacement_objects_disabled"] = True
    objects = run_subprocess(
        ["git", "rev-list", "--objects", "--no-object-names", *revisions],
        cwd=repo,
        capture_output=True,
    )
    if objects.returncode:
        raise RuntimeError("git_history_inventory_failed")
    inventory_by_id: dict[str, str] = {}
    for line in objects.stdout.splitlines():
        object_id, _, raw_name = line.partition(b" ")
        try:
            oid = object_id.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if oid:
            inventory_by_id.setdefault(
                oid, raw_name.decode("utf-8", errors="surrogateescape")
            )
    inventory = list(inventory_by_id.items())
    # 空range (base == HEAD 等) は検査対象ゼロの成功。空入力をcat-fileへ
    # 渡すと出力行数が合わず inventory 失敗と誤判定するため、ここで返す
    if not inventory:
        return [], []
    object_ids = [object_id for object_id, _ in inventory]
    check = run_subprocess(
        ["git", "cat-file", "--batch-check"],
        cwd=repo,
        input="\n".join(object_ids) + "\n",
        text=True,
        capture_output=True,
    )
    if check.returncode:
        raise RuntimeError("git_history_inventory_failed")
    metadata_lines = check.stdout.splitlines()
    if len(metadata_lines) != len(inventory):
        raise RuntimeError("git_history_inventory_failed")
    eligible: list[tuple[str, str, int, str]] = []
    for source, metadata in _zip_exact(inventory, metadata_lines):
        object_id, name = source
        fields = metadata.split()
        if len(fields) != 3 or fields[0] != object_id:
            raise RuntimeError("git_history_inventory_failed")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if fields[1] not in {"blob", "commit", "tree", "tag"} or size < 0:
            raise RuntimeError("git_history_inventory_failed")
        if fields[1] == "blob" or (
            private_identity is not None and fields[1] in {"commit", "tag", "tree"}
        ):
            if size <= _HISTORY_BATCH_BUDGET_BYTES:
                eligible.append((object_id, name, size, fields[1]))
            elif private_identity is not None:
                private_identity.skipped += 1
    aliases: dict[str, set[str]] = {}
    if reviewed_exceptions is not None:
        # rev-list --objectsでは同一blobの別pathが失われる。全到達treeをNUL区切りで読む。
        roots = run_subprocess(
            ["git", "log", "--format=%T", *revisions], cwd=repo, capture_output=True
        )
        if roots.returncode:
            raise RuntimeError("git_history_inventory_failed")
        for tree in set(roots.stdout.splitlines()):
            tree_files = run_subprocess(
                ["git", "ls-tree", "-r", "-z", tree.decode("ascii")],
                cwd=repo,
                capture_output=True,
            )
            if tree_files.returncode:
                raise RuntimeError("git_history_inventory_failed")
            for record in tree_files.stdout.split(b"\0"):
                if not record:
                    continue
                metadata, sep, raw_path = record.partition(b"\t")
                fields = metadata.split()
                if not sep or len(fields) != 3:
                    raise RuntimeError("git_history_inventory_failed")
                if fields[1] == b"blob":
                    aliases.setdefault(fields[2].decode("ascii"), set()).add(
                        raw_path.decode("utf-8", errors="surrogateescape")
                    )
    secret_hits: set[str] = set()
    path_hits: set[str] = set()
    eligible.reverse()
    while eligible:
        chunk: list[tuple[str, str, int, str]] = []
        chunk_bytes = 0
        while eligible and (
            not chunk or chunk_bytes + eligible[-1][2] <= _HISTORY_BATCH_BUDGET_BYTES
        ):
            item = eligible.pop()
            chunk.append(item)
            chunk_bytes += item[2]
        batch = run_subprocess(
            ["git", "cat-file", "--batch"],
            cwd=repo,
            input=("\n".join(object_id for object_id, _, _, _ in chunk) + "\n").encode(
                "ascii"
            ),
            capture_output=True,
        )
        if batch.returncode:
            raise RuntimeError("git_history_inventory_failed")
        cursor = 0
        try:
            for object_id, name, expected_size, object_type in chunk:
                line_end = batch.stdout.index(b"\n", cursor)
                header = batch.stdout[cursor:line_end].decode("ascii").split()
                if (
                    len(header) != 3
                    or header[0] != object_id
                    or header[1] != object_type
                ):
                    raise RuntimeError("git_history_inventory_failed")
                size = int(header[2])
                if size != expected_size:
                    raise RuntimeError("git_history_inventory_failed")
                start = line_end + 1
                end = start + size
                if end >= len(batch.stdout) or batch.stdout[end : end + 1] != b"\n":
                    raise RuntimeError("git_history_inventory_failed")
                data = batch.stdout[start:end]
                cursor = end + 1
                # Gitのobject形式に対して、実際に読んだbytesと固定OIDの一致を確認。
                digest = hashlib.new(
                    "sha1" if len(object_id) == 40 else "sha256", usedforsecurity=False
                )
                digest.update(f"{object_type} {len(data)}\0".encode("ascii") + data)
                if digest.hexdigest() != object_id:
                    raise RuntimeError("git_raw_object_digest_mismatch")
                if private_identity is not None:
                    names = aliases.get(object_id)
                    reviewed_raw = bool(
                        object_type == "blob"
                        and reviewed_exceptions is not None
                        and names
                        and all(
                            reviewed_exceptions.all_paths_reviewed(
                                alias,
                                data,
                                record=False,
                                normalized=True,
                                normalizer=private_identity.path_normalizer,
                            )
                            for alias in names
                        )
                    )
                    private_identity.check(data, reviewed_raw_paths=reviewed_raw)
                if object_type != "blob":
                    continue
                label = f"history:{sanitized_evidence_label(name or object_id[:12])}"
                if reviewed_exceptions is None:
                    if text_has(SECRET_PATTERNS, data):
                        secret_hits.add(label)
                else:
                    names = aliases.get(object_id)
                    if not names:
                        # detached blob/tag等でpath根拠なしなら例外を使わない。
                        if text_has(SECRET_PATTERNS, data):
                            secret_hits.add(label)
                    for alias in sorted(names or ()):
                        if reviewed_exceptions.has_unreviewed(alias, data):
                            secret_hits.add(
                                f"history:{object_id[:12]}:{sanitized_evidence_label(alias)}"
                            )
                if reviewed_exceptions is None:
                    if text_has(PATH_PATTERNS, data):
                        path_hits.add(label)
                else:
                    names = aliases.get(object_id)
                    if not names and personal_path_matches(data):
                        path_hits.add(label)
                    for alias in sorted(names or ()):
                        if reviewed_exceptions.has_unreviewed_paths(alias, data):
                            path_hits.add(
                                f"history:{object_id[:12]}:{sanitized_evidence_label(alias)}"
                            )
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if cursor != len(batch.stdout):
            raise RuntimeError("git_history_inventory_failed")
    return sorted(secret_hits), sorted(path_hits)


def diff_added_lines_have(
    repo: Path,
    patterns: tuple[re.Pattern, ...],
    *diff_args: str,
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    scan_path: str | None = None,
) -> bool:
    """Return whether added text lines in one Git diff match ``patterns``.

    A target-diff scan must not reclassify unchanged baseline text merely because
    its file also contains a new, unrelated line.  Patch metadata is excluded;
    only actual ``+`` lines are decoded by ``text_has``.
    """
    if reviewed_exceptions is not None and reviewed_exceptions.has_path_entries:
        inventory = run_subprocess(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                *diff_args,
                "--",
            ],
            cwd=repo,
            capture_output=True,
        )
        if inventory.returncode:
            raise RuntimeError("git_target_diff_inventory_failed")
        for raw_path in inventory.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if len(diff_args) == 2:
                blob = run_subprocess(
                    ["git", "show", f"{diff_args[1]}:{path}"],
                    cwd=repo,
                    capture_output=True,
                )
                data = blob.stdout if blob.returncode == 0 else None
            else:
                candidate = repo / path
                if candidate.is_symlink():
                    data = None
                else:
                    try:
                        data = candidate.read_bytes()
                    except FileNotFoundError:
                        data = None
                    except OSError as exc:
                        raise RuntimeError("git_target_diff_inventory_failed") from exc
            if data is not None and reviewed_exceptions.all_paths_reviewed(path, data):
                continue
            if diff_added_lines_have(repo, patterns, *diff_args, scan_path=path):
                return True
        return False
    pathspec = ["--", ":(literal)" + scan_path] if scan_path is not None else ["--"]
    result = run_subprocess(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--unified=0",
            *diff_args,
            *pathspec,
        ],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    added_lines: list[bytes] = []
    in_hunk = False
    for line in result.stdout.splitlines():
        if line.startswith(b"diff --git "):
            in_hunk = False
        elif line.startswith(b"@@"):
            in_hunk = True
        elif line.startswith((b"--- ", b"+++ ")) and not in_hunk:
            continue
        elif in_hunk and line.startswith(b"+"):
            added_lines.append(line[1:])
    added = b"\n".join(added_lines)
    if added and text_has(patterns, added):
        return True

    # Git omits patch lines for UTF-16 and attribute-classified binary text.
    # Inspect only those binary entries and compare decoded blob lines, so an
    # unchanged baseline path in a modified file is not reclassified as new.
    numstat = run_subprocess(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--numstat",
            "-z",
            *diff_args,
            *pathspec,
        ],
        cwd=repo,
        capture_output=True,
    )
    if numstat.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    revisions = list(diff_args)
    old_revision = revisions[0] if len(revisions) == 2 else "HEAD"
    new_revision = revisions[1] if len(revisions) == 2 else None
    for entry in numstat.stdout.split(b"\0"):
        if not entry:
            continue
        additions, separator, remainder = entry.partition(b"\t")
        deletions, separator2, raw_path = remainder.partition(b"\t")
        if not separator or not separator2 or additions != b"-" or deletions != b"-":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        old_blob = run_subprocess(
            ["git", "show", f"{old_revision}:{path}"],
            cwd=repo,
            capture_output=True,
        )
        old_data = old_blob.stdout if old_blob.returncode == 0 else b""
        if new_revision is None:
            candidate = repo / path
            if candidate.is_symlink():
                continue
            try:
                new_data = candidate.read_bytes()
            except OSError as exc:
                raise RuntimeError("git_target_diff_inventory_failed") from exc
        else:
            new_blob = run_subprocess(
                ["git", "show", f"{new_revision}:{path}"],
                cwd=repo,
                capture_output=True,
            )
            if new_blob.returncode:
                continue
            new_data = new_blob.stdout
        old_text = _decode_scannable_text(old_data)
        new_text = _decode_scannable_text(new_data)
        if new_text is None:
            # Genuinely binary content (non-UTF-8/UTF-16 noise) that Git also
            # omits from the unified diff. Fall back to a raw-byte pattern
            # search so an embedded ASCII personal path is not skipped just
            # because the surrounding bytes cannot be decoded as text. Only
            # report a hit that is new relative to the comparison base, so an
            # unchanged baseline blob is not reclassified.
            if _bytes_pattern_hit(patterns, new_data) and not _bytes_pattern_hit(
                patterns, old_data
            ):
                return True
            continue
        old_lines = old_text.splitlines() if old_text is not None else []
        new_lines = new_text.splitlines()
        introduced = "\n".join(
            line
            for operation, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
                a=old_lines, b=new_lines, autojunk=False
            ).get_opcodes()
            if operation in {"insert", "replace"}
            for line in new_lines[j1:j2]
        ).encode("utf-8")
        if introduced and text_has(patterns, introduced):
            return True
    return False


def _decode_scannable_text(data: bytes) -> str | None:
    for encoding in ("utf-8", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


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
    return any(re.search(pattern.pattern.encode("ascii"), data) for pattern in patterns)


def cmo3_contents_xml(data: bytes) -> bytes | None:
    """Return the ``contents`` XML inside a .cmo3, or None if it cannot be read.

    XOR-ing every byte with 0xE3 exposes a zip local file header.  Its flag
    0x0808 leaves the header sizes at zero, so the raw deflate stream right
    after the header is inflated until the stream itself ends, then checked
    against the CRC-32 and size in the data descriptor that follows it (raw
    deflate alone can decode damaged bytes into plausible garbage).  The header
    offset is not fixed, so every signature is tried.  A truncated or corrupt
    entry, or expanding past ``CMO3_MAX_XML_BYTES`` across all entries, makes
    the whole file unreadable rather than partly clean.
    """
    plain = data.translate(_CMO3_XOR_TABLE)
    documents: list[bytes] = []
    for match in _ZIP_LOCAL_HEADER.finditer(plain):
        offset = match.start()
        if len(plain) < offset + 30:
            continue
        (method,) = struct.unpack_from("<H", plain, offset + 8)
        name_length, extra_length = struct.unpack_from("<HH", plain, offset + 26)
        name_start = offset + 30
        if (
            method != 8
            or plain[name_start : name_start + name_length] != CMO3_ENTRY_NAME
        ):
            continue
        # 上限は全entryの合計に効かせる (0 は zlib で「無制限」の意味になる)
        remaining = CMO3_MAX_XML_BYTES - sum(map(len, documents))
        if remaining <= 0:
            return None
        inflater = zlib.decompressobj(-15)
        try:
            document = inflater.decompress(
                plain[name_start + name_length + extra_length :], remaining
            )
        except zlib.error:
            return None
        if not inflater.eof:
            return None
        descriptor = inflater.unused_data
        if descriptor.startswith(b"PK\x07\x08"):
            descriptor = descriptor[4:]
        if len(descriptor) < 12:
            return None
        crc, _compressed_size, size = struct.unpack_from("<III", descriptor)
        if crc != zlib.crc32(document) or size != len(document):
            return None
        documents.append(document)
    return b"\n".join(documents) if documents else None


def comparison_parent(repo: Path, base_ref: str, commit: str) -> str:
    """Prefer a base-containing parent, then a unique base ancestor."""
    parents = run_subprocess(
        ["git", "show", "-s", "--format=%P", commit],
        cwd=repo,
        text=True,
        encoding="ascii",
        errors="strict",
        capture_output=True,
    )
    if parents.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    candidates = parents.stdout.split()
    if not candidates:
        empty_tree = run_subprocess(
            ["git", "hash-object", "-t", "tree", "--stdin"],
            cwd=repo,
            input=b"",
            capture_output=True,
        )
        if empty_tree.returncode:
            raise RuntimeError("git_target_diff_inventory_failed")
        return empty_tree.stdout.decode("ascii").strip()
    for parent in candidates:
        contains_base = run_subprocess(
            ["git", "merge-base", "--is-ancestor", base_ref, parent],
            cwd=repo,
            capture_output=True,
        )
        if contains_base.returncode == 0:
            return parent
        if contains_base.returncode != 1:
            raise RuntimeError("git_target_diff_inventory_failed")
    if len(candidates) > 1:
        # mainが先へ進んでも、過去のmergeで取り込んだmain由来の追加を
        # branchの新規追加として再検出しない。曖昧ならfirst parentを保つ。
        base_ancestors = []
        for parent in candidates:
            ancestor_of_base = run_subprocess(
                ["git", "merge-base", "--is-ancestor", parent, base_ref],
                cwd=repo,
                capture_output=True,
            )
            if ancestor_of_base.returncode == 0:
                base_ancestors.append(parent)
            elif ancestor_of_base.returncode != 1:
                raise RuntimeError("git_target_diff_inventory_failed")
        if len(base_ancestors) == 1:
            return base_ancestors[0]
    return candidates[0]


def target_diff_personal_path_hits(
    repo: Path,
    base_ref: str,
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
) -> list[str]:
    """Scan personal paths introduced anywhere after ``base_ref``.

    Each commit is compared with its first parent so a path that was introduced
    and later removed remains detectable.  The current index/worktree diff is
    checked separately.  Evidence labels intentionally avoid file contents and
    checkout paths.
    """
    commits = run_subprocess(
        ["git", "rev-list", "--reverse", f"{base_ref}..HEAD"],
        cwd=repo,
        text=True,
        encoding="ascii",
        errors="strict",
        capture_output=True,
    )
    if commits.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    hits: set[str] = set()
    for commit in commits.stdout.splitlines():
        parent = comparison_parent(repo, base_ref, commit)
        if diff_added_lines_have(
            repo, PATH_PATTERNS, parent, commit, reviewed_exceptions=reviewed_exceptions
        ):
            hits.add(f"target-diff:commit:{commit[:12]}")
    if diff_added_lines_have(
        repo, PATH_PATTERNS, "HEAD", reviewed_exceptions=reviewed_exceptions
    ):
        hits.add("target-diff:working-tree")
    untracked = run_subprocess(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
    )
    if untracked.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    for raw_name in untracked.stdout.split(b"\0"):
        if not raw_name:
            continue
        path = repo / raw_name.decode("utf-8", errors="surrogateescape")
        _guard_worktree_parent_components(repo, path)
        if path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RuntimeError("git_target_diff_inventory_failed") from exc
        # 全体decode失敗時も raw bytes で拾う (診断は diff_added_lines_have と同じ)
        if (
            reviewed_exceptions.has_unreviewed_paths(
                path.relative_to(repo).as_posix(), data
            )
            if reviewed_exceptions is not None
            else text_has(PATH_PATTERNS, data)
            or _bytes_pattern_hit(PATH_PATTERNS, data)
        ):
            hits.add("target-diff:untracked-worktree")
            break
    return sorted(hits)


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


# impact_map 等の change-sensitive 整合性検査向け。README / OPERATIONS の最短
# コマンド（base 未指定）でも同じ remote base を使う。
DEFAULT_CONSISTENCY_BASE_REF = "origin/main"


def run_consistency_gate(repo: Path, base_ref: str | None) -> dict:
    script = Path(__file__).with_name("consistency_gate.py")
    spec = importlib.util.spec_from_file_location("consistency_gate", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
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


def scan(
    repo: Path,
    expected_identity: str | None = None,
    *,
    release: bool = False,
    base_ref: str | None = None,
    consistency_base_ref: str | None = None,
    identity_base_ref: str | None = None,
    reviewed_secret_exceptions: Path | None = None,
    require_private_identity: bool = False,
    allowed_identities: Path | None = None,
) -> dict:
    repo = repo.resolve()
    root_error = repository_root_error(repo)
    if root_error is not None:
        return root_error
    repo = Path(run(repo, "git", "rev-parse", "--show-toplevel")[1]).resolve()
    if not base_ref:
        shallow_code, shallow = run(repo, "git", "rev-parse", "--is-shallow-repository")
        if shallow_code or shallow not in {"true", "false"}:
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": ["git_history_inventory_failed"],
            }
        if shallow == "true":
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": ["shallow_history_unavailable"],
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
    if consistency_base_ref is None and base_ref is None:
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
                "repo": repository_evidence_label(repo),
                "issues": ["invalid_non_remote_or_non_ancestor_base_ref"],
            }
        identity_range = f"{base_ref}..HEAD"
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
            f"{identity_base[1]}..HEAD",
        )
        probes["historical_identities"] = run(
            repo,
            "git",
            "log",
            "--format=%an <%ae>|%cn <%ce>%n%(trailers:key=Co-authored-by,valueonly)",
            "--all",
        )
    else:
        probes["identities"] = run(
            repo,
            "git",
            "log",
            "--format=%an <%ae>|%cn <%ce>%n%(trailers:key=Co-authored-by,valueonly)",
            "--all",
        )
    if any(code for code, _ in probes.values()):
        return {
            "status": "tool_error",
            "repo": repository_evidence_label(repo),
            "issues": ["git_probe_failed"],
        }
    head = probes["head"][1]
    dirty = probes["dirty"][1]
    identities = probes["identities"][1]
    resolved_base_ref: str | None = None
    base_oid: str | None = None
    if base_ref:
        resolved_base_ref = symbolic_probe[1]
        base_oid = base_probe[1]
    _, remote = run(repo, "git", "remote", "get-url", "origin")
    reviewed_exceptions = None
    if reviewed_secret_exceptions is not None:
        try:
            reviewed_exceptions = ReviewedSecretExceptions(
                Path(reviewed_secret_exceptions), remote
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
    missing = (
        [] if base_ref else [name for name in REQUIRED if not (repo / name).is_file()]
    )
    invalid_documents: list[str] = []
    review_record = repo / REVIEW_RECORD
    if not base_ref and REVIEW_RECORD not in missing:
        try:
            record_text = review_record.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 読めない記録を「存在するからpass」にしない
            invalid_documents.append(REVIEW_RECORD)
        else:
            if REVIEW_RECORD_MARKER not in record_text:
                invalid_documents.append(REVIEW_RECORD)
    private_identity = PrivateIdentityScan(repo, require_private_identity)
    credential_finding_count = 0
    path_hits: list[str] = []
    unscanned_path_files: list[str] = []
    try:
        if base_ref:
            paths, deleted_paths = changed_working_tree_files(repo, base_ref)
        else:
            paths = working_tree_files(repo)
            deleted_paths = deleted_working_tree_files(repo)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": repository_evidence_label(repo),
            "issues": ["git_worktree_inventory_failed"],
        }
    # 個人識別子は作業treeの在庫全体を見る。差分に無い既存JPEGを復号できないときだけ、
    # そのfileを未展開として続け、Pull Request全体は止めない。差分と未commit変更のJPEGは止める。
    private_worktree_enabled = private_identity.checker is not None
    private_worktree_inventory_count = 0
    private_worktree_scanned_count = 0
    private_worktree_cache_reused = 0
    private_worktree_symlinks_excluded = 0
    private_worktree_sensitive_names: set[str] = set()
    submission_rels: set[str] | None = None
    if private_identity.checker is not None:
        try:
            if base_ref:
                private_paths = working_tree_files(repo)
                submission_rels = {
                    path.relative_to(repo).as_posix()
                    for path in submission_files(repo, paths)
                }
            else:
                private_paths = paths
            private_deleted = deleted_paths
        except RuntimeError:
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
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
            repo, private_identity, reviewed_secret_exceptions, remote
        )
        for path in current_paths:
            relative = path.relative_to(repo)
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
            if private_identity.checker is None:
                break
            label = sanitized_evidence_label(
                rel,
                private_name=(
                    rel in private_worktree_sensitive_names
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
                    "repo": repository_evidence_label(repo),
                    "issues": [f"worktree_file_unreadable:{label}"],
                }
            content_sha256 = None
            if identity_cache is not None:
                # 同じ鍵で合格済みの内容なら本文検査を省く。ファイル名の検査は毎回行う。
                content_sha256 = hashlib.sha256(data).hexdigest()
                cached = identity_cache.lookup(content_sha256)
                if cached is not None and private_identity.reuse_clean_pass(cached):
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
            private_identity.check(
                data,
                reviewed_raw_paths=reviewed_raw,
                defer_unverified_jpeg=(
                    submission_rels is not None and rel not in submission_rels
                ),
            )
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
        rel = path.relative_to(repo).as_posix()
        label = sanitized_evidence_label(
            rel,
            private_name=(
                rel in private_worktree_sensitive_names
                or private_identity.report.get("status") == "tool_error"
            ),
        )
        if not path.is_file():
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": [f"worktree_file_unreadable:{label}"],
            }
        try:
            data = path.read_bytes()
        except OSError:
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": [f"worktree_file_unreadable:{label}"],
            }
        if (
            reviewed_exceptions.has_unreviewed(rel, data)
            if reviewed_exceptions is not None
            else text_has(SECRET_PATTERNS, data)
        ):
            credential_finding_count += 1
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
    try:
        history_credential_findings, history_path_hits = history_hits(
            repo,
            (f"{base_ref}..HEAD",) if base_ref else ("--all",),
            reviewed_exceptions,
            private_identity if private_identity.checker is not None else None,
        )
        credential_finding_count += len(history_credential_findings)
    except RuntimeError:
        return {
            "status": "tool_error",
            "repo": repository_evidence_label(repo),
            "issues": ["git_history_inventory_failed"],
        }
    if base_ref:
        try:
            path_hits.extend(
                target_diff_personal_path_hits(repo, base_ref, reviewed_exceptions)
            )
        except RuntimeError:
            return {
                "status": "tool_error",
                "repo": repository_evidence_label(repo),
                "issues": ["git_target_diff_inventory_failed"],
            }
    else:
        path_hits.extend(history_path_hits)
    identity_lines = {line for line in identities.splitlines() if line}
    identity_mismatches = {
        line
        for line in identity_lines
        if expected_identity
        and (
            line.split("|")[0] != expected_identity
            or line.split("|")[-1] != expected_identity
        )
    }
    if allowed_identity_set is not None:
        identity_mismatches |= {
            line
            for line in identity_lines
            if not _identity_line_allowed(allowed_identity_set, line)
        }
    historical_identity_lines = {
        line
        for line in probes.get("historical_identities", probes["identities"])[
            1
        ].splitlines()
        if line
    }
    historical_identity_mismatches = {
        line
        for line in historical_identity_lines
        if expected_identity
        and (
            line.split("|")[0] != expected_identity
            or line.split("|")[-1] != expected_identity
        )
    }
    # 現在設定の名義はexpected_identity指定時だけ測る。identity未設定環境
    # (CI containerなど) をtool_errorにせず、判定はunknownでfail-closedに保つ
    effective_status = "not_evaluated"
    effective_mismatches: set[str] = set()
    if expected_identity:
        effective_probes = (
            run(repo, "git", "var", "GIT_AUTHOR_IDENT"),
            run(repo, "git", "var", "GIT_COMMITTER_IDENT"),
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
        list((repo / ".github" / "workflows").glob("*.y*ml"))
        if (repo / ".github" / "workflows").is_dir()
        else []
    )
    invalid_workflows: list[str] = []
    for workflow in workflows:
        try:
            workflow_text = workflow.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            invalid_workflows.append(
                sanitized_evidence_label(
                    workflow.relative_to(repo).as_posix(),
                    private_name=(
                        workflow.relative_to(repo).as_posix()
                        in private_worktree_sensitive_names
                        or private_identity.report.get("status") == "tool_error"
                    ),
                )
            )
            continue
        if not re.search(r"(?m)^jobs\s*:", workflow_text):
            invalid_workflows.append(
                sanitized_evidence_label(
                    workflow.relative_to(repo).as_posix(),
                    private_name=(
                        workflow.relative_to(repo).as_posix()
                        in private_worktree_sensitive_names
                        or private_identity.report.get("status") == "tool_error"
                    ),
                )
            )
    dependency_files = sorted(
        name for name in DEPENDENCY_FILES if (repo / name).is_file()
    )
    private_identity_report = private_identity.result()
    private_identity_report.update(
        worktree_scope="git_inventory_regular_files_excluding_ignored_and_links",
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
    if reviewed_exceptions is not None and private_identity.checker is not None:
        private_identity_report["reviewed_path_exceptions"] = [
            {"id": key, "occurrence_count": count}
            for key, count in sorted(
                reviewed_exceptions.normalized_path_applied.items()
            )
        ]
    checks = {
        "clean_worktree": {"status": "pass" if not dirty else "fail"},
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
            "reviewed_exceptions": (
                reviewed_exceptions.report() if reviewed_exceptions is not None else []
            ),
        },
        "personal_path_scan": {
            "status": (
                "fail" if path_hits else "unknown" if unscanned_path_files else "pass"
            ),
            "files": path_hits,
            "unscanned_files": unscanned_path_files,
            **(
                {"reviewed_exceptions": reviewed_exceptions.report(personal_paths=True)}
                if reviewed_exceptions is not None
                else {}
            ),
        },
        "public_attribution": public_attribution_result(
            repo, base_ref, allowed_identity_set
        ),
        "private_identity_scan": private_identity_report,
        "identity_gate": _identity_gate_result(
            repo, base_ref, identity_base[1] if identity_base is not None else None
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
                if identity_base
                else "target_diff" if base_ref else "repository_history"
            ),
            "base_ref": identity_base[0] if identity_base else None,
            "base_oid": identity_base[1] if identity_base else None,
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
            "status": "pass" if remote else "unknown",
            "url": redact_remote(remote),
        },
    }
    if identity_base:
        checks["historical_identity_audit"] = {
            "status": "observed",
            "identity_count": len(historical_identity_lines),
            "mismatch_count": len(historical_identity_mismatches),
            "scope": "all_refs",
        }
    checks["repository_consistency"] = run_consistency_gate(
        repo, consistency_base_ref or base_ref
    )
    if release:
        release_baseline = (
            (resolved_consistency_base_ref, consistency_base_oid)
            if resolved_consistency_base_ref
            and resolved_consistency_base_ref.startswith("refs/tags/")
            and consistency_base_oid
            else None
        )
        checks["readme_release_freshness"] = check_release_readme_freshness(
            repo, baseline=release_baseline
        )
        if (repo / "README.md").is_file():
            readme_report = run_readme_release_gate(repo)
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
    automated_check_names = {
        "clean_worktree",
        "secret_scan",
        "personal_path_scan",
        "commit_identity",
        "identity_gate",
        "origin",
    }
    # CI（公開モード）は非公開の型設定を持たない。許可名義の一覧が渡され、
    # 非公開の型を必須にしていない時だけ、名義の判定を public_attribution に任せる。
    # 一覧も無い、または必須の時は従来どおり fail のまま（止める側に倒す）。
    identity_gate_report = checks["identity_gate"]
    if (
        identity_gate_report.get("reason") == "private_patterns_not_configured"
        and allowed_identity_set is not None
        and not require_private_identity
    ):
        checks["identity_gate"] = {
            "status": "not_configured",
            "reason": "private_patterns_not_configured",
            "covered_by": "public_attribution",
        }
        automated_check_names.discard("identity_gate")
    if (
        require_private_identity
        or private_identity.report["status"] != "not_configured"
    ):
        automated_check_names.add("private_identity_scan")
    if allowed_identity_set is not None:
        automated_check_names.add("public_attribution")
    if not base_ref:
        automated_check_names.update(
            {"required_documents", "dependency_configuration", "ci_configuration"}
        )
    if release:
        automated_check_names.add("readme_release_design")
        automated_check_names.add("readme_release_freshness")
    automated_check_names.add("repository_consistency")
    tool_error = any(
        checks[name]["status"] == "tool_error" for name in automated_check_names
    )
    blocking = any(checks[name]["status"] == "fail" for name in automated_check_names)
    unknown = any(checks[name]["status"] == "unknown" for name in automated_check_names)
    report = {
        "status": (
            "tool_error" if tool_error else "blocked" if blocking or unknown else "pass"
        ),
        "publication_decision": "blocked_human_review_required",
        "repo": repository_evidence_label(repo),
        "head": head,
        "scan_scope": (
            {
                "mode": "target_diff",
                "base_ref": sanitized_evidence_label(base_ref),
                "resolved_base_ref": sanitized_evidence_label(resolved_base_ref),
                "base_oid": base_oid,
            }
            if base_ref
            else {"mode": "repository"}
        ),
        "consistency_scope": (
            {
                "mode": "target_diff",
                "base_ref": sanitized_evidence_label(consistency_base_ref),
                "resolved_base_ref": sanitized_evidence_label(
                    resolved_consistency_base_ref
                ),
                "base_oid": consistency_base_oid,
            }
            if consistency_base_ref
            else {"mode": "same_as_scan"}
        ),
        "checks": checks,
    }
    return sanitized_ref_report(
        report,
        (
            base_ref,
            resolved_base_ref,
            consistency_base_ref,
            resolved_consistency_base_ref,
            identity_base_ref,
            identity_base[0] if identity_base else None,
        ),
        private_identity,
    )


class ScanOptions:
    """検査実行時の選択。importlib経由のテスト読込でも使えるようdataclassを避ける。"""

    def __init__(
        self,
        repo: Path | None,
        release: bool = False,
        expected_identity: str | None = None,
        audience: str = "unspecified",
        interactive: bool = False,
        show_json: bool = True,
        intent: str | None = None,
        base_ref: str | None = None,
        consistency_base_ref: str | None = None,
        identity_base_ref: str | None = None,
        github_settings_profile: str = "solo_public",
        reviewed_secret_exceptions: Path | None = None,
        allowed_identities: Path | None = None,
    ) -> None:
        self.repo = repo
        self.release = release
        self.expected_identity = expected_identity
        self.audience = audience
        self.interactive = interactive
        self.show_json = show_json
        self.intent = intent
        self.base_ref = base_ref
        self.consistency_base_ref = consistency_base_ref
        self.identity_base_ref = identity_base_ref
        self.github_settings_profile = github_settings_profile
        self.reviewed_secret_exceptions = reviewed_secret_exceptions
        self.allowed_identities = allowed_identities


def boundary_sections() -> dict[str, list[str]]:
    return {
        "guarantees": list(GUARANTEES),
        "non_guarantees": list(NON_GUARANTEES),
    }


def format_boundary_text() -> str:
    lines = [
        "## 保証すること (CLIが担当する範囲)",
        *(f"- {item}" for item in GUARANTEES),
        "",
        "## 保証しないこと (別証拠・人間判断が要る)",
        *(f"- {item}" for item in NON_GUARANTEES),
    ]
    return "\n".join(lines)


def enrich_report(report: dict, options: ScanOptions) -> dict:
    """scan結果に V3 の境界メタデータと選択オプションを載せる。"""
    enriched = dict(report)
    enriched["schema"] = SCHEMA
    enriched["options"] = {
        "audience": options.audience,
        "mode": (
            "target_diff"
            if options.base_ref
            else "release" if options.release else "standard"
        ),
        "expected_identity_configured": bool(options.expected_identity),
        "interactive": options.interactive,
        "intent": options.intent,
        "base_ref": (
            sanitized_evidence_label(options.base_ref) if options.base_ref else None
        ),
        "consistency_base_ref": (
            sanitized_evidence_label(options.consistency_base_ref)
            if options.consistency_base_ref
            else None
        ),
        "identity_base_ref": (
            sanitized_evidence_label(options.identity_base_ref)
            if options.identity_base_ref
            else None
        ),
        "github_settings_profile": options.github_settings_profile,
        "reviewed_secret_exceptions_configured": options.reviewed_secret_exceptions
        is not None,
    }
    enriched.update(boundary_sections())
    label_checker = None
    if options.repo is not None:
        expected = report.get("checks", {}).get("private_identity_scan", {})
        candidate = PrivateIdentityScan(options.repo.resolve(), False)
        # optionsの再構成時に、scanと異なるpolicy/providerからraw名を戻さない。
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
    return sanitized_ref_report(
        enriched,
        (options.base_ref, options.consistency_base_ref, options.identity_base_ref),
        label_checker,
    )


def build_intent_dialogue(options: ScanOptions) -> dict:
    """AI が外部操作直前に使う質問パケットを作る。"""
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
        scan_report = enrich_report(
            scan_report,
            ScanOptions(
                repo=options.repo,
                release=release,
                expected_identity=options.expected_identity,
                audience=options.audience,
                interactive=options.interactive,
                show_json=options.show_json,
                intent=options.intent,
                base_ref=options.base_ref,
                consistency_base_ref=options.consistency_base_ref,
                identity_base_ref=options.identity_base_ref,
                github_settings_profile=options.github_settings_profile,
                reviewed_secret_exceptions=options.reviewed_secret_exceptions,
                allowed_identities=options.allowed_identities,
            ),
        )
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


def dialogue_exit_code(dialogue: dict) -> int:
    status = dialogue.get("status")
    if status == "ready_after_confirmation":
        return 0
    if status == "blocked":
        return 1
    if status == "needs_human_input":
        return 1
    return 2


def format_check_line(name: str, check: dict) -> str:
    status = check.get("status", "unknown")
    detail_parts: list[str] = []
    if name == "secret_scan" and "finding_count" in check:
        detail_parts.append(f"findings={check['finding_count']}")
    if name == "required_documents":
        if check.get("missing"):
            detail_parts.append(f"missing={','.join(check['missing'])}")
        if check.get("invalid"):
            detail_parts.append(f"invalid={','.join(check['invalid'])}")
    if name == "personal_path_scan" and check.get("files"):
        detail_parts.append(f"files={len(check['files'])}")
    if name == "personal_path_scan" and check.get("unscanned_files"):
        detail_parts.append(f"unscanned={len(check['unscanned_files'])}")
    if name == "commit_identity":
        detail_parts.append(f"identities={check.get('identity_count', 0)}")
        if check.get("mismatch_count"):
            detail_parts.append(f"mismatches={check['mismatch_count']}")
    if name == "ci_configuration":
        detail_parts.append(f"workflows={check.get('workflow_count', 0)}")
    if check.get("reason"):
        detail_parts.append(str(check["reason"]))
    detail = f" ({'; '.join(detail_parts)})" if detail_parts else ""
    return f"- {name}: {status}{detail}"


def format_human_report(report: dict) -> str:
    lines = [
        "# Repo Preflight 結果",
        "",
        f"schema: {report.get('schema', SCHEMA)}",
        f"status: {report.get('status')}",
        f"publication_decision: {report.get('publication_decision', 'n/a')}",
    ]
    if report.get("repo"):
        lines.append(f"repo: {report['repo']}")
    if report.get("head"):
        lines.append(f"head: {report['head']}")
    options = report.get("options") or {}
    if options:
        lines.append(
            "options: "
            f"audience={options.get('audience')}, "
            f"mode={options.get('mode')}, "
            f"expected_identity_configured={options.get('expected_identity_configured')}"
        )
    lines.extend(["", format_boundary_text(), ""])
    if report.get("issues"):
        lines.append("## issues")
        lines.extend(f"- {issue}" for issue in report["issues"])
        lines.append("")
    checks = report.get("checks") or {}
    if checks:
        lines.append("## checks")
        for name, check in checks.items():
            if isinstance(check, dict):
                lines.append(format_check_line(name, check))
        lines.append("")
    lines.extend(
        [
            "## 読み方",
            "- status:pass は「このCLIが担当するローカル自動検査に合格した」だけを意味します。",
            "- publication_decision は常に人間レビュー要求です。pass だけを根拠に公開しないでください。",
            "- unknown / fail の項目は、別の証拠または人が埋めるまで先へ進めません。",
        ]
    )
    return "\n".join(lines)


def _emit(stream: TextIO, text: str) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def prompt_from_stdin(prompt: str = "") -> str:
    """対話入力。promptはstderrへ出し、stdoutのJSONを汚さない。"""
    if prompt:
        sys.stderr.write(prompt)
        sys.stderr.flush()
    line = sys.stdin.readline()
    if line == "":
        raise EOFError("EOF while reading interactive input")
    return line.rstrip("\r\n")


def prompt_text(
    message: str,
    *,
    default: str | None = None,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input_fn(f"{message}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        output_fn("値を入力してください。")


def prompt_choice(
    title: str,
    choices: tuple[tuple[str, str], ...],
    *,
    default_key: str,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> str:
    keys = [key for key, _ in choices]
    if default_key not in keys:
        raise ValueError(f"unknown default choice: {default_key}")
    output_fn(title)
    for index, (key, label) in enumerate(choices, start=1):
        marker = " (default)" if key == default_key else ""
        output_fn(f"  {index}) {key} — {label}{marker}")
    while True:
        raw = input_fn(f"番号または key [{default_key}]: ").strip().lower()
        if not raw:
            return default_key
        if raw.isdigit():
            position = int(raw)
            if 1 <= position <= len(choices):
                return choices[position - 1][0]
        for key, _ in choices:
            if raw == key.lower():
                return key
        output_fn("選択肢の番号または key を入力してください。")


def prompt_yes_no(
    message: str,
    *,
    default: bool,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        raw = input_fn(f"{message} [{default_label}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "はい"}:
            return True
        if raw in {"n", "no", "いいえ"}:
            return False
        output_fn("y または n で答えてください。")


def collect_interactive_options(
    *,
    initial_repo: Path | None = None,
    initial_release: bool = False,
    initial_identity: str | None = None,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    """対話で ScanOptions を集める。入出力はテスト差し替え可能。"""
    if output_fn is None:
        output_fn = lambda text: _emit(sys.stderr, text)

    output_fn("Repo Preflight v3 — 対話モード")
    output_fn("見せる相手を広げる前のローカル検査です。公開や push は実行しません。")
    output_fn("")
    output_fn(format_boundary_text())
    output_fn("")

    audience = prompt_choice(
        "見せる相手 (audience) を選んでください。必要な人間確認の観点が変わります。",
        AUDIENCE_CHOICES,
        default_key="local",
        input_fn=input_fn,
        output_fn=output_fn,
    )
    mode = prompt_choice(
        "検査モードを選んでください。",
        MODE_CHOICES,
        default_key="release" if initial_release else "standard",
        input_fn=input_fn,
        output_fn=output_fn,
    )
    release = mode == "release"

    default_repo = str(initial_repo) if initial_repo is not None else str(Path.cwd())
    repo_text = prompt_text(
        "検査対象リポジトリのパス",
        default=default_repo,
        input_fn=input_fn,
        output_fn=output_fn,
    )
    repo = Path(repo_text)

    use_identity = prompt_yes_no(
        "全commitの作者/committer名義を固定値と照合しますか?",
        default=bool(initial_identity),
        input_fn=input_fn,
        output_fn=output_fn,
    )
    expected_identity = initial_identity
    if use_identity:
        expected_identity = prompt_text(
            "期待する名義 (例: Example <dev@example.invalid>)",
            default=initial_identity or "",
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if not expected_identity:
            expected_identity = None
    else:
        expected_identity = None

    show_json = prompt_yes_no(
        "結果のJSONも表示しますか?",
        default=True,
        input_fn=input_fn,
        output_fn=output_fn,
    )

    output_fn("")
    output_fn("選択内容:")
    output_fn(f"- audience: {audience}")
    output_fn(f"- mode: {mode}")
    output_fn(f"- repo: {repo}")
    output_fn(
        f"- expected_identity: {expected_identity if expected_identity else '(未設定)'}"
    )
    output_fn(f"- show_json: {show_json}")
    if not prompt_yes_no(
        "この内容で検査を実行しますか?",
        default=True,
        input_fn=input_fn,
        output_fn=output_fn,
    ):
        raise SystemExit(1)

    return ScanOptions(
        repo=repo,
        release=release,
        expected_identity=expected_identity,
        audience=audience,
        interactive=True,
        show_json=show_json,
    )


def build_parser() -> argparse.ArgumentParser:
    gate = load_dialogue_gate()
    parser = argparse.ArgumentParser(
        description=(
            "Gitリポジトリを見せる相手を広げる前の読み取り専用 preflight。"
            "AI実装フローでは --intent で操作直前の質問パケットを出す。"
            "保証範囲と非保証範囲を常に表示する。"
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        help="検査対象リポジトリ。--intent create_repo 以外では必須",
    )
    parser.add_argument(
        "--reviewed-secret-exceptions",
        type=Path,
        help="明示レビュー済みの限定secret例外JSON。自動読込しない",
    )
    parser.add_argument(
        "--allowed-identities",
        type=Path,
        help="公開してよいcommit名義の一覧JSON。author / committer がここに無ければ commit_identity を fail にする",
    )
    parser.add_argument(
        "--intent",
        choices=list(gate.INTENTS),
        help=(
            "AIが実行しようとする操作。"
            "create_repo / push / open_pr / merge / configure_settings / publish / release。"
            "指定時は不足設定と推奨案を質問パケットとして返す"
        ),
    )
    parser.add_argument(
        "--base-ref",
        help=(
            "既存repoの今回差分だけを検査するbase ref。"
            "--intent push/open_pr/merge専用で、baseはHEADの祖先でなければならない"
        ),
    )
    parser.add_argument(
        "--consistency-base-ref",
        help=(
            "repo全体scanを狭めず、整合性のchange-sensitive検査だけに使うbase ref。"
            "releaseではHEADの祖先のtagも使用できる。"
            "plain scan と publish/release 向け。"
            f"未指定時は {DEFAULT_CONSISTENCY_BASE_REF} を試し、usable ならそれを使う"
        ),
    )
    parser.add_argument(
        "--identity-base-ref",
        help=(
            "release時の今回操作名義を比較するorigin remote ref。"
            "全履歴の名義分布は別の監査結果に残す。"
            "--expected-identityと併用し、HEADの祖先に限る"
        ),
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="コンソール補助: 検査オプションをTTYで選ぶ (本体は --intent のエージェント対話)",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="release準備としてREADME情報設計ゲートも自動実行する",
    )
    parser.add_argument(
        "--require-private-identity",
        action="store_true",
        help="明示private provider/policyを必須にする。未設定をtool_errorで拒否する（intent操作では常に必須）。",
    )
    parser.add_argument(
        "--expected-identity",
        help='Expected Git author and committer identity, for example "Example <dev@example.com>"',
    )
    parser.add_argument(
        "--audience",
        choices=[key for key, _ in AUDIENCE_CHOICES],
        default="unspecified",
        help="見せる相手。publish intent やメタデータに使う",
    )
    parser.add_argument(
        "--github-settings-profile",
        choices=["solo_public", "team_public", "high_risk_public"],
        default="solo_public",
        help=(
            "configure_settings intentで比較する運用profile。"
            "solo_public / team_public / high_risk_public"
        ),
    )
    parser.add_argument(
        "--human",
        action="store_true",
        help="人間/エージェント向け要約を stderr に出し、JSONは stdout に出す",
    )
    parser.add_argument(
        "--record-dismissal",
        metavar="PROPOSAL_ID",
        help=(
            "採用先 repo の .repo-preflight.json に dismiss を記録して終了。"
            "--repo と --dismissal-mode が必要"
        ),
    )
    parser.add_argument(
        "--dismissal-mode",
        choices=["forever", "7d", "30d", "90d"],
        help="--record-dismissal と併用。forever / 7d / 30d / 90d",
    )
    parser.add_argument(
        "--dismissal-reason",
        default="",
        help="dismiss 記録の理由 (任意)",
    )
    return parser


def resolve_options(
    args: argparse.Namespace,
    *,
    stdin_is_tty: bool,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    reviewed_secret_exceptions = getattr(args, "reviewed_secret_exceptions", None)
    allowed_identities = getattr(args, "allowed_identities", None)
    intent = getattr(args, "intent", None)
    base_ref = getattr(args, "base_ref", None)
    consistency_base_ref = getattr(args, "consistency_base_ref", None)
    identity_base_ref = getattr(args, "identity_base_ref", None)
    if base_ref and consistency_base_ref:
        raise SystemExit("error: --base-ref and --consistency-base-ref are exclusive")
    if base_ref and intent not in {"push", "open_pr", "merge"}:
        raise SystemExit("error: --base-ref requires --intent push, open_pr, or merge")
    # --consistency-base-ref は repo 全体 scan を狭めず、整合性の change-sensitive
    # 検査だけに scope を与える。target_diff 系 intent (push/open_pr/merge) は
    # 自前の --base-ref を持つので排他。plain scan (intent 無し) は repo 全体 scan
    # そのものなので許可する。未指定時は scan() 側で origin/main を既定候補にする。
    if consistency_base_ref and not (
        intent in {"publish", "release"} or intent is None
    ):
        raise SystemExit(
            "error: --consistency-base-ref requires publish/release intent or a plain scan"
        )
    if identity_base_ref and (
        intent != "release" or not args.expected_identity or base_ref
    ):
        raise SystemExit(
            "error: --identity-base-ref requires --intent release and --expected-identity"
        )
    # intent モードはエージェント対話が本体。TTYメニューは使わない
    want_console = bool(
        args.interactive or (args.repo is None and stdin_is_tty and not intent)
    )
    if want_console:
        options = collect_interactive_options(
            initial_repo=args.repo,
            initial_release=bool(args.release),
            initial_identity=args.expected_identity,
            input_fn=input_fn,
            output_fn=output_fn,
        )
        options.reviewed_secret_exceptions = reviewed_secret_exceptions
        options.allowed_identities = allowed_identities
        options.intent = intent
        options.base_ref = base_ref
        options.consistency_base_ref = consistency_base_ref
        options.identity_base_ref = identity_base_ref
        options.github_settings_profile = args.github_settings_profile
        return options

    gate = load_dialogue_gate()
    if intent == "configure_settings" and args.repo is None:
        raise SystemExit("error: --repo is required for --intent configure_settings")
    if intent and not gate.intent_needs_scan(intent) and args.repo is None:
        return ScanOptions(
            repo=None,
            release=bool(args.release),
            expected_identity=args.expected_identity,
            audience=args.audience,
            interactive=False,
            show_json=True,
            intent=intent,
            base_ref=base_ref,
            consistency_base_ref=consistency_base_ref,
            identity_base_ref=identity_base_ref,
            github_settings_profile=args.github_settings_profile,
            reviewed_secret_exceptions=reviewed_secret_exceptions,
            allowed_identities=allowed_identities,
        )
    if args.repo is None:
        raise SystemExit(
            "error: --repo is required "
            "(create_repo intent のみ省略可。コンソール補助は --interactive)"
        )
    return ScanOptions(
        repo=args.repo,
        release=bool(args.release),
        expected_identity=args.expected_identity,
        audience=args.audience,
        interactive=False,
        show_json=True,
        intent=intent,
        base_ref=base_ref,
        consistency_base_ref=consistency_base_ref,
        identity_base_ref=identity_base_ref,
        github_settings_profile=args.github_settings_profile,
        reviewed_secret_exceptions=reviewed_secret_exceptions,
        allowed_identities=allowed_identities,
    )


_MODULE_NAME_SAFE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,127}\Z")
_FRAME_BASENAME_UNSAFE = re.compile(r"[:/\\\r\n\x00]")


def unexpected_exception_issue(exc: BaseException) -> str:
    """例外の型名だけを返す。ModuleNotFoundError だけ安全な name と frame を足す。

    message と path は path や秘密を含み得るので記録しない。
    name は識別子だけ、frame は basename と行番号だけを残す。
    """
    issue = f"unexpected_exception:{type(exc).__name__}"
    if not isinstance(exc, ModuleNotFoundError):
        return issue
    name = getattr(exc, "name", None)
    if isinstance(name, str) and _MODULE_NAME_SAFE.fullmatch(name):
        issue += f":name={name}"
    tb = exc.__traceback__
    if tb is None:
        return issue
    frames = traceback.extract_tb(tb)
    if not frames:
        return issue
    last = frames[-1]
    base = os.path.basename(last.filename)
    lineno = last.lineno
    if (
        isinstance(base, str)
        and base
        and base not in {".", ".."}
        and len(base) <= 180
        and _FRAME_BASENAME_UNSAFE.search(base) is None
        and isinstance(lineno, int)
        and lineno > 0
    ):
        issue += f":at={base}:{lineno}"
    return issue


def report_exit_code(report: dict) -> int:
    if report["status"] == "pass":
        return 0
    if report["status"] == "tool_error":
        return 2
    return 1


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
