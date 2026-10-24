"""レビュー3C 高8 の提案（2026-10-24 15:29 人承認）どおり、例示・コンテナの利用者名と macOS の共有フォルダを外す。

値はすべて仮の値で、実行時に組み立てる。
"""

import pytest

from test_review_3c import H, U, message, policy  # noqa: F401

GREEN = {
    "例示の user": f"/{H}/user/project",
    "devcontainer の vscode": f"/{H}/vscode/.cache",
    "docker の node": f"/{H}/node/app",
    "macOS の共有フォルダ": f"/{U}/Shared/data",
    "<user> の例": f"/{H}/<user>/project",
    "$USER の例": f"/{U}/$USER/x",
}
RED = {
    "実名は止まったまま": f"/{H}/alice/x",
    "似た名前は止まる": f"/{H}/username/x",
    "小文字の shared は外さない": f"/{U}/shared-alice/x",
    "vscode の後ろの実名ではなく前": f"/{U}/alice/vscode",
}


@pytest.mark.parametrize("name", sorted(GREEN))
def test_human_pending_exemptions_are_green(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, GREEN[name])
    assert result.returncode == 0, (name, result.stderr)


@pytest.mark.parametrize("name", sorted(RED))
def test_human_pending_real_names_stay_red(policy, tmp_path, name):  # noqa: F811
    result = message(policy, tmp_path, RED[name])
    assert result.returncode == 1, (name, result.stderr)
