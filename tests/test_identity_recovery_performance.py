"""公開合成入力で回復解析の実行上限を検証する。"""

from pathlib import Path


def test_recovery_gate_and_parser_bound_repeated_unclosed_escaped_quotes():
    # 各quoteから終端を探し直す旧regexはこの合成入力で二乗時間になった。
    import subprocess
    import sys

    code = r"""
import runpy
engine = runpy.run_path("scripts/identity_normalization.py")
text = "}} " + ('\\"' * 20000) + " + 1"
assert not engine["_needs_foreign_literal_recovery"](text)
assert engine["_literal_plus_chains"](text) == []
# 後方の有効chainも省かず、元の拒否へ到達する。
text += '\nconst x: string = "fixture-private-" + "operator";'
assert engine["scan_private_text"](text, ["fixture-private-operator"])["status"] == "fail"
"""
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        timeout=5,
    )
