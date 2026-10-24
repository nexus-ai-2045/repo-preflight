"""品質係の下見（2026-10-24）で、人の承認（15:29 の `.get(` と `href=` の中）より広かった3つを止める。

(a) .local のメール（git が自動で作る「名前@端末名.local」）
(b) HTTP の動詞（GET・POST など）の直後のホーム
(c) path=・url=・to=・redirect= などの後に引用符で来るホーム
(d) 数字+x で始まるドメインと Punycode の末尾のメール（画像の倍率の除外が広すぎた分）

値はすべて仮の値で、実行時に組み立てる。IDENTITY_GATE_SCRIPT で直す前の版に差し替えると、RED が落ちる。
"""

import pytest

from test_review_3c import AT, H, message, policy  # noqa: F401

RED = {
    # (a)
    "git の自動メール 名前@端末名.local": "Author: alice <alice"
    + AT
    + "alices-laptop.local>",
    "mDNS の .local": "ssh pi" + AT + "raspberrypi.local",
    "大文字の .LOCAL": "alice" + AT + "Alice-PC.LOCAL",
    # (b)
    "GET の直後の名前1つ": f"GET /{H}/alice",
    "GET の直後の経路の形": f"GET /{H}/feed",
    "POST の直後で下に続きがある": f"POST /{H}/alice/x",
    "DELETE の直後": f"DELETE /{H}/alice",
    "OPTIONS の直後": f"OPTIONS /{H}/alice",
    # (c)
    "path= の引用符": f'path="/{H}/alice"',
    "url= の引用符": f"url='/{H}/alice'",
    "to= の引用符": f'to="/{H}/alice"',
    "redirect= の引用符": f'redirect="/{H}/alice"',
    "action= の引用符": f'action="/{H}/alice"',
    "route: の引用符": f'route: "/{H}/alice"',
    "url: の JSX": f'url={{"/{H}/alice"}}',
    # (d)
    "数字+x で始まるドメイン": "fixture" + AT + "2x.company.com",
    "Punycode の末尾": "fixture" + AT + "company.xn--p1ai",
}
GREEN = {
    "承認どおり .get( の中": f'app.get("/{H}/index", show)',
    "承認どおり href= の中": f'<a href="/{H}/feed">',
    "例示の localhost は外したまま": "user" + AT + "localhost",
    "例示の .test は外したまま": "alice" + AT + "example.test",
    "画像の倍率は外したまま": "icon" + AT + "3x.webp",
}


@pytest.mark.parametrize("name", sorted(RED))
def test_wider_than_human_approval_is_red(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, RED[name])
    assert result.returncode == 1, (name, result.stderr)


@pytest.mark.parametrize("name", sorted(GREEN))
def test_human_approved_route_stays_green(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, GREEN[name])
    assert result.returncode == 0, (name, result.stderr)
