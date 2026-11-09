"""実IDのない検査ルールと実セッションURLの境界を確認する。"""

import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import identity_gate as g  # noqa: E402


def test_セッションの識別子がない検査ルール定義は通す():
    # 既存ルールを別moduleへ移すとき、[.]の正規化がルール自身をURL扱いした回帰。
    host = ".".join(("claude", "ai"))
    prefix = host + "/code/" + "session" + "_"
    rule = (
        '    re.compile(r"(?im)^[ \\t]*claude-session[ \\t]*:|'
        + prefix.replace(".", "[.]")
        + '"),'
    )
    assert not g.session_trace_found(prefix)
    assert g.shape_findings(rule) == []
    # 1文字でもIDがあれば、伏せ字・URL符号化・ファイル名の写しも引き続き止める。
    fullwidth = str.maketrans({chr(c): chr(c + 0xFEE0) for c in range(33, 127)})
    for domain in ("ai", "com"):
        for separator in ("/", "_"):
            for first in ("0", "a", "_", "-"):
                prefix = (
                    ".".join(("claude", domain))
                    + separator
                    + "code"
                    + separator
                    + "session_"
                )
                assert not g.session_trace_found(prefix)
                actual = prefix + first
                for variant in (
                    actual,
                    actual.replace(".", "[.]"),
                    quote(actual, safe=""),
                    actual.replace("/", "_"),
                    actual.translate(fullwidth),
                    f'"{prefix}" + "{first}"',
                ):
                    assert g.session_trace_found(variant)
        # セッション専用経路でないcode IDの拒否も維持する。
        assert g.session_trace_found(".".join(("claude", domain)) + "/code/" + "0")
