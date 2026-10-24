"""Web の経路の形（app.get の中の index の経路など）を実ホームの検査から外す（2026-10-24 15:29 人承認）。

値はすべて仮の値で、実行時に組み立てる。ホームの下に続きのある実名と ubuntu は止まったまま。
"""

import pytest

from test_review_3c import H, message, policy  # noqa: F401

GREEN = {
    "express の get": f'app.get("/{H}/index", show)',
    "router の post": f"router.post('/{H}/feed', save)",
    "href の経路": f'<a href="/{H}/feed">',
    "jsx の to": f'<Link to={{"/{H}/settings"}}>',
    "push とクエリ": f"router.push('/{H}/settings?tab=1')",
}
RED = {
    "経路の下に続きがある実名": f'app.get("/{H}/alice/x")',
    "経路でない引用符の実名": f'path_name = "/{H}/alice"',
    "読み込みの実名": f'fs.readFile("/{H}/alice")',
    "引用符なしの実名": f"/{H}/alice/project",
    "経路でも ubuntu は止める": f'app.get("/{H}/ubuntu")',
    "経路の文脈でも普通の実ホーム": f'href="/{H}/alice/.ssh/id"',
}


@pytest.mark.parametrize("name", sorted(GREEN))
def test_web_route_is_green(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, GREEN[name])
    assert result.returncode == 0, (name, result.stderr)


@pytest.mark.parametrize("name", sorted(RED))
def test_real_home_stays_red(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, RED[name])
    assert result.returncode == 1, (name, result.stderr)
