"""敵対的レビュー3C の中以下（表の対象外）で、漏れ止めを 9 割台へ上げる分のカナリアと対照。

値はすべて仮の値で、実行時に組み立てる。この commit だけ外しても、重大・高の直しは残る。
"""

import pytest

from test_review_3c import AT, GM, H, J, U, message, policy  # noqa: F401

Z = "0" * 40
SESSION = J(["Claude", "-Session"])
RED = {
    "ゼロ幅入りのパス": f"/Us\u200b{U[2:]}/alice/x",
    "ゼロ幅入りのメール": "alice" + AT + "gm\u200b" + GM[2:],
    "ゼロ幅入りのセッション行": J(["Claude-Ses", "\u200b", "sion: 0000"]),
    "ハイフンに似た文字 U+2010": SESSION.replace("-", "\u2010") + ": 0000",
    "ハイフンに似た文字 en dash": SESSION.replace("-", "\u2013") + ": 0000",
    "key = value": J(["claude", "_session_id"]) + " = 00000000-0000",
    "JSON のキー": '"' + J(["claude", "_session_id"]) + '": "00000000-0000"',
    "camel の JSON": '"' + J(["claude", "SessionId"]) + '": "00000000"',
    "括弧の点 [.]": "https://" + J(["claude", "[.]ai/chat/00000000-0000"]),
    "hxxps と (.)": "hxxps://" + J(["claude", "(.)ai/code/session_0000"]),
    "\\x2f のエスケープ": "\\x2f" + U + "\\x2falice",
    "8進の \\057": "\\057" + U + "\\057alice",
    "+ の無い連結": '"alice" "' + AT + GM + '"',
    "/var の下のホーム": f"/var/{H}/alice/x",
    "/export の下のホーム": f"/export/{H}/alice/x",
    "外付けの macOS": f"/Volumes/Mac/{U}/alice/x",
    "smb の共有": f"smb://nas/{U}/alice",
    "DSA の鍵": J(["-----BEGIN DSA ", "PRIVATE KEY-----"]),
    "暗号化した鍵": J(["-----BEGIN ENCRYPTED ", "PRIVATE KEY-----"]),
    "PGP の鍵": J(["-----BEGIN PGP ", "PRIVATE KEY BLOCK-----"]),
    "AWS の一時鍵 ASIA": "AS" + "IA" + "0" * 16,
    "Google の AIza": "AI" + "za" + "0" * 35,
    "GitLab の glpat": "gl" + "pat-" + "0" * 20,
    "Hugging Face の hf_": "hf" + "_" + "0" * 34,
    "Stripe の sk_live": "sk" + "_live_" + "0" * 24,
    "xAI の xai-": "xa" + "i-" + Z,
}
GREEN = {
    "隣り合う文字列（引数）": 'print("a", "b")',
    "隣り合う文字列（連結）": 'msg = "hello" " world"',
    "正規表現の [.]": 'r"chat[.]openai[.]com/(?:c|share)/"',
    "/var/www": "/var/www/html/index.html",
    "外付けの普通のフォルダ": "/Volumes/Data/photos",
    "smb のホーム以外": "smb://nas/share/docs",
    "hf_ の短い名前": "from huggingface_hub import hf_hub_download",
    "xai- の短い名前": "xai-sdk のインストール",
    "ASIA の単語": "ASIAN-PACIFIC REGION",
    "セッション = 変数": J(["claude", "_session_id"]) + " = os.environ['X']",
    "全角ダッシュの文": "設定 — セッションの説明",
}


@pytest.mark.parametrize("name", sorted(RED))
def test_review_3c_extra_canary_is_red(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, RED[name])
    assert result.returncode == 1, (name, result.stderr)


@pytest.mark.parametrize("name", sorted(GREEN))
def test_review_3c_extra_control_is_green(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, GREEN[name])
    assert result.returncode == 0, (name, result.stderr)
