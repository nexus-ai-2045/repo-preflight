"""公開側へ持ち主の個人名義を出さないための検査。

照合する個人名義 (本名・メール・OSのアカウント名・端末名) は、人に書かせず
実行中の環境から毎回自動で集める。集めた値はメモリ上だけで使い、標準出力・
標準エラー・ファイルのどこにも書かない。報告は「どの種類の値が・どこに」
だけを出し、値そのものはSHA-256の先頭だけで示す。

サブコマンド:
  config  今のrepoで実際に使われるコミット名義が公開用の名義かを確かめる。
  check   push範囲のコミット名義・メッセージ・追加行・ファイル名を照合する。
  exec    集めた値を PUBLIC_IDENTITY_DENYLIST_EXTRA に入れて別の検査を起動し、
          その出力に値が出たら伏せ字に置き換えて流す。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import socket
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ZERO_SHA = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
EXTRA_DENYLIST_ENV = "PUBLIC_IDENTITY_DENYLIST_EXTRA"
# 任意。存在する時だけ追加で読む。無くても空でも検査は止めない。
OPTIONAL_PRIVATE_FILE = Path("~/.config/repo-preflight/private-identities.txt")
REDACTED = "[個人名義]"
REMOTE_QUERY_TIMEOUT_SECONDS = 15

# 公開してよいコミット名義のメール。GitHubのnoreplyとbotだけを許す。
DEFAULT_ALLOWED_EMAIL = re.compile(
    r"(?:\d+\+)?[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?"
    r"@users\.noreply\.github\.com|noreply@github\.com",
    re.IGNORECASE,
)

# 環境から拾っても個人を指さない値。照合すると誤検知だけが増える。
GENERIC_VALUES = frozenset(
    {
        "admin",
        "administrator",
        "build",
        "builder",
        "claude",
        "codespace",
        "codespaces",
        "default",
        "dev",
        "developer",
        "docker",
        "github",
        "guest",
        "home",
        "local",
        "localdomain",
        "localhost",
        "mac",
        "macbook",
        "nobody",
        "node",
        "noreply",
        "owner",
        "root",
        "runner",
        "test",
        "ubuntu",
        "user",
        "users",
        "vagrant",
        "vscode",
        "worker",
    }
)
# 端末名から名前らしい部分だけを残すために捨てる語。
HOSTNAME_NOISE = frozenset(
    {
        "air",
        "imac",
        "iphone",
        "ipad",
        "lan",
        "macbook",
        "macbookair",
        "macbookpro",
        "macmini",
        "macpro",
        "mbp",
        "mini",
        "pc",
        "pro",
        "studio",
        "the",
        "desktop",
        "laptop",
        "home",
        "local",
    }
)
MIN_TOKEN_LENGTH = 3
# noreply のアドレスは個人を指さない (例: 共同作成者のtrailer)。
NOREPLY_PATTERN = re.compile(r"(?:^|[.+_-])no-?reply(?:$|@|[.+_-])")


@dataclass(frozen=True)
class Identity:
    value: str
    source: str

    @property
    def label(self) -> str:
        digest = hashlib.sha256(self.value.encode("utf-8")).hexdigest()[:8]
        return f"{self.source}#{digest}"


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _git_config(key: str, *scope: str, cwd: Path | None = None) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "config", *scope, "--get-all", key],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _command_output(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _full_name_from_passwd() -> str:
    try:
        import pwd

        gecos = pwd.getpwuid(os.getuid()).pw_gecos
    except (ImportError, KeyError, OSError):
        return ""
    return gecos.split(",")[0].strip()


def _hostname_tokens(name: str) -> list[str]:
    tokens = []
    for raw in re.split(r"[^0-9A-Za-z぀-ヿ一-鿿]+", name):
        token = raw
        # "Taro's MacBook" は "Taros-MacBook" になるので所有格の s を外した形も持つ。
        if token.lower().endswith("s") and len(token) > MIN_TOKEN_LENGTH + 1:
            tokens.append(token[:-1])
        tokens.append(token)
    return [t for t in tokens if t.casefold() not in HOSTNAME_NOISE]


def _email_parts(email: str) -> list[str]:
    local = email.split("@", 1)[0]
    return [email, local]


def _is_allowed_public(
    value: str, allowed_email: re.Pattern[str], public_names: set[str]
) -> bool:
    folded = _normalize(value)
    public_tokens = {t for n in public_names for t in re.split(r"[^0-9a-z]+", n) if t}
    if folded in public_names or folded in public_tokens:
        return True
    return bool(allowed_email.fullmatch(value))


# サービスやAIが付けるコミット名義。持ち主の個人名義ではない。
SERVICE_EMAIL_DOMAINS = frozenset(
    {"anthropic.com", "cursor.com", "openai.com", "github.com", "google.com"}
)


def _history_personal_values(repo: Path, allowed_email: re.Pattern[str]) -> list[str]:
    """repo の履歴で、公開用でもサービスでもないメールを使ったコミットの名義を拾う。

    以前に私用の名義で作ったコミットがあれば、その名前とメールも照合に加える。
    """
    try:
        log = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=",
                "-C",
                str(repo),
                "log",
                "--all",
                "--format=%an%x00%ae%x00%cn%x00%ce",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout
    except OSError:
        return []
    values: list[str] = []
    for line in set(log.splitlines()):
        fields = line.split("\x00")
        for name, email in zip(fields[0::2], fields[1::2]):
            email = email.strip()
            domain = email.rsplit("@", 1)[-1].casefold()
            if (
                not email
                or allowed_email.fullmatch(email)
                or domain in SERVICE_EMAIL_DOMAINS
            ):
                continue
            values += [name, *name.split(), *_email_parts(email)]
    return values


def collect_identities(
    *,
    allowed_email: re.Pattern[str] = DEFAULT_ALLOWED_EMAIL,
    public_names: set[str] | None = None,
    environ: dict[str, str] | None = None,
    sources: dict[str, list[str]] | None = None,
    repo: Path | None = None,
) -> list[Identity]:
    """環境から個人名義の候補を集める。値は呼び出し元へ返すだけで外へ出さない。

    sources を渡すとそれだけを使う (テスト用)。
    """
    env = os.environ if environ is None else environ
    public = {_normalize(n) for n in (public_names or set())}
    if sources is None:
        sources = {}
        login = []
        for getter in (getpass.getuser,):
            try:
                login.append(getter())
            except (OSError, KeyError):
                pass
        login += [env.get("USER", ""), env.get("LOGNAME", ""), env.get("USERNAME", "")]
        sources["os_account"] = login
        sources["home_dir"] = [Path(env.get("HOME", str(Path.home()))).name]
        full_name = _full_name_from_passwd() or _command_output(["id", "-F"])
        sources["full_name"] = [full_name, *full_name.split()]
        names = _git_config("user.name", "--global") + _git_config(
            "user.name", "--system"
        )
        sources["git_global_name"] = names + [part for n in names for part in n.split()]
        emails = _git_config("user.email", "--global") + _git_config(
            "user.email", "--system"
        )
        # Apply the existing public-email policy before deriving private aliases.
        # Otherwise an allowed numeric noreply address creates its own localpart
        # deny candidate and rejects the same registered public commit identity.
        sources["git_global_email"] = [
            part
            for email in emails
            if not allowed_email.fullmatch(email.strip())
            for part in _email_parts(email)
        ]
        hosts = [
            socket.gethostname(),
            _command_output(["scutil", "--get", "ComputerName"]),
            _command_output(["scutil", "--get", "LocalHostName"]),
        ]
        sources["host_name"] = [t for h in hosts if h for t in _hostname_tokens(h)]
        private_file = OPTIONAL_PRIVATE_FILE.expanduser()
        if private_file.is_file():
            try:
                sources["private_file"] = [
                    line.strip()
                    for line in private_file.read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ]
            except (OSError, UnicodeDecodeError):
                pass
        if repo is not None:
            sources["repo_history"] = _history_personal_values(repo, allowed_email)

    seen: set[str] = set()
    identities: list[Identity] = []
    for source, values in sources.items():
        for value in values:
            value = (value or "").strip()
            folded = _normalize(value)
            if not value or folded in seen:
                continue
            ascii_only = value.isascii()
            if ascii_only and len(value) < MIN_TOKEN_LENGTH:
                continue
            if folded in GENERIC_VALUES or NOREPLY_PATTERN.search(folded):
                continue
            if _is_allowed_public(value, allowed_email, public):
                continue
            seen.add(folded)
            identities.append(Identity(value=value, source=source))
    return identities


def _matcher(identity: Identity) -> re.Pattern[str]:
    escaped = re.escape(_normalize(identity.value))
    if identity.value.isascii():
        # 英数字の値は単語の途中 (例: "pro" が "project") では当てない。
        return re.compile(rf"(?<![0-9a-z]){escaped}(?![0-9a-z])")
    return re.compile(escaped)


def find_hits(text: str, identities: list[Identity]) -> list[Identity]:
    folded = _normalize(text)
    return [i for i in identities if _matcher(i).search(folded)]


def redact(text: str, identities: list[Identity]) -> str:
    # 長い値から伏せて、部分一致で一部だけ残るのを防ぐ。
    for identity in sorted(identities, key=lambda i: len(i.value), reverse=True):
        text = re.sub(re.escape(identity.value), REDACTED, text, flags=re.IGNORECASE)
        normalized = unicodedata.normalize("NFKC", identity.value)
        if normalized != identity.value:
            text = re.sub(re.escape(normalized), REDACTED, text, flags=re.IGNORECASE)
    return text


def _git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.fsmonitor=", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} が失敗しました (exit {completed.returncode})"
        )
    return completed.stdout


def effective_identity_problems(
    repo: Path, allowed_email: re.Pattern[str], identities: list[Identity]
) -> list[str]:
    problems = []
    for role, var in (
        ("author", "GIT_AUTHOR_IDENT"),
        ("committer", "GIT_COMMITTER_IDENT"),
    ):
        ident = _git(repo, "var", var, check=False).strip()
        match = re.match(r"(?P<name>.*) <(?P<email>[^>]*)>", ident)
        if not match:
            problems.append(f"{role}: 名義を読めません")
            continue
        if not allowed_email.fullmatch(match.group("email")):
            problems.append(
                f"{role}: メールが公開用 (GitHub の noreply) ではありません"
            )
        for hit in find_hits(match.group("name"), identities):
            problems.append(f"{role}: 名前に個人名義 {hit.label} が入っています")
    return problems


def _rev_list(repo: Path, ranges: list[str]) -> list[str]:
    out = _git(repo, "rev-list", "--reverse", *ranges)
    return [line for line in out.splitlines() if line]


def _advertised_remote_commits(repo: Path, remote: str) -> list[str]:
    """実送信先が現在advertiseする、ローカルで読めるcommitだけを返す。"""
    try:
        if remote in _git(repo, "remote").splitlines():
            destinations = _git(
                repo, "remote", "get-url", "--push", "--all", "--", remote
            ).splitlines()
        else:
            # URL直指定ではpush用resolverを使えないため、pushInsteadOfが
            # 一つでもあれば拒否する。自前で書換え規則を再実装しない。
            push_rewrites = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=",
                    "-C",
                    str(repo),
                    "config",
                    "--name-only",
                    "--get-regexp",
                    r"^url\..*\.pushinsteadof$",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=REMOTE_QUERY_TIMEOUT_SECONDS,
            )
            if push_rewrites.returncode == 0:
                raise RuntimeError(
                    "送信先の履歴照会先を特定できません "
                    "(URL直指定とpushInsteadOf設定の併用)"
                )
            if push_rewrites.returncode != 1:
                raise RuntimeError("送信先のURL書換え設定を確認できませんでした")
            destinations = _git(
                repo, "ls-remote", "--get-url", "--", remote
            ).splitlines()
        if len(destinations) != 1:
            raise RuntimeError("送信先が一意でないため検査範囲を決められません")
        destination = destinations[0]
        # ls-remoteはfetch用のinsteadOfを再適用する。別の宛先へ変わるなら
        # その履歴を除外根拠にせず停止する（--get-urlは通信しない）。
        lookup_destinations = _git(
            repo, "ls-remote", "--get-url", "--", destination
        ).splitlines()
        if lookup_destinations != [destination]:
            raise RuntimeError("送信先の履歴照会先が再書換えされるため停止します")
        advertised = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=",
                "-C",
                str(repo),
                "ls-remote",
                "--heads",
                "--refs",
                "--",
                destination,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=REMOTE_QUERY_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("送信先の履歴照会が時間切れになりました") from None
    except OSError:
        raise RuntimeError("送信先の履歴を照会できませんでした") from None
    if advertised.returncode != 0:
        # URLやGitのstderrには認証情報・個人のパスが入り得るため出力しない。
        raise RuntimeError("送信先の履歴を照会できませんでした")
    commits = []
    for line in advertised.stdout.splitlines():
        fields = line.split()
        if (
            len(fields) != 2
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[0])
            or not fields[1].startswith("refs/heads/")
        ):
            raise RuntimeError("送信先の履歴照会結果を確認できませんでした")
        sha = fields[0]
        # 未取得の先端は除外しない。必要なら既存履歴も含めて安全側へ検査する。
        if _git(repo, "cat-file", "-t", sha, check=False).strip() == "commit":
            commits.append(sha)
    return list(dict.fromkeys(commits))


def push_ranges_from_stdin(repo: Path, remote: str, stdin_text: str) -> list[list[str]]:
    """pre-push の stdin から、まだ remote に無いコミットの範囲を作る。"""
    ranges = []
    remote_commits: list[str] | None = None
    for line in stdin_text.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = fields
        if local_sha == ZERO_SHA:
            continue
        if remote_sha != ZERO_SHA:
            ranges.append([f"{remote_sha}..{local_sha}"])
        else:
            if remote_commits is None:
                remote_commits = _advertised_remote_commits(repo, remote)
            ranges.append(
                [local_sha, "--not", *remote_commits] if remote_commits else [local_sha]
            )
    return ranges


def check_commits(
    repo: Path,
    commits: list[str],
    identities: list[Identity],
    allowed_email: re.Pattern[str],
) -> list[str]:
    violations = []
    for sha in commits:
        short = sha[:12]
        meta = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%B", sha)
        an, ae, cn, ce, body = (meta.split("\x00", 4) + [""] * 5)[:5]
        for role, name, email in (("author", an, ae), ("committer", cn, ce)):
            if not allowed_email.fullmatch(email.strip()):
                violations.append(
                    f"{short} {role}: メールが公開用 (GitHub の noreply) ではありません"
                )
            for hit in find_hits(name, identities) + find_hits(email, identities):
                violations.append(
                    f"{short} {role}: 個人名義 {hit.label} が入っています"
                )
        for hit in find_hits(body, identities):
            violations.append(
                f"{short} メッセージ: 個人名義 {hit.label} が入っています"
            )
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
        base = parents[0] if parents else EMPTY_TREE
        names = _git(repo, "diff", "--name-only", "--no-renames", base, sha)
        for path in names.splitlines():
            for hit in find_hits(path, identities):
                violations.append(
                    f"{short} ファイル名 {redact(path, identities)}: 個人名義 {hit.label}"
                )
        diff = _git(
            repo, "diff", "--no-color", "--no-ext-diff", "-U0", "--text", base, sha
        )
        current = "?"
        for line in diff.splitlines():
            if line.startswith("+++ "):
                current = redact(line[4:].removeprefix("b/"), identities)
            elif line.startswith("+") and not line.startswith("+++"):
                for hit in find_hits(line[1:], identities):
                    violations.append(
                        f"{short} {current}: 個人名義 {hit.label} が追加されています"
                    )
    return list(dict.fromkeys(violations))


def _allowed_email(args: argparse.Namespace) -> re.Pattern[str]:
    if args.allowed_email_regex:
        return re.compile(args.allowed_email_regex, re.IGNORECASE)
    return DEFAULT_ALLOWED_EMAIL


def _public_names(args: argparse.Namespace) -> set[str]:
    return set(args.public_name or [])


def _report(violations: list[str]) -> int:
    if not violations:
        print(
            "[local_identity_guard] OK: 個人名義は見つかりませんでした。",
            file=sys.stderr,
        )
        return 0
    print(
        "[local_identity_guard] 停止: 公開側に個人名義が出ます。値は表示しません。",
        file=sys.stderr,
    )
    for line in violations:
        print(f"  - {line}", file=sys.stderr)
    print(
        "  対処: コミット名義を GitHub の noreply にし、該当箇所から個人名義を除いてください。"
        " 値をチャットやファイルに書いて確認しないでください。",
        file=sys.stderr,
    )
    return 1


def cmd_config(args: argparse.Namespace) -> int:
    allowed = _allowed_email(args)
    identities = collect_identities(
        allowed_email=allowed, public_names=_public_names(args), repo=Path(args.repo)
    )
    return _report(effective_identity_problems(Path(args.repo), allowed, identities))


def cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    allowed = _allowed_email(args)
    identities = collect_identities(
        allowed_email=allowed, public_names=_public_names(args), repo=Path(args.repo)
    )
    if args.pre_push:
        ranges = push_ranges_from_stdin(repo, args.remote, sys.stdin.read())
    else:
        ranges = [args.range.split()]
    commits: list[str] = []
    for spec in ranges:
        commits += _rev_list(repo, spec)
    violations = check_commits(repo, list(dict.fromkeys(commits)), identities, allowed)
    violations += effective_identity_problems(repo, allowed, identities)
    return _report(violations)


def cmd_exec(args: argparse.Namespace) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print(
            "[local_identity_guard] exec: 起動する検査を -- の後に指定してください。",
            file=sys.stderr,
        )
        return 2
    identities = collect_identities(
        allowed_email=_allowed_email(args),
        public_names=_public_names(args),
        repo=Path(args.repo),
    )
    env = dict(os.environ)
    values = "\n".join(i.value for i in identities)
    env[EXTRA_DENYLIST_ENV] = "\n".join(
        v for v in (env.get(EXTRA_DENYLIST_ENV, ""), values) if v
    )
    # 親から継承した照合値も子へ渡すため、出力の伏せ字対象から落とさない。
    redaction_identities = [
        Identity(value=value, source="forwarded_denylist")
        for value in dict.fromkeys(env[EXTRA_DENYLIST_ENV].splitlines())
        if value.strip()
    ]
    process = subprocess.Popen(
        command,
        env=env,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    # 起動した検査が違反箇所として値を表示しても、ここで伏せる。
    for line in process.stdout:
        sys.stderr.write(redact(line, redaction_identities))
    return process.wait()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument(
        "--allowed-email-regex",
        help="公開してよいコミットのメール (既定は GitHub の noreply と bot)",
    )
    parser.add_argument(
        "--public-name",
        action="append",
        help="公開用の名義 (例: 組織のアカウント名)。照合から外す。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config").set_defaults(func=cmd_config)
    check = sub.add_parser("check")
    check.add_argument(
        "--range", default="HEAD", help="rev-list に渡す範囲 (空白区切り可)"
    )
    check.add_argument(
        "--pre-push", action="store_true", help="pre-push の stdin から範囲を作る"
    )
    check.add_argument("--remote", default="origin")
    check.set_defaults(func=cmd_check)
    exe = sub.add_parser("exec")
    exe.add_argument("command", nargs=argparse.REMAINDER)
    exe.set_defaults(func=cmd_exec)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(
            f"[local_identity_guard] 検査できないため止めます: {exc}", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
