from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, TextIO

# impact_map 等の change-sensitive 整合性検査向け。README / OPERATIONS の最短
# コマンド（base 未指定）でも同じ remote base を使う。
DEFAULT_CONSISTENCY_BASE_REF = "origin/main"

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


def valid_initial_private_scope(
    expected,
    base_ref,
    consistency_base_ref,
    identity_base_ref,
    private_submission,
    release,
):
    return (
        isinstance(expected, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", expected)
        and expected.split("/")[1] not in {".", ".."}
        and not any(
            (
                base_ref is not None,
                consistency_base_ref is not None,
                identity_base_ref is not None,
                private_submission,
                release,
            )
        )
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
        private_submission: bool = False,
        consistency_base_ref: str | None = None,
        identity_base_ref: str | None = None,
        github_settings_profile: str = "solo_public",
        reviewed_secret_exceptions: Path | None = None,
        allowed_identities: Path | None = None,
        media_inspection: str = "strict",
        allow_expected_private_commit_identity: bool = False,
        initial_private_save: str | None = None,
    ) -> None:
        self.repo = repo
        self.release = release
        self.expected_identity = expected_identity
        self.audience = audience
        self.interactive = interactive
        self.show_json = show_json
        self.intent = intent
        self.base_ref = base_ref
        self.initial_private_save = initial_private_save
        self.private_submission = private_submission
        self.allow_expected_private_commit_identity = (
            allow_expected_private_commit_identity
        )
        self.media_inspection = media_inspection
        self.consistency_base_ref = consistency_base_ref
        self.identity_base_ref = identity_base_ref
        self.github_settings_profile = github_settings_profile
        self.reviewed_secret_exceptions = reviewed_secret_exceptions
        self.allowed_identities = allowed_identities


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
    format_boundary_text: Callable[[], str],
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


def build_parser(gate: ModuleType) -> argparse.ArgumentParser:
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
        "--initial-private-save",
        metavar="OWNER/REPO",
        help="空のprivate originへの初回pushを実地確認し、全refを検査する",
    )
    parser.add_argument(
        "--private-submission",
        action="store_true",
        help="live確認済みprivate originへ送るcommit済みobjectsだけを検査。--base-refとpush/open_pr/merge専用",
    )
    parser.add_argument(
        "--media-inspection",
        choices=("strict", "metadata"),
        default="strict",
        help="metadataは明示private送信専用。媒体の形式・metadata・先頭末尾・SHAを検査し、payload意味検査とは区別",
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
        "--allow-expected-private-commit-identity",
        action="store_true",
        help="live PRIVATE submissionの完全一致author/committer欄だけ期待名義を許可する。本文・秘密・path検査は維持する。",
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
    load_gate: Callable[[], ModuleType],
    format_boundary_text: Callable[[], str],
    stdin_is_tty: bool,
    input_fn: Callable[[str], str] = prompt_from_stdin,
    output_fn: Callable[[str], None] | None = None,
) -> ScanOptions:
    reviewed_secret_exceptions = getattr(args, "reviewed_secret_exceptions", None)
    allowed_identities = getattr(args, "allowed_identities", None)
    intent = getattr(args, "intent", None)
    base_ref = getattr(args, "base_ref", None)
    private_submission = getattr(args, "private_submission", False)
    media_inspection = getattr(args, "media_inspection", "strict")
    allow_private_identity = getattr(
        args, "allow_expected_private_commit_identity", False
    )
    if allow_private_identity and (
        not private_submission
        or not args.expected_identity
        or not base_ref
        or intent not in {"push", "open_pr", "merge"}
        or args.release
    ):
        raise SystemExit(
            "error: private commit identity requires explicit private submission and expected identity"
        )
    if media_inspection == "metadata" and (
        not private_submission
        or intent not in {"push", "open_pr", "merge"}
        or not base_ref
        or args.release
        or reviewed_secret_exceptions is not None
    ):
        raise SystemExit(
            "error: metadata inspection requires an explicit private committed submission"
        )
    if private_submission and (
        not base_ref or intent not in {"push", "open_pr", "merge"} or args.release
    ):
        raise SystemExit(
            "error: --private-submission requires --base-ref and push/open_pr/merge without --release"
        )
    consistency_base_ref = getattr(args, "consistency_base_ref", None)
    identity_base_ref = getattr(args, "identity_base_ref", None)
    initial_private_save = getattr(args, "initial_private_save", None)
    if initial_private_save is not None and (
        intent != "push"
        or not valid_initial_private_scope(
            initial_private_save,
            base_ref,
            consistency_base_ref,
            identity_base_ref,
            private_submission,
            args.release,
        )
    ):
        raise SystemExit("error: invalid_initial_private_save_scope")
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
            format_boundary_text=format_boundary_text,
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
        options.initial_private_save = initial_private_save
        options.private_submission = private_submission
        options.allow_expected_private_commit_identity = allow_private_identity
        options.media_inspection = media_inspection
        options.consistency_base_ref = consistency_base_ref
        options.identity_base_ref = identity_base_ref
        options.github_settings_profile = args.github_settings_profile
        return options

    gate = load_gate()
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
            private_submission=private_submission,
            initial_private_save=initial_private_save,
            allow_expected_private_commit_identity=allow_private_identity,
            media_inspection=media_inspection,
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
        private_submission=private_submission,
        initial_private_save=initial_private_save,
        allow_expected_private_commit_identity=allow_private_identity,
        media_inspection=media_inspection,
        consistency_base_ref=consistency_base_ref,
        identity_base_ref=identity_base_ref,
        github_settings_profile=args.github_settings_profile,
        reviewed_secret_exceptions=reviewed_secret_exceptions,
        allowed_identities=allowed_identities,
    )
