"""同じ本文の未正規化・正規化後の件数を、完全束縛のまま照合する。"""

import pytest

from test_reviewed_path_exceptions import (
    MODULE,
    git,
    make_repo,
    policy,
    private_origin,
    report,
    sample_path,
)


def duplicate_source(*, hidden=False):
    value = sample_path() + "/report.txt"
    split = value.index("synthetic-owner")
    text = f"RAW = {value!r}\nREBUILT = {value[:split]!r} + {value[split:]!r}\n"
    if hidden:
        extra = value.replace("synthetic-owner", "other-fixture")
        text += f"HIDDEN = {extra[:split]!r} + {extra[split:]!r}\n"
    return text.encode(), value


def reviewed_duplicate(tmp_path, *, count=2, hidden=False):
    data, value = duplicate_source(hidden=hidden)
    config = policy(
        tmp_path,
        data,
        path="fixture.py",
        scanned_data=value.encode(),
        occurrence_count=count,
    )
    reviewed = MODULE.ReviewedSecretExceptions(
        config, "https://github.com/example/repo.git"
    )
    return data, reviewed, config


def test_normalized_exact_count_also_covers_raw_view(tmp_path, private_origin):
    data, reviewed, _ = reviewed_duplicate(tmp_path)
    raw = MODULE.personal_path_matches(data)
    normalized = MODULE.normalized_personal_path_matches(data)
    assert list(raw.values()) == [1]
    assert list(normalized.values()) == [2]
    assert reviewed.all_paths_reviewed("fixture.py", data, normalized=True)
    assert not reviewed.has_unreviewed_paths("fixture.py", data)
    assert reviewed.all_paths_reviewed("fixture.py", data)


@pytest.mark.parametrize("count", [1, 3])
def test_wrong_normalized_count_never_approves_normalized_view(
    tmp_path, private_origin, count
):
    data, reviewed, _ = reviewed_duplicate(tmp_path, count=count)
    assert not reviewed.all_paths_reviewed("fixture.py", data, normalized=True)
    if count == 3:
        assert reviewed.has_unreviewed_paths("fixture.py", data)


@pytest.mark.parametrize("change", ["path", "content"])
def test_normalized_count_keeps_path_and_content_binding(
    tmp_path, private_origin, change
):
    data, reviewed, _ = reviewed_duplicate(tmp_path)
    path = "copy.py" if change == "path" else "fixture.py"
    if change == "content":
        data += b"CHANGED = True\n"
    assert reviewed.has_unreviewed_paths(path, data)
    assert not reviewed.all_paths_reviewed(path, data, normalized=True)


def test_legacy_raw_count_still_approves_only_raw_view(tmp_path, private_origin):
    data, reviewed, _ = reviewed_duplicate(tmp_path, count=1)
    assert reviewed.all_paths_reviewed("fixture.py", data)
    assert not reviewed.all_paths_reviewed("fixture.py", data, normalized=True)


@pytest.mark.parametrize(
    "count,hidden,expected",
    [(2, False, "pass"), (1, False, "fail"), (3, False, "fail"), (2, True, "fail")],
)
def test_full_scan_rejects_unreviewed_reconstructions(
    tmp_path, monkeypatch, private_origin, count, hidden, expected
):
    repo = make_repo(tmp_path)
    data, _, config = reviewed_duplicate(tmp_path, count=count, hidden=hidden)
    (repo / "fixture.py").write_bytes(data)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Add synthetic path count fixture")
    private_policy = tmp_path / "private-policy.txt"
    private_policy.write_text("unrelated-fixture\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(private_policy))
    result = report(repo, config, require_private_identity=True)
    assert result["checks"]["private_identity_scan"]["status"] == expected
    if expected == "pass":
        assert result["checks"]["personal_path_scan"]["status"] == "pass"
