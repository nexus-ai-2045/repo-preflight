#!/usr/bin/env python3
"""名義・共同作者行・本文の必須検査。

個人の型はこのファイルに書かない。非公開の設定から読む:
  1. 環境変数 PREFLIGHT_PRIVATE_PATTERNS_FILE が指すJSON
  2. 環境変数 PREFLIGHT_PRIVATE_PATTERNS（JSON文字列。CIのシークレット用）
設定が無いときは「設定なし」で失敗する（任意にしない）。
"""

from __future__ import annotations
import functools, hashlib, html, json, os, re, shutil, subprocess, sys, unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
import importlib.util

# path文法の正本（identity_normalization.py）を同梱fileから読む。別の文法を持たない。
_PATH_SPEC = importlib.util.spec_from_file_location(
    "_identity_gate_path_grammar",
    Path(__file__).resolve().with_name("identity_normalization.py"),
)
_PATH_GRAMMAR = importlib.util.module_from_spec(_PATH_SPEC)
_PATH_SPEC.loader.exec_module(_PATH_GRAMMAR)

# 行境界は LF のみ。\s は制御文字を吸収し、検査前に攻撃値を消してしまう。
TRAILER_RE = re.compile(
    r"^[ \t]*(co-authored-by|signed-off-by)[ \t]*:([^\n]*)$", re.I | re.M
)
# 公開の入口では「〜-by:」の署名行をすべて見る（Assisted-by・Reviewed-by など。敵対的レビュー M6）
PUBLIC_TRAILER_RE = re.compile(
    r"^[ \t]*([a-z][a-z0-9]*(?:-[a-z0-9]+)*-by)[ \t]*:([^\n]*)$", re.I | re.M
)
# 公開してよい汎用の型（個人名は入れない）
USER_DIRECTORY_COMPONENT = "Users"
LINUX_DIRECTORY_COMPONENT = "home"
GENERIC_PATH_RES = [
    re.compile(r"[A-Za-z]:[/\\]Us" r"ers[/\\][^/\\\s]+"),
    re.compile(rf"/{USER_DIRECTORY_COMPONENT}/[^/\s]+"),
    re.compile(rf"/{LINUX_DIRECTORY_COMPONENT}/[^/\s]+"),
    re.compile(r"~[A-Za-z0-9_.-]+/"),  # ホームの省略記法
]
# 開発道具が本文へ付けるセッション行とセッションURL。公開してよい汎用の型で、
# 個人の型（非公開設定）とは別に常に止める。値は出さず種類だけ返す。
# readiness_scan.py の claude_session 規則と同じ型にそろえる。
# 敵対的レビュー H1: 行頭に限らず、箇条書き・強調・空白区切り・全角コロン（NFKC で半角へ）も見る。
# レビュー3C 高7: 「:」の後ろが ID の形（数字を含む英数字の塊）のときだけ。英語の文（Start a Claude session: run …）は通す。
SESSION_TRACE_RES = (
    re.compile(
        # レビュー3C 中以下: key = value と JSON のキー（"claude_session_id": …・claudeSessionId）も見る
        r"(?:claude|codex|cursor|chatgpt|copilot)[\s_*-]*session(?:[\s_-]*id)?[\s*\"']*[:=]"
        r"[\s*`'\"]*(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{4,}",
        re.I,
    ),
    # Claude の作業 URL。レビュー3C 高2: claude.ai と claude.com の project・chat・share・code を見る。
    # ファイル名に写した形（/ を _ に置き換えた名前。高1）も同じ型で止める。
    re.compile(
        r"claude[.](?:ai|com)[/_](?:code[/_](?:session_|projects?[/_])"
        r"|(?:projects?|chat|share|code)/[A-Za-z0-9_-])",
        re.I,
    ),
    re.compile(
        r"(?:chatgpt[.]com|chat[.]openai[.]com)/(?:codex/tasks|c|share|g)/", re.I
    ),
    re.compile(r"cursor[.](?:com|sh)/(?:agents|background-agent|bc)[/?#]", re.I),
)

# 伏せた書き方（[at]・(dot) など）と、文字列の連結（"…" + "…"）
_AT_FORM_RE = re.compile(r"\s*[\[({<]\s*at\s*[\])}>]\s*", re.I)
# レビュー3C 中以下: 括弧で囲んだ点（[.]・(.)・{.}）も伏せた点として戻す
_DOT_FORM_RE = re.compile(r"\s*[\[({<]\s*(?:dot|[.])\s*[\])}>]\s*", re.I)
# レビュー3C 中以下: + の無い隣り合う文字列（"…" "…"）も連結として見る
_CONCAT_RE = re.compile(r"[\"']\s*\+?\s*[\"']")
_JSON_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
# レビュー3C 中以下: 言語の文字列エスケープ（\x2f・8進の \057）
_BYTE_ESCAPE_RE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|([0-7]{3}))")
# レビュー3C 中以下: ゼロ幅の文字と、ハイフンに似た文字
_ZERO_WIDTH_RE = re.compile("[\u00ad\u200b-\u200f\u2060\ufeff]")
_HYPHEN_LIKE = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"})


def text_variants(text: str) -> list[str]:
    """検査に使う写し（元の値・URL を何重にでも戻した値・JSON の \\u・HTML 実体・NFKC・伏せ字・連結）。

    判定のためだけに作り、外へは出さない。敵対的レビュー H1〜H3。
    変わり得ない写しは作らない（`%` が無ければ URL を戻さない、ASCII だけなら NFKC を掛けない など）。
    同じ本文の写しは直近のものを使い回す（形の判定とメールの判定で二度作らない）。
    """
    return list(_text_variants(text))


@functools.lru_cache(maxsize=4)
def _text_variants(text: str) -> tuple[str, ...]:
    out = [text]
    current = text
    for _ in range(4):
        if "%" not in current:
            break
        decoded = unquote(current)
        if decoded == current:
            break
        out.append(decoded)
        current = decoded
    for value in list(out):
        if "\\" not in value and "&" not in value:
            continue
        unescaped = _JSON_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)
        unescaped = _BYTE_ESCAPE_RE.sub(
            lambda m: chr(int(m.group(1), 16) if m.group(1) else int(m.group(2), 8)),
            unescaped,
        )
        unescaped = html.unescape(unescaped.replace("\\/", "/"))
        if unescaped != value:
            out.append(unescaped)
            if "%" in unescaped:
                again = unquote(unescaped)
                if again != unescaped:
                    out.append(again)
    for value in list(out):
        if value.isascii():
            normal = value
        else:
            normal = _ZERO_WIDTH_RE.sub("", value).translate(_HYPHEN_LIKE)
            normal = unicodedata.normalize("NFKC", normal)
        normal = _DOT_FORM_RE.sub(".", _AT_FORM_RE.sub("@", normal))
        if '"' in normal or "'" in normal:
            normal = _CONCAT_RE.sub("", normal)
        out.append(normal)
    return tuple(dict.fromkeys(out))


def session_trace_found(text: str, variants: list[str] | None = None) -> bool:
    """セッション行・URLの有無だけ返す。符号化・全角・強調の写しも見る。"""
    return any(
        r.search(candidate)
        for candidate in (variants or text_variants(text))
        for r in SESSION_TRACE_RES
    )


# 誰のものかに関係なく、形だけで止めるもの（非公開の照合値は要らない）。
# 個人のメールドメイン。会社・組織のドメインや noreply は対象外。
PERSONAL_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.jp",
        "ymail.com",
        "outlook.com",
        "outlook.jp",
        "hotmail.com",
        "hotmail.co.jp",
        "live.com",
        "live.jp",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "docomo.ne.jp",
        "ezweb.ne.jp",
        "au.com",
        "softbank.ne.jp",
        "i.softbank.jp",
        "nifty.com",
    }
)
# 国や地域で末尾が変わる個人向けの大手（gmail.co.uk・yahoo.de など）と、短い個人向けドメイン
PERSONAL_MAIL_FAMILY_RE = re.compile(
    r"^(?:gmail|googlemail|yahoo|ymail|rocketmail|outlook|hotmail|live|msn|icloud|me|mac|aol|"
    r"proton|protonmail|pm|gmx|web|yandex|mail|inbox|qq|foxmail|163|126|yeah|sina|sohu|naver|"
    r"hanmail|daum|fastmail|tutanota|tuta|tutamail|zoho|zohomail|hey|duck|mailbox|posteo|"
    r"libero|orange|free|laposte|t-online|seznam|wp|o2|rediffmail|rambler|ezweb|docomo|"
    r"softbank|nifty|biglobe|ocn|so-net|excite|livedoor)"
    r"[.](?:com|net|org|me|ne|co|or|jp|uk|de|fr|it|es|ru|cn|kr|in|br|ca|au|nl|be|ch|at|pl|cz|"
    r"se|no|dk|fi|ie|nz|mx|ar|tw|hk|sg|io|ai|info|biz|eu)(?:[.][a-z]{2})?$",
    re.I,
)


def personal_mail_domain(domain: str) -> bool:
    domain = domain.casefold().rstrip(".")
    return domain in PERSONAL_MAIL_DOMAINS or bool(
        PERSONAL_MAIL_FAMILY_RE.match(domain)
    )


# レビュー3C 高5: ドメインの末尾は英字2文字以上（@v4.1.0・lodash@4.17.21・x@W.T を外す）。
# 画像の倍率（icon@2x.png）もメールとして見ない。
MAIL_RE = re.compile(
    r"[A-Za-z0-9._%+-]+@(?!\d+x[.])"
    r"([A-Za-z0-9-]+(?:[.][A-Za-z0-9-]+)*[.][A-Za-z]{2,})(?![A-Za-z0-9-])"
)
# 鍵の形。readiness_scan.py の SECRET_PATTERNS（claude_session 以外）と同じ型。
KEY_SHAPE_RES = (
    # レビュー3C 高6: 単語の途中（risk-・desk-）では当てず、本体に数字を1つ以上求める
    re.compile(r"(?<![A-Za-z0-9])sk-(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY"),
    # レビュー3C 中以下: Google・GitLab・Hugging Face・Stripe・xAI の鍵の形
    re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?<![A-Za-z0-9])glpat-[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"),
    re.compile(r"(?<![A-Za-z0-9])[rs]k_live_[0-9A-Za-z]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])xai-[A-Za-z0-9]{40,}"),
)
# 実ホームパスの例外にしてよいのは、個人でない固定の利用者名だけ。
# GitHub Actions のホスト型ランナーの利用者名は runner で固定。
# レビュー3C 高8 の提案（2026-10-24 15:29 人承認）: 例示とコンテナで決まって使う利用者名
# （例示の user、devcontainer の vscode、docker の node）も外す。macOS の共有フォルダは
# _path_user_exempt で大文字の Shared だけ外す。
DEFAULT_ALLOWED_HOME_USERS = frozenset({"runner", "user", "vscode", "node"})
_MAC_SHARED_USER = "Shared"


def _home_user(match: re.Match) -> str:
    value = match.group()
    tail = re.split(r"[/\\]+", value.rstrip("/\\"))[-1]
    return tail.strip("\"'")


# 敵対的レビュー H3: 大文字小文字を問わない形と WSL のドライブ配下のユーザー置き場。
# 前が英数字・点・ハイフン・スラッシュなら URL の一部（api.example.com/users/... など）として見ない。
# レビュー3C 重大1: 前を見る条件は ASCII だけ（かな漢字の直後のパスも止める）。
_ASCII_PATH_BEFORE = r"(?<![A-Za-z0-9_./\\-])"
_PATH_USER = r"(?P<user>[^/\\\s'\"]+)"
EXTRA_PATH_RES = (
    re.compile(_ASCII_PATH_BEFORE + r"/us" r"ers/" + _PATH_USER, re.I),
    # 大文字の /Users/ は macOS のホームだけなので、点・下線の直後（1./・_/）も止める
    re.compile(r"(?<![A-Za-z0-9])/Us" r"ers/" + _PATH_USER),
    # 高4: 逆斜線で区切ったホームも見る
    re.compile(_ASCII_PATH_BEFORE + r"[/\\]+ho" r"me[/\\]+" + _PATH_USER, re.I),
    re.compile(r"/mnt/[a-z]/us" r"ers/" + _PATH_USER, re.I),
    # レビュー3C 中以下: /var・/export の下のホーム、外付けの macOS、共有フォルダの URL
    re.compile(r"(?<![A-Za-z0-9_.-])/(?:var|export)/ho" r"me/" + _PATH_USER),
    re.compile(r"(?<![A-Za-z0-9_.-])/Volumes/[^/\s]+/Us" r"ers/" + _PATH_USER),
    re.compile(
        r"(?:smb|afp|nfs|cifs)://[^/\s]+/(?:us" r"ers|ho" r"me)/" + _PATH_USER, re.I
    ),
    re.compile(r"(?<![a-z])[a-z]:[/\\]+us" r"ers[/\\]+" + _PATH_USER, re.I),
    # 高3: 道具のリンク（vscode://file/…・cursor://file/…・file://localhost/…）
    re.compile(
        r"(?:[a-z][a-z0-9+.-]*://file|file://localhost)/+(?:us" r"ers|ho" r"me)/"
        + _PATH_USER,
        re.I,
    ),
    # 高4: WSL の UNC パス（wsl$ と wsl.localhost の下のホーム）
    re.compile(
        r"[/\\]{2}wsl(?:\$|[.]localhost)[/\\]+[^/\\\s]+[/\\]+ho" r"me[/\\]+" + _PATH_USER,
        re.I,
    ),
    # 高3: 道具が / を - に置き換えて符号化したホーム（Claude のプロジェクト置き場の名前など）
    re.compile(
        r"(?<![^/\\\s'\"=:(\[])(?:[A-Za-z]-)?-Us" r"ers-(?P<user>[A-Za-z0-9._]+)"
        r"(?=[-/\\\s'\"]|$)",
        re.M,
    ),
    re.compile(r"(?<![^/\\\s'\"=:(\[])-ho" r"me-(?P<user>[A-Za-z0-9._]+)-"),
)
# 高7・高8: 例示の記号（<user>・$USER・${USER}・{id}・:id）で始まる利用者名と、
# HTTP の動詞の直後（GET のすぐ後ろの feed などの経路）は実ホームとして見ない。
_PLACEHOLDER_USER_START = ("<", "$", "{", ":")
_HTTP_VERB_BEFORE = re.compile(r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[ \t]+$")


def _path_user_exempt(candidate: str, start: int, user: str, allowed) -> bool:
    user = user.strip("\"'")
    if (
        user.casefold() in allowed
        or user == _MAC_SHARED_USER
        or user.startswith(_PLACEHOLDER_USER_START)
    ):
        return True
    return _HTTP_VERB_BEFORE.search(candidate[max(0, start - 10) : start]) is not None


def shape_findings(text: str, allow_home_users=DEFAULT_ALLOWED_HOME_USERS) -> list[str]:
    """形だけで判定し、種類だけ返す。値は返さない。符号化・全角・連結の写しも見る。"""
    kinds: list[str] = []
    allowed = {u.casefold() for u in allow_home_users}
    candidates = text_variants(text)
    if any(
        not _path_user_exempt(c, m.start(), _home_user(m), allowed)
        for c in candidates
        for r in _PATH_GRAMMAR.PATH_PATTERNS
        for m in r.finditer(c)
    ) or any(
        not _path_user_exempt(c, m.start(), m.group("user"), allowed)
        for c in candidates
        for r in EXTRA_PATH_RES
        for m in r.finditer(c)
    ):
        kinds.append("実ホームパスの形")
    if any(
        personal_mail_domain(m.group(1))
        for c in candidates
        for m in MAIL_RE.finditer(c)
    ):
        kinds.append("個人メールの形")
    if session_trace_found(text, candidates):
        kinds.append("セッション行あり")
    if any(r.search(c) for c in candidates for r in KEY_SHAPE_RES):
        kinds.append("鍵の形")
    return kinds


PUBLIC_POLICY_SCHEMAS = (
    "public-commit-identities/v1",
    "repo-preflight.identity-policy/v1",
)
REPO_POLICY_PATH = ".repo-preflight/identity-policy.json"
DEFAULT_PUBLIC_POLICY = (
    Path(__file__).resolve().parent.parent
    / "references"
    / "public-commit-identities.json"
)


@dataclass(frozen=True)
class PublicPolicy:
    """通してよい名義の一覧（完全一致）と、ホームパスの固定利用者名の例外。"""

    identities: frozenset
    allow_home_users: frozenset = DEFAULT_ALLOWED_HOME_USERS
    # 記録者（committer）にだけ許す名義（GitHub の Web 操作の記録者など。作者・署名行には使えない）
    committer_only: frozenset = frozenset()

    def __contains__(self, identity: str) -> bool:
        return identity in self.identities

    def allows(self, identity: str, role: str) -> bool:
        if identity in self.identities:
            return True
        return role == "コミッター" and identity in self.committer_only

    @property
    def emails(self) -> frozenset:
        """名簿にあるメールだけ（小文字）。本文に出てよいのはこれと予約ドメインだけ。"""
        return frozenset(
            v.rsplit("<", 1)[1].rstrip(">").casefold()
            for v in self.identities | self.committer_only
        )


def parse_public_policy(raw: str) -> PublicPolicy:
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) - {
        "$schema",
        "schema",
        "identities",
        "allow_home_users",
        "committer_only",
    }:
        raise ValueError("public_policy_invalid")
    if data.get("schema", PUBLIC_POLICY_SCHEMAS[0]) not in PUBLIC_POLICY_SCHEMAS:
        raise ValueError("public_policy_invalid")
    identities = data.get("identities")
    homes = data.get("allow_home_users", [])
    committer_only = data.get("committer_only", [])
    for values in (identities, homes, committer_only):
        if not isinstance(values, list) or any(
            not isinstance(v, str) or not v.strip() or identity_has_controls(v)
            for v in values
        ):
            raise ValueError("public_policy_invalid")
    if not identities or any(
        not re.fullmatch(r"[^<>]+ <[^<>\s]+>", v.strip())
        for v in [*identities, *committer_only]
    ):
        raise ValueError("public_policy_invalid")
    # 利用者名は1成分だけ。区切りや空白で別のpathを許す入口にしない。
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", v) for v in homes):
        raise ValueError("public_policy_invalid")
    for value in [*identities, *committer_only]:
        email = value.strip().rsplit("<", 1)[1].rstrip(">")
        if personal_mail_domain(email.rsplit("@", 1)[-1]):
            raise ValueError("public_policy_personal_mail")
    return PublicPolicy(
        frozenset(v.strip() for v in identities),
        DEFAULT_ALLOWED_HOME_USERS | frozenset(homes),
        frozenset(v.strip() for v in committer_only),
    )


def load_public_policy(path=None, repo: str = ".") -> PublicPolicy:
    """明示file か、同梱の既定一覧。名簿はリポに置かない（リポ側の一覧では広げられない）。

    `repo` は互換のために受け取るだけで使わない。リポの .repo-preflight/identity-policy.json は読まない。
    """
    del repo
    if path is None:
        path = DEFAULT_PUBLIC_POLICY
    with open(path, encoding="utf-8") as stream:
        return parse_public_policy(stream.read())


@dataclass
class Policy:
    allow_emails: list[str] = field(default_factory=list)  # 完全一致
    allow_email_res: list[re.Pattern] = field(default_factory=list)
    allow_names: list[str] = field(default_factory=list)
    deny_res: list[re.Pattern] = field(default_factory=list)  # 個人の型（非公開）
    allow_path_res: list[re.Pattern] = field(
        default_factory=list
    )  # 箱の作業場など許す型
    reject_all_coauthors: bool = True
    deny_email_sha256s: list[str] = field(default_factory=list)


def load_policy() -> Policy:
    raw = None
    p = os.environ.get("PREFLIGHT_PRIVATE_PATTERNS_FILE")
    if p:
        with open(p, encoding="utf-8") as stream:
            raw = stream.read()
    elif os.environ.get("PREFLIGHT_PRIVATE_PATTERNS"):
        raw = os.environ["PREFLIGHT_PRIVATE_PATTERNS"]
    if not raw:
        raise SystemExit("設定なし: 非公開の型設定が読めません（失敗扱い）")
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError("private_policy_object_required")
    if set(d) - {
        "allow_emails",
        "allow_email_patterns",
        "allow_names",
        "deny_patterns",
        "allow_path_patterns",
        "reject_all_coauthors",
        "deny_email_sha256s",
    }:
        raise ValueError("private_policy_unknown_key")
    for key in (
        "allow_emails",
        "allow_email_patterns",
        "allow_names",
        "deny_patterns",
        "allow_path_patterns",
        "deny_email_sha256s",
    ):
        values = d.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError("private_policy_list_invalid")
    if type(d.get("reject_all_coauthors", True)) is not bool:
        raise ValueError("private_policy_boolean_required")
    if not d.get("allow_emails") and not d.get("allow_email_patterns"):
        raise ValueError("private_policy_email_allowlist_required")
    if any(
        not re.fullmatch(r"[0-9a-fA-F]{64}", value)
        for value in d.get("deny_email_sha256s", [])
    ):
        raise ValueError("private_policy_email_hash_invalid")
    c = lambda xs: [re.compile(x, re.I) for x in xs]
    return Policy(
        allow_emails=[e.lower() for e in d.get("allow_emails", [])],
        allow_email_res=c(d.get("allow_email_patterns", [])),
        allow_names=d.get("allow_names", []),
        deny_res=c(d.get("deny_patterns", [])),
        allow_path_res=c(d.get("allow_path_patterns", [])),
        reject_all_coauthors=d.get("reject_all_coauthors", True),
        deny_email_sha256s=[v.lower() for v in d.get("deny_email_sha256s", [])],
    )


def email_ok(pol: Policy, email: str) -> bool:
    if identity_has_controls(email):
        return False
    e = email.strip().casefold()
    if hashlib.sha256(e.encode("utf-8")).hexdigest() in pol.deny_email_sha256s:
        return False
    return e in pol.allow_emails or any(r.fullmatch(e) for r in pol.allow_email_res)


def masked_identity_name(name: str) -> bool:
    """伏せ字はログ表示にだけ使い、登録可能な名義として扱わない。"""
    normalized = unicodedata.normalize("NFKC", name).strip()
    return normalized.strip("[](){}<>【】 ").casefold() == "redacted"


def identity_has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def parse_trailer(value: str) -> tuple[str, str] | None:
    """strip 前に制御文字を拒否し、名義とメール以外の末尾を許さない。"""
    if identity_has_controls(value):
        return None
    match = re.fullmatch(r" *([^<>]+?) *<([^<>\s]+)> *", value)
    if not match or not match.group(1).strip():
        return None
    return match.group(1).strip(), match.group(2)


def scan_text(pol: Policy, text: str, where: str) -> list[str]:
    """本文（コミット本文・PR題名と本文・コメント）を検査。見つけた中身は出さず種類だけ返す。"""
    out = []
    for m in TRAILER_RE.finditer(text):
        kind, val = m.group(1).lower(), m.group(2)
        parsed = parse_trailer(val)
        if parsed and masked_identity_name(parsed[0]):
            out.append(f"{where}: {kind}の名義が伏せ字")
        if kind == "co-authored-by" and pol.reject_all_coauthors:
            out.append(f"{where}: 共同作者行あり")
        elif (
            not parsed
            or not email_ok(pol, parsed[1])
            or (pol.allow_names and parsed[0] not in pol.allow_names)
        ):
            out.append(f"{where}: 許可外の{kind}行")
    for kind in shape_findings(text):
        # パスは下の非公開policy付きの判定、鍵は秘密検査が持つ。二重に数えない。
        if kind not in {"実ホームパスの形", "鍵の形"}:
            out.append(f"{where}: {kind}")
    for r in pol.deny_res:
        if r.search(text):
            out.append(f"{where}: 個人の型に一致")
            break
    for r in GENERIC_PATH_RES:
        for m in r.finditer(text):
            if not any(a.match(text, m.start()) for a in pol.allow_path_res):
                out.append(f"{where}: 実在パスの型")
                break
    return out


def check_identity(
    pol: Policy, name: str, email: str, role: str, where: str
) -> list[str]:
    out = []
    if identity_has_controls(name) or identity_has_controls(email):
        out.append(f"{where}: {role}の名義に制御文字")
    if masked_identity_name(name):
        out.append(f"{where}: {role}の名義が伏せ字")
    if not email_ok(pol, email):
        out.append(f"{where}: {role}のメールが許可外")
    if pol.allow_names and name not in pol.allow_names:
        out.append(f"{where}: {role}の名前が許可外")
    if any(r.search(f"{name} {email}") for r in pol.deny_res):
        out.append(f"{where}: {role}が個人の型に一致")
    return out


def git(repo: str, *a: str) -> str:
    # Preserve the explicit repository even when a caller carries Git's directory overrides.
    env = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_INDEX_FILE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        env.pop(key, None)
    return subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            repo,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "log.showSignature=false",
            "-c",
            "i18n.logOutputEncoding=UTF-8",
            *a,
        ],
        check=True,
        capture_output=True,
        env=env,
    ).stdout.decode("utf-8")


def commit_ids(repo: str, rng: str, run_git) -> list[str]:
    output = run_git(repo, "rev-list", *((rng,) if isinstance(rng, str) else rng))
    if not isinstance(output, str):
        raise ValueError("identity_commit_list_invalid")
    if not output:
        return []
    ids = output.removesuffix("\n").split("\n")
    if any(not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) for oid in ids):
        raise ValueError("identity_commit_list_invalid")
    return ids


def parse_commit_identity(value: str) -> tuple[str, str]:
    if identity_has_controls(value):
        raise ValueError("identity_commit_header_invalid")
    match = re.fullmatch(
        r"([^<>]+) <([^<>\s]+)> (-?[0-9]+) ([+-])([0-9]{2})([0-9]{2})", value
    )
    if (
        not match
        or not match.group(1).strip()
        or int(match.group(5)) > 23
        or int(match.group(6)) > 59
    ):
        raise ValueError("identity_commit_header_invalid")
    return match.group(1), match.group(2)


def raw_commits(repo: str, rng: str, run_git):
    """整形ログの区切り文字に依存せず、保存された commit の名義と本文を読む。"""
    for oid in commit_ids(repo, rng, run_git):
        raw = run_git(repo, "cat-file", "commit", oid)
        if not isinstance(raw, str) or "\n\n" not in raw:
            raise ValueError("identity_commit_header_invalid")
        headers, body = raw.split("\n\n", 1)
        identities = {}
        previous = None
        # splitlines() は RS 等も行境界にするため、絶対に使わない。
        for line in headers.split("\n"):
            if line.startswith(" "):
                if previous is None or previous in {"author", "committer"}:
                    raise ValueError("identity_commit_header_invalid")
                continue
            key, separator, value = line.partition(" ")
            if not separator or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", key):
                raise ValueError("identity_commit_header_invalid")
            previous = key
            if key in {"author", "committer"}:
                if key in identities:
                    raise ValueError("identity_commit_header_invalid")
                identities[key] = parse_commit_identity(value)
        if set(identities) != {"author", "committer"}:
            raise ValueError("identity_commit_header_invalid")
        yield oid, *identities["author"], *identities["committer"], body


def check_range(
    pol: Policy, repo: str, rng: str, *, identity_rng: str | None = None, git_fn=None
) -> list[str]:
    """PRの全コミット（基点..HEAD）を1件ずつ検査。マージコミットも含める。"""
    run_git = git if git_fn is None else git_fn
    identity_commits = (
        set(commit_ids(repo, identity_rng, run_git))
        if identity_rng is not None
        else None
    )
    out = []
    for h, an, ae, cn, ce, body in raw_commits(repo, rng, run_git):
        w = h[:8]
        if identity_commits is None or h in identity_commits:
            out += check_identity(pol, an, ae, "作者", w)
            out += check_identity(pol, cn, ce, "コミッター", w)
        out += scan_text(pol, body, w + " 本文")
    return out


def _public_policy(allowed_identities) -> PublicPolicy:
    if isinstance(allowed_identities, PublicPolicy):
        return allowed_identities
    return PublicPolicy(frozenset(allowed_identities))


# 例示用に予約されたドメイン（RFC 2606 / 6761）。実在の人に届かないので本文に出てよい。
RESERVED_MAIL_DOMAIN_RE = re.compile(
    r"(?:^|[.])(?:example(?:[.](?:com|net|org))?|invalid|test|localhost|local)$", re.I
)
# ssh の git 利用者（git@github.com:… の clone 手順）。個人メールの形は別に見る。
SSH_GIT_LOCAL = "git"


def unlisted_mail_found(text: str, policy: "PublicPolicy") -> bool:
    """名簿にないメールの形があるか。会社・組織のドメインも、名簿に無ければ止める。"""
    allowed = policy.emails
    for candidate in text_variants(text):
        for m in MAIL_RE.finditer(candidate):
            domain = m.group(1).casefold().rstrip(".")
            if personal_mail_domain(domain):
                continue  # 「個人メールの形」として別に数える
            if RESERVED_MAIL_DOMAIN_RE.search(domain):
                continue
            if m.group(0).split("@", 1)[0] == SSH_GIT_LOCAL:
                continue
            if m.group(0).casefold().rstrip(".") not in allowed:
                return True
    return False


# 敵対的レビュー H6: CI を飛ばす印（GitHub が見る [skip ci] 系と skip-checks 行）
SKIP_CI_RE = re.compile(
    r"\[(?:skip[ _-]?ci|ci[ _-]?skip|no[ _-]?ci|skip[ _-]?actions|actions[ _-]?skip)\]"
    r"|^[ \t]*skip-checks[ \t]*:[ \t]*true",
    re.I | re.M,
)


def check_public_text(allowed_identities, text: str, where: str) -> list[str]:
    """本文（コミット本文・PR題名と本文・コメント）の公開検査。非公開policyは使わない。"""
    policy = _public_policy(allowed_identities)
    issues = []
    normal = unicodedata.normalize("NFKC", text)
    # 署名行（Co-authored-by など）は下で名簿と完全一致を見るので、二重に数えない
    if unlisted_mail_found(PUBLIC_TRAILER_RE.sub("", normal), policy):
        issues.append(f"{where}: 名簿にないメールの形")
    if SKIP_CI_RE.search(normal):
        issues.append(f"{where}: CI を飛ばす指示")
    for trailer in PUBLIC_TRAILER_RE.finditer(normal):
        parsed = parse_trailer(trailer.group(2))
        if (
            not parsed
            or masked_identity_name(parsed[0])
            or f"{parsed[0]} <{parsed[1]}>" not in policy
        ):
            issues.append(f"{where}: 作者情報の行が公開許可名義と不一致")
    issues += [
        f"{where}: {kind}" for kind in shape_findings(text, policy.allow_home_users)
    ]
    return issues


def check_public_range(
    allowed_identities, repo, rng, *, git_fn=None, diffs=False, exceptions=None
):
    """非公開policyを使わず、許可名義の一覧と形だけで判定する（pre-push とCIの共通の中身）。

    diffs=True で、各コミットが足した行も同じ形で見る（pre-push と --range 入口）。
    readiness の全履歴走査は自前の秘密・個人パス検査があるので既定は見ない。
    """
    policy = _public_policy(allowed_identities)
    run_git = git if git_fn is None else git_fn
    fixture_exceptions = load_fixture_exceptions(exceptions) if diffs else {}
    issues = []
    for oid, an, ae, cn, ce, body in raw_commits(repo, rng, run_git):
        for name, email, role in [(an, ae, "作者"), (cn, ce, "コミッター")]:
            if masked_identity_name(name) or not policy.allows(
                f"{name} <{email}>", role
            ):
                issues.append(f"{oid[:8]}: {role}が公開許可名義と不一致")
        issues += check_public_text(policy, body, oid[:8])
        if diffs:
            skip = _exception_skipper(repo, oid, fixture_exceptions)
            issues += diff_findings(
                policy, added_lines(repo, oid, run_git, skip), f"{oid[:8]}: 差分に"
            )
            issues += diff_findings(
                policy, changed_paths(repo, oid, run_git), f"{oid[:8]}: ファイル名に"
            )
            numstat = run_git(
                repo,
                "show",
                "--format=",
                "--numstat",
                "-z",
                "--no-renames",
                "--diff-filter=AM",
                "--first-parent",
                oid,
            )
            if isinstance(numstat, str) and new_binary_count(numstat, skip):
                issues.append(f"{oid[:8]}: {NEW_BINARY_KIND}")
    return issues


# 敵対的レビュー H3: 中身を形で読めないファイル（画像・圧縮・PDF・ノートの出力など）は、
# 足したり変えたりしたら人の承認待ちとして止める。ファイル名は出さず件数だけ。
NEW_BINARY_KIND = "読めない形のファイル（画像・圧縮・PDF・ノートなど。人の承認が要る）"
OPAQUE_EXTENSIONS = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "heic",
        "heif",
        "tif",
        "tiff",
        "bmp",
        "ico",
        "psd",
        "pdf",
        "zip",
        "gz",
        "tgz",
        "bz2",
        "xz",
        "zst",
        "7z",
        "rar",
        "tar",
        "jar",
        "whl",
        "ipynb",
        "docx",
        "xlsx",
        "pptx",
        "odt",
        "sqlite",
        "sqlite3",
        "db",
        "mov",
        "mp4",
        "mp3",
        "m4a",
        "wav",
        "pkl",
        "pickle",
        "npy",
        "npz",
        "parquet",
    }
)


def new_binary_count(numstat_z: str, skip=None) -> int:
    """`--numstat -z` の出力から、バイナリか読めない拡張子のファイルの数を返す。

    skip(path) が真のファイル（人が確認して中身を SHA256 で固定した例外）は数えない。
    """
    count = 0
    for record in filter(None, numstat_z.split("\0")):
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if skip is not None and skip(path):
            continue
        ext = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
        if (added, deleted) == ("-", "-") or ext in OPAQUE_EXTENSIONS:
            count += 1
    return count


# 1件の commit で足した行の上限（文字数）。超えたら読まずに止める（人の承認が要る）。
MAX_ADDED_CHARS = 4_000_000
TOO_LARGE_KIND = "大きすぎる中身（読まずに止めた。人の承認が要る）"


def diff_findings(policy: "PublicPolicy", added: str, prefix: str) -> list[str]:
    """足した行の形と、名簿にないメール（会社・組織のドメインも。敵対的レビュー H2）。

    1件で足した行が上限（MAX_ADDED_CHARS）を超えるときは、読まずに人の承認待ちで止める。
    生成物や写しの大きな塊を形で読み切れず、push の時間の上限も守れないため。
    """
    if len(added) > MAX_ADDED_CHARS:
        return [f"{prefix}{TOO_LARGE_KIND}"]
    issues = [
        f"{prefix}{kind}" for kind in shape_findings(added, policy.allow_home_users)
    ]
    if unlisted_mail_found(added, policy):
        issues.append(f"{prefix}名簿にないメールの形")
    return issues


# 名義の検査（足した行の形と読めないファイル）から外すファイルの一覧。人が中身を確かめたものだけ。
# 固定した repo-preflight 側の一覧だけを読む（調べるリポ側の一覧では広げられない）。
# 1行に1ファイル：「SHA256 2つの空白 パス 2つの空白 # 理由」。フォルダ・ワイルドカードは書けない。
# そのコミット（またはステージ）での中身の SHA256 が一致するときだけ外す。中身が変われば、また検査する。
FIXTURE_EXCEPTIONS_FILE = (
    Path(__file__).resolve().parent.parent
    / ".repo-preflight"
    / "identity-fixture-exceptions.txt"
)
_EXCEPTION_LINE_RE = re.compile(
    r"^(?P<sha>[0-9a-f]{64})  (?P<path>[A-Za-z0-9._/-]+)  # (?P<reason>\S.*)$"
)


def parse_fixture_exceptions(text: str) -> dict:
    """例外一覧を読む。形が違う行・フォルダ・ワイルドカード・重複は ValueError（止める）。"""
    entries: dict = {}
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        m = _EXCEPTION_LINE_RE.match(line)
        if not m:
            raise ValueError("fixture_exception_line_invalid")
        path = m.group("path")
        parts = path.split("/")
        if (
            path.endswith("/")
            or path.startswith("/")
            or any(part in ("", ".", "..") for part in parts)
            or path in entries
        ):
            raise ValueError("fixture_exception_path_invalid")
        entries[path] = m.group("sha")
    return entries


@functools.lru_cache(maxsize=None)
def _default_fixture_exceptions() -> tuple:
    try:
        text = FIXTURE_EXCEPTIONS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    return tuple(sorted(parse_fixture_exceptions(text).items()))


def load_fixture_exceptions(exceptions=None) -> dict:
    if exceptions is not None:
        return dict(exceptions)
    return dict(_default_fixture_exceptions())


def _blob_sha256(repo: str, spec: str, *, hook: bool = False) -> str | None:
    """`<commit>:<path>` か `:<path>`（索引）の中身の SHA256。読めなければ None（外さない）。"""
    cmd = ["git", "-C", repo, "cat-file", "blob", spec]
    env = None
    if not hook:
        env = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(key, None)
    result = subprocess.run(cmd, capture_output=True, env=env)
    if result.returncode:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def _exception_skipper(repo: str, rev: str, exceptions: dict, *, hook: bool = False):
    """パスが例外一覧にあり、そのコミット（rev が空なら索引）の中身の SHA256 が一致すれば真。"""
    cache: dict = {}

    def skip(path: str) -> bool:
        expected = exceptions.get(path)
        if expected is None:
            return False
        if path not in cache:
            cache[path] = _blob_sha256(repo, f"{rev}:{path}", hook=hook) == expected
        return cache[path]

    return skip


def _added_from_diff(diff: str, skip=None) -> str:
    """unified diff から足した行だけを返す。skip(path) が真のファイルの行は入れない。"""
    out = []
    current_skipped = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current_skipped = False
            continue
        if line.startswith("+++"):
            target = line[4:]
            current_skipped = bool(
                skip is not None and target.startswith("b/") and skip(target[2:])
            )
            continue
        if line.startswith("+") and not current_skipped:
            out.append(line[1:])
    return "\n".join(out)


def added_lines(repo: str, oid: str, run_git, skip=None) -> str:
    """そのコミットで足された行だけを返す（消した行と文脈行は見ない）。"""
    diff = run_git(
        repo,
        "show",
        "--format=",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--first-parent",
        oid,
    )
    if not isinstance(diff, str):
        return ""
    return _added_from_diff(diff, skip)


# レビュー3C 高1: 足した・変えた・改名したファイルのパス名（改名先も）。-z で読み、改行で並べ直す。
_PATH_NAME_ARGS = ("--name-only", "-z", "--no-renames", "--diff-filter=ACMRT")


def _joined_paths(out) -> str:
    return "\n".join(filter(None, out.split("\0"))) if isinstance(out, str) else ""


def changed_paths(repo: str, oid: str, run_git) -> str:
    """そのコミットで足した・変えたパス名（改名は新しい名前）を返す。"""
    return _joined_paths(
        run_git(repo, "show", "--format=", *_PATH_NAME_ARGS, "--first-parent", oid)
    )


def _hook_git(repo: str, *args: str) -> str:
    """フックの中の git。GIT_INDEX_FILE はそのまま使う（`commit -a` や一部だけの commit は一時の索引を渡す）。"""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    ).stdout


def check_public_staged(allowed_identities, repo: str, exceptions=None) -> list[str]:
    """pre-commit の中身：これから作る commit の作者・記録者と、ステージした差分の足した行。

    全履歴ラチェットは pre-push と必須チェックで見る（ここは2秒以内に収める）。
    """
    policy = _public_policy(allowed_identities)
    issues = []
    for var, role in (
        ("GIT_AUTHOR_IDENT", "作者"),
        ("GIT_COMMITTER_IDENT", "コミッター"),
    ):
        ident = _hook_git(repo, "var", var).strip()
        m = re.match(r"^(.*?) <([^<>]*)> \d+ [+-]\d{4}$", ident)
        if (
            not m
            or masked_identity_name(m.group(1))
            or not policy.allows(f"{m.group(1)} <{m.group(2)}>", role)
        ):
            issues.append(f"これから作る commit: {role}が公開許可名義と不一致")
    diff = _hook_git(
        repo,
        "diff",
        "--cached",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-renames",
    )
    skip = _exception_skipper(repo, "", load_fixture_exceptions(exceptions), hook=True)
    added = _added_from_diff(diff, skip)
    issues += diff_findings(policy, added, "ステージした差分に")
    issues += diff_findings(
        policy,
        _joined_paths(_hook_git(repo, "diff", "--cached", *_PATH_NAME_ARGS)),
        "ステージしたファイル名に",
    )
    numstat = _hook_git(
        repo, "diff", "--cached", "--numstat", "-z", "--no-renames", "--diff-filter=AM"
    )
    if new_binary_count(numstat, skip):
        issues.append(f"ステージした差分に{NEW_BINARY_KIND}")
    return issues


GITLEAKS_PINS = (
    Path(__file__).resolve().parent.parent / "references" / "gitleaks-pins.json"
)
GITLEAKS_CONFIG = Path(__file__).resolve().parent.parent / ".gitleaks.toml"


def pinned_gitleaks() -> str:
    """PATH の gitleaks が固定した版のハッシュと一致するときだけ、その絶対パスを返す（敵対的レビュー H4）。"""
    exe = shutil.which("gitleaks")
    if exe is None:
        raise RuntimeError("gitleaks_missing")
    exe = os.path.realpath(exe)
    with open(GITLEAKS_PINS, encoding="utf-8") as stream:
        pins = json.load(stream)
    allowed = set(pins["binary_sha256"].values())
    digest = hashlib.sha256()
    with open(exe, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() not in allowed:
        raise RuntimeError("gitleaks_unpinned")
    return exe


def _gitleaks(repo: str, args: list[str]) -> bool:
    result = subprocess.run(
        [
            pinned_gitleaks(),
            "git",
            *args,
            "--no-banner",
            "--redact",
            "--log-level",
            "error",
            "--config",
            str(GITLEAKS_CONFIG),
            ".",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("gitleaks_error")
    return result.returncode == 0


def gitleaks_range(repo: str, spec: list[str]) -> bool:
    """送る範囲だけ gitleaks で見る。"""
    return _gitleaks(repo, [f"--log-opts={' '.join(spec)}"])


def gitleaks_staged(repo: str) -> bool:
    """ステージした差分を gitleaks で見る。gitleaks が無い・固定の版でなければ RuntimeError（止める）。"""
    exe = pinned_gitleaks()
    config = GITLEAKS_CONFIG
    result = subprocess.run(
        [
            exe,
            "git",
            "--staged",
            "--no-banner",
            "--redact",
            "--log-level",
            "error",
            "--config",
            str(config),
            ".",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("gitleaks_error")
    return result.returncode == 0


def pre_push_ranges(repo: str, remote: str, stdin_text: str) -> list[list[str]]:
    """pre-push の範囲作りは既存の local_identity_guard.push_ranges_from_stdin をそのまま使う。"""
    spec = importlib.util.spec_from_file_location(
        "local_identity_guard", Path(__file__).with_name("local_identity_guard.py")
    )
    guard = sys.modules.get("local_identity_guard")
    if guard is None:
        guard = importlib.util.module_from_spec(spec)
        sys.modules["local_identity_guard"] = (
            guard  # dataclass が自分の module を引くため
        )
        spec.loader.exec_module(guard)
    return guard.push_ranges_from_stdin(Path(repo), remote, stdin_text)


ZERO_OID = "0" * 40


def check_pushed_tags(policy: "PublicPolicy", repo: str, stdin_text: str) -> list[str]:
    """注釈付きタグの作者（tagger）と本文を見る（敵対的レビュー H7）。値は出さない。"""
    issues = []
    for line in stdin_text.splitlines():
        fields = line.split()
        if len(fields) != 4 or fields[1] == ZERO_OID:
            continue
        local_sha = fields[1]
        kind = _hook_git(repo, "cat-file", "-t", local_sha).strip()
        seen = set()
        while kind == "tag" and local_sha not in seen:
            seen.add(local_sha)
            raw = _hook_git(repo, "cat-file", "tag", local_sha)
            header, _, body = raw.partition("\n\n")
            where = f"タグ {local_sha[:8]}"
            tagger = re.search(r"^tagger (.*?) <([^<>]*)> \d+ [+-]\d{4}$", header, re.M)
            if (
                not tagger
                or masked_identity_name(tagger.group(1))
                or f"{tagger.group(1)} <{tagger.group(2)}>" not in policy
            ):
                issues.append(f"{where}: タグの作者が公開許可名義と不一致")
            issues += check_public_text(policy, body, where)
            target = re.search(r"^object ([0-9a-f]{40,64})$", header, re.M)
            if not target:
                issues.append(f"{where}: タグの中身が読めない")
                break
            local_sha = target.group(1)
            kind = _hook_git(repo, "cat-file", "-t", local_sha).strip()
    return issues


def check_new_roots(repo: str, ranges: list[list[str]]) -> list[str]:
    """別の履歴（相手に無い根の commit）を送ろうとしたら止める（敵対的レビュー H8）。

    相手が空のとき（初回の push）だけ、根が1つなら通す。
    """
    initial_roots: set[str] = set()
    issues = []
    for rng in ranges:
        roots = set(_hook_git(repo, "rev-list", "--max-parents=0", *rng).split())
        initial = len(rng) == 1 and ".." not in rng[0]
        if initial:
            initial_roots |= roots
        elif roots:
            issues.append("相手に無い根の commit（別の履歴）を送ろうとしている")
    if len(initial_roots) > 1:
        issues.append("初回の push に根の commit が複数ある（別の履歴が混ざっている）")
    return issues


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    import argparse

    ap = argparse.ArgumentParser(description="名義と本文の検査")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--range", help="例: origin/main..HEAD")
    ap.add_argument("--message-file", help="commit-msg 用")
    ap.add_argument(
        "--pr-event", help="GITHUB_EVENT_PATH のJSON（PR本文・題名・コメント）"
    )
    ap.add_argument(
        "--public",
        action="store_true",
        help="非公開policyを使わず、許可名義の一覧と形だけで判定する（pre-push とCIの共通入口）",
    )
    ap.add_argument(
        "--allowed-identities",
        help="--public の一覧file（既定: 固定した repo-preflight に同梱の一覧。リポ側の一覧は読まない）",
    )
    ap.add_argument(
        "--pre-push",
        action="store_true",
        help="--public で、pre-push の標準入力から範囲を作る",
    )
    ap.add_argument(
        "--remote", default="origin", help="--pre-push の送り先（フックの $1）"
    )
    ap.add_argument(
        "--staged",
        action="store_true",
        help="--public で、pre-commit 用にこれから作る commit の名義とステージした差分（gitleaks 込み）を見る",
    )
    ns = ap.parse_args(argv)
    if not (ns.message_file or ns.range or ns.pr_event or ns.pre_push or ns.staged):
        ap.error("検査対象を指定してください")
    if ns.public:
        return public_main(ns)
    if ns.pre_push or ns.staged or ns.allowed_identities:
        ap.error(
            "--pre-push・--staged・--allowed-identities は --public と使ってください"
        )
    try:
        pol = load_policy()
    except (OSError, UnicodeError, ValueError, TypeError, re.error):
        print('{"status": "tool_error", "reason": "private_patterns_invalid"}')
        return 2
    try:
        issues: list[str] = []
        if ns.message_file:
            issues += scan_text(
                pol, open(ns.message_file, encoding="utf-8").read(), "コミット文"
            )
        if ns.range:
            issues += check_range(pol, ns.repo, ns.range)
        if ns.pr_event:
            ev = json.load(open(ns.pr_event, encoding="utf-8"))
            pr = ev.get("pull_request") or ev.get("issue") or {}
            issues += scan_text(
                pol, (pr.get("title") or "") + "\n" + (pr.get("body") or ""), "PR本文"
            )
            if ev.get("comment"):
                issues += scan_text(pol, ev["comment"].get("body") or "", "コメント")
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ):
        print('{"status": "tool_error", "reason": "identity_input_unavailable"}')
        return 2
    for i in issues:
        print("失敗:", i, file=sys.stderr)
    print(
        json.dumps(
            {"status": "fail" if issues else "pass", "issue_count": len(issues)},
            ensure_ascii=False,
        )
    )
    return 1 if issues else 0


# PR 題名・見出しの日本語境界。github-ops-skills の pr-japanese-gate の判定をそのまま移した。
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_ATX = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)\s*$", re.MULTILINE)
_SETEXT = re.compile(
    r"^[ \t]{0,3}(.+?)[ \t]*\n[ \t]{0,3}(?:=+|-+)[ \t]*$", re.MULTILINE
)
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_LINK = re.compile(r"!?\[[^\]]*\]\(")


def _rendered_text(source: str) -> str:
    visible = []
    fence_char, fence_length = None, 0
    for line in source.split("\n"):
        if fence_char:
            if re.fullmatch(
                rf"[ \t]{{0,3}}{re.escape(fence_char)}{{{fence_length},}}[ \t]*", line
            ):
                fence_char = None
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        opening = _FENCE.match(line) if indent <= 3 else None
        if opening:
            fence_char, fence_length = opening.group(1)[0], len(opening.group(1))
            continue
        visible.append(line)
    return _HTML_COMMENT.sub("", "\n".join(visible))


def japanese_findings(title: str, body: str) -> list[str]:
    """題名に日本語、本文に日本語の見出し、英語だけの見出しなし。見出しの値は返さない。"""
    rendered_body, visible_title = _rendered_text(body), _rendered_text(title)
    atx = _ATX.findall(rendered_body)
    headings = atx + _SETEXT.findall(rendered_body)
    errors = []
    if _MD_LINK.search(visible_title):
        errors.append("PR題名にMarkdownリンクがある")
    if not _JAPANESE.search(visible_title):
        errors.append("PR題名に日本語がない")
    if any(_MD_LINK.search(h) for h in headings):
        errors.append("見出しにMarkdownリンクがある")
    if not any(_JAPANESE.search(h) for h in atx):
        errors.append("PR本文に日本語の見出しがない")
    if any(re.search(r"[A-Za-z]", h) and not _JAPANESE.search(h) for h in headings):
        errors.append("英語だけの見出しがある")
    return errors


def _pr_japanese(path: str) -> list[str]:
    with open(path, encoding="utf-8") as stream:
        pr = json.load(stream).get("pull_request")
    if not pr:
        return []
    return [
        f"PR本文: {e}"
        for e in japanese_findings(pr.get("title") or "", pr.get("body") or "")
    ]


def _pr_texts(path: str) -> list[tuple[str, str]]:
    with open(path, encoding="utf-8") as stream:
        ev = json.load(stream)
    pr = ev.get("pull_request") or ev.get("issue") or {}
    texts = [("PR本文", (pr.get("title") or "") + "\n" + (pr.get("body") or ""))]
    if ev.get("comment"):
        texts.append(("コメント", ev["comment"].get("body") or ""))
    return texts


def _report(issues: list[str]) -> int:
    for i in issues:
        print("失敗:", i, file=sys.stderr)
    print(
        json.dumps(
            {"status": "fail" if issues else "pass", "issue_count": len(issues)},
            ensure_ascii=False,
        )
    )
    return 1 if issues else 0


def public_main(ns) -> int:
    """pre-push フックと必須チェックが呼ぶ1つの入口。判定の中身は check_public_*。"""
    try:
        policy = load_public_policy(ns.allowed_identities, ns.repo)
    except (OSError, UnicodeError, ValueError, TypeError):
        print('{"status": "tool_error", "reason": "public_policy_invalid"}')
        return 2
    try:
        issues: list[str] = []
        if ns.message_file:
            with open(ns.message_file, encoding="utf-8") as stream:
                issues += check_public_text(policy, stream.read(), "コミット文")
        if ns.range:
            issues += check_public_range(policy, ns.repo, ns.range, diffs=True)
        if ns.pre_push:
            stdin_text = sys.stdin.read()
            ranges = pre_push_ranges(ns.repo, ns.remote, stdin_text)
            for rng in ranges:
                issues += check_public_range(policy, ns.repo, rng, diffs=True)
                if not gitleaks_range(ns.repo, rng):
                    issues.append("送る範囲に秘密情報の形（gitleaks）")
            issues += check_pushed_tags(policy, ns.repo, stdin_text)
            issues += check_new_roots(ns.repo, ranges)
        if ns.staged:
            issues += check_public_staged(policy, ns.repo)
            if not gitleaks_staged(ns.repo):
                issues.append("ステージした差分に秘密情報の形（gitleaks）")
        if ns.pr_event:
            for where, text in _pr_texts(ns.pr_event):
                issues += check_public_text(policy, text, where)
            issues += _pr_japanese(ns.pr_event)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        AttributeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        if str(exc) == "gitleaks_unpinned":
            print(
                "失敗: gitleaks が固定した版（references/gitleaks-pins.json）と一致しないので止めます",
                file=sys.stderr,
            )
        if str(exc) == "gitleaks_missing":
            print(
                "失敗: gitleaks が無いので止めます（入れてから commit してください）",
                file=sys.stderr,
            )
        print('{"status": "tool_error", "reason": "identity_input_unavailable"}')
        return 2
    return _report(issues)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
