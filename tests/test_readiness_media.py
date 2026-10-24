"""Synthetic media through the actual worktree/history readiness entry."""

import io
import hashlib
import subprocess
import sys

import pytest
from pypdf import PdfWriter
from test_readiness_scan import MODULE, SCRIPT, make_repo, git
from test_media_identity import make_av, requires_decoder, FFMPEG, FFPROBE

requires_pdf = pytest.mark.skipif(
    sys.version_info < (3, 10), reason="PDF inspection requires Python 3.10+"
)

DENIED = "readiness-media-private@example.invalid"


def setup_scan(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    policy = tmp_path / "policy.txt"
    policy.write_text(DENIED + "\n")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return repo


def pdf(title="Public document"):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.add_metadata({"/Title": title})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def private_result(repo):
    return MODULE.scan(repo, require_private_identity=True)["checks"][
        "private_identity_scan"
    ]


@pytest.mark.parametrize("name", [b"ID3-catalog/entry.md", b"%PDF-reference/list.md"])
def test_repository_name_is_not_decoded_as_media(tmp_path, monkeypatch, name):
    repo = setup_scan(tmp_path, monkeypatch)
    scanner = MODULE.PrivateIdentityScan(repo, True)
    assert scanner.checker is not None

    def unexpected_decoder(*args, **kwargs):
        pytest.fail("repository-name bytes were sent to the media decoder")

    scanner.media_checker = unexpected_decoder
    scanner.check(name, repository_name=True)
    assert scanner.report["status"] == "pass"
    assert scanner.report.get("media_objects", 0) == 0


@requires_pdf
def test_real_readiness_pdf_metadata_and_adapter_evidence(tmp_path, monkeypatch):
    repo = setup_scan(tmp_path, monkeypatch)
    (repo / "document.pdf").write_bytes(pdf())
    result = private_result(repo)
    assert result["status"] == "pass"
    assert result["media_objects"] >= 1
    assert (
        result["media_adapter_sha256"]
        == hashlib.sha256(
            SCRIPT.with_name("media_identity.py").read_bytes()
        ).hexdigest()
    )
    assert "pypdf" in result["media_decoder_versions"]
    (repo / "document.pdf").write_bytes(pdf(DENIED))
    assert private_result(repo)["status"] == "fail"


@pytest.mark.parametrize(
    "name,body,reason",
    [
        ("wrong.pdf", b"public ordinary text", "format_mismatch"),
        ("wrong.mp3", pdf(), "format_mismatch"),
        (
            "unknown.mp4",
            b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00",
            "unverified" if FFMPEG and FFPROBE else "dependency_unavailable",
        ),
    ],
)
def test_declared_format_and_unknown_mp4_stop_real_entry(
    tmp_path, monkeypatch, name, body, reason
):
    repo = setup_scan(tmp_path, monkeypatch)
    (repo / name).write_bytes(body)
    result = private_result(repo)
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_media_" + reason


def test_history_blobs_apply_same_declared_format_dispatch(tmp_path, monkeypatch):
    repo = setup_scan(tmp_path, monkeypatch)
    (repo / "old.pdf").write_text("not a PDF")
    git(repo, "add", "old.pdf")
    git(repo, "commit", "-m", "Synthetic wrong-format history")
    (repo / "old.pdf").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-m", "Remove fixture")
    result = private_result(repo)
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_media_format_mismatch"


@requires_pdf
def test_media_neither_reuses_nor_records_clean_cache(tmp_path, monkeypatch):
    repo = setup_scan(tmp_path, monkeypatch)
    body = pdf()
    (repo / "document.pdf").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    calls = {"lookup": [], "record": []}

    class Cache:
        def lookup(self, key):
            calls["lookup"].append(key)
            return ()

        def record(self, key, value):
            calls["record"].append(key)

        def save(self):
            pass

    monkeypatch.setattr(MODULE.IdentityScanCache, "open", lambda *a, **kw: Cache())
    for _ in range(2):
        assert private_result(repo)["status"] == "pass"
    assert digest not in calls["lookup"]
    assert digest not in calls["record"]
    assert calls["lookup"]  # ordinary documents still use their cache
    (repo / "document.pdf").write_bytes(pdf(DENIED))
    assert private_result(repo)["status"] == "fail"


@requires_decoder
def test_mp3_actual_entry_decodes_and_missing_decoder_stops(tmp_path, monkeypatch):
    repo = setup_scan(tmp_path, monkeypatch)
    path = make_av(tmp_path, "mp3")
    (repo / "sound.mp3").write_bytes(path.read_bytes())
    assert private_result(repo)["status"] == "pass"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = private_result(repo)
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_media_dependency_unavailable"


def test_external_provider_never_certifies_media(tmp_path, monkeypatch):
    repo = setup_scan(tmp_path, monkeypatch)
    from identity_provider_fixture import configure_private_provider

    configure_private_provider(tmp_path, monkeypatch)
    scanner = MODULE.PrivateIdentityScan(repo, True)
    scanner.check(pdf(), source_names=("document.pdf",))
    assert scanner.result()["status"] == "tool_error"
    assert (
        scanner.result()["reason"]
        == "private_identity_media_external_provider_unsupported"
    )


@pytest.mark.parametrize("annotated", [False, True])
def test_tag_only_tree_keeps_media_extension_binding(tmp_path, monkeypatch, annotated):
    repo = setup_scan(tmp_path, monkeypatch)

    def raw(*args, data=None):
        return subprocess.run(
            ["git", *args], cwd=repo, input=data, capture_output=True, check=True
        ).stdout.strip()

    blob = raw("hash-object", "-w", "--stdin", data=b"Public ordinary text")
    tree = raw("mktree", data=b"100644 blob " + blob + b"\twrong.pdf\n")
    args = ["tag", "synthetic-tree"]
    if annotated:
        args += ["-a", "-m", "Synthetic tree tag"]
    raw(*args, tree.decode("ascii"))
    result = private_result(repo)
    assert result["status"] == "tool_error"
    assert result["reason"] == "private_identity_media_format_mismatch"


def test_utf16_bom_is_not_mpeg_layer_three_magic():
    assert MODULE.media_kind("Public text".encode("utf-16")) is None
    assert MODULE.media_kind(b"\xff\xfb\x90\x00") == "mp3"


@requires_decoder
@pytest.mark.parametrize("historical", [False, True])
def test_decoded_mp4_secret_reaches_existing_secret_gate(
    tmp_path, monkeypatch, historical
):
    from test_media_identity import inject_sei, supported_mp4

    # Synthetic credential is deliberately encoded inside SEI, absent raw UTF-8.
    repo = setup_scan(tmp_path, monkeypatch)
    fixture = supported_mp4.__wrapped__(tmp_path)
    marker = "ghp_" + "A" * 24
    encoded = marker.encode("utf-16-le")
    placeholder = "Q" * len(encoded)
    candidate = inject_sei(fixture, b"public-uuid-1234", placeholder, tmp_path)
    assert candidate.count(placeholder.encode()) == 1
    candidate = candidate.replace(placeholder.encode(), encoded)
    assert marker.encode() not in candidate
    target = repo / "video.mp4"
    target.write_bytes(candidate)
    if historical:
        git(repo, "add", "video.mp4")
        git(repo, "commit", "-m", "Synthetic encoded metadata")
        target.unlink()
        git(repo, "add", "-u")
        git(repo, "commit", "-m", "Remove synthetic video")
    checks = MODULE.scan(repo, require_private_identity=True)["checks"]
    assert checks["private_identity_scan"]["status"] == "pass"
    assert checks["secret_scan"]["status"] == "fail"
    assert checks["secret_scan"]["finding_count"] >= 1
