"""Private metadata coverage is explicit and cannot widen the public scan policy."""

import hashlib
import io
import json
from pathlib import Path

import pytest

pytest.importorskip(
    "PIL", minversion="13.0", reason="Pillow媒体検査はPython 3.11以上とPillow 13が必要"
)
from PIL import Image, PngImagePlugin

from test_private_submission_scope import submission, run_scan
from test_readiness_scan import MODULE, git


def add_png(repo, comment="public metadata"):
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", comment + "\n" + "x" * 5000)
    raw = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(raw, format="PNG", pnginfo=info)
    p = repo / "sample.png"
    p.write_bytes(raw.getvalue())
    git(repo, "add", "sample.png")
    git(repo, "commit", "-m", "synthetic asset")
    return p


@pytest.mark.parametrize("private,release", [(False, False), (True, True)])
def test_metadata_policy_rejects_nonprivate_or_release_before_io(
    tmp_path, private, release
):
    result = MODULE.scan(
        tmp_path / "missing",
        private_submission=private,
        release=release,
        base_ref="origin/main",
        media_inspection="metadata",
    )
    assert result == {
        "status": "tool_error",
        "issues": ["invalid_media_inspection_scope"],
    }


@pytest.mark.parametrize("intent", ["publish", "release", "configure_settings"])
def test_cli_metadata_policy_rejects_public_lifecycle(intent):
    args = MODULE.build_parser().parse_args(
        [
            "--repo",
            ".",
            "--intent",
            intent,
            "--base-ref",
            "origin/main",
            "--private-submission",
            "--media-inspection",
            "metadata",
        ]
    )
    with pytest.raises(SystemExit):
        MODULE.resolve_options(args, stdin_is_tty=False)


def test_default_keeps_oversize_unknown_but_explicit_private_policy_covers_metadata(
    submission, monkeypatch
):
    repo, base, _, _ = submission
    p = add_png(repo)
    monkeypatch.setattr(MODULE, "_HISTORY_BATCH_BUDGET_BYTES", 4096)
    strict = run_scan(repo, base)
    assert strict["checks"]["private_identity_scan"]["status"] == "unknown"
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["status"] == "pass"
    evidence = result["checks"]["private_identity_scan"]
    assert evidence["media_inspection"] == "metadata_private_only"
    assert evidence["media_content_privacy_verified"] is False
    (record,) = evidence["metadata_media_inspections"]
    assert record["source_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert record["payload_inspected"] is False
    assert evidence["oversize_objects_unscanned"] == 0


def test_private_marker_in_metadata_still_blocks(submission, monkeypatch):
    repo, base, _, _ = submission
    add_png(repo, "synthetic-private-identity-marker")
    monkeypatch.setattr(MODULE, "_HISTORY_BATCH_BUDGET_BYTES", 4096)
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["status"] == "blocked"
    assert result["checks"]["private_identity_scan"]["status"] == "fail"
    assert "synthetic-private-identity-marker" not in json.dumps(result)


def test_unknown_large_format_is_not_exempt(submission, monkeypatch):
    repo, base, _, _ = submission
    (repo / "large.bin").write_bytes(b"x" * 5000)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic opaque file")
    monkeypatch.setattr(MODULE, "_HISTORY_BATCH_BUDGET_BYTES", 4096)
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["checks"]["private_identity_scan"]["status"] == "unknown"


def test_fake_media_is_not_a_metadata_success(submission):
    repo, base, _, _ = submission
    (repo / "fake.mp4").write_bytes(b"not a video")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic invalid media")
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["checks"]["private_identity_scan"]["status"] == "tool_error"


def test_changed_git_object_bytes_are_not_accepted(submission, monkeypatch):
    from subprocess import CompletedProcess

    repo, base, _, _ = submission
    add_png(repo)
    original = MODULE.run_subprocess

    def corrupt(args, **kwargs):
        if list(args[:3]) == ["git", "cat-file", "blob"]:
            kwargs["stdout"].write(b"x" * 5110)
            return CompletedProcess(args, 0)
        return original(args, **kwargs)

    monkeypatch.setattr(MODULE, "run_subprocess", corrupt)
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["checks"]["private_identity_scan"]["status"] == "tool_error"


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("alignment", [0, 1])
def test_raw_metadata_identity_is_decoded_before_joining(
    submission, encoding, alignment, monkeypatch
):
    repo, base, _, _ = submission
    marker = "synthetic-private-identity-marker"
    info = PngImagePlugin.PngInfo()
    info.add_text("before-padding", "x" * 70000)
    info.add(b"vpAg", b"q" * alignment + marker.encode(encoding))
    info.add_text("after-padding", "x" * 70000)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    raw = output.getvalue()
    assert marker.encode(encoding) not in raw[:65536] + raw[-65536:]
    (repo / "encoded.png").write_bytes(raw)
    git(repo, "add", "encoded.png")
    git(repo, "commit", "-m", "synthetic encoded metadata")
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["status"] == "blocked"
    assert result["checks"]["private_identity_scan"]["status"] == "fail"
    assert marker not in json.dumps(result)


def test_expanded_metadata_views_are_bounded(submission, monkeypatch):
    repo, base, _, _ = submission
    add_png(repo)
    monkeypatch.setattr(MODULE, "_MEDIA_METADATA_DECODED_BUDGET_BYTES", 64)
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["checks"]["private_identity_scan"]["status"] == "tool_error"


def test_xmp_decoded_identity_reaches_private_callback(submission):
    repo, base, _, _ = submission
    marker = "synthetic-private-identity-marker"
    encoded = "".join("&#" + str(ord(c)) + ";" for c in marker)
    info = PngImagePlugin.PngInfo()
    info.add_itxt("XML:com.adobe.xmp", "<root>" + encoded + "</root>", zip=True)
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    (repo / "xmp.png").write_bytes(output.getvalue())
    git(repo, "add", "xmp.png")
    git(repo, "commit", "-m", "synthetic XMP metadata")
    result = run_scan(repo, base, media_inspection="metadata")
    assert result["status"] == "blocked"
    assert result["checks"]["private_identity_scan"]["status"] == "fail"
    assert marker not in json.dumps(result)


@pytest.mark.parametrize("suffix", ["png", "jpg"])
@pytest.mark.parametrize("private_target", [False, True])
def test_media_named_symlinks_keep_existing_text_inspection(
    submission, suffix, private_target
):
    repo, base, _, _ = submission
    marker = "synthetic-private-identity-marker"
    target = marker if private_target else "assets/target.png"
    (repo / "link-target.txt").write_text(target)
    import subprocess

    oid = subprocess.check_output(
        ["git", "hash-object", "-w", "link-target.txt"], cwd=repo, text=True
    ).strip()
    (repo / "link-target.txt").unlink()
    git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        "120000," + oid + ",preview." + suffix,
    )
    git(repo, "commit", "-m", "synthetic media symlink")
    # Materialize the Git link as a plain target file, including on Windows.
    git(repo, "config", "core.symlinks", "false")
    git(repo, "checkout-index", "--force", "--", "preview." + suffix)
    strict = run_scan(repo, base)
    result = run_scan(repo, base, media_inspection="metadata")
    assert (
        result["status"]
        == strict["status"]
        == ("blocked" if private_target else "pass")
    )
    assert (
        result["checks"]["private_identity_scan"].get("metadata_media_inspections", [])
        == []
    )
    assert marker not in json.dumps(result)
