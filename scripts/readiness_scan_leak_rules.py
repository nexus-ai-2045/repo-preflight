from __future__ import annotations

import hashlib
import re
from collections import Counter
from urllib.parse import unquote

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
