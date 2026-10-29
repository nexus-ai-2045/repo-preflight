"""一時 fixture の鍵と Git object で保護変更の判断 chain を検証する。"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from protected_change_decisions import protected_delta, verify_decisions

TRUST = ".repo-preflight/decision-trust.json"
BUNDLE = ".repo-preflight/decision-bundle.json"
PATHS = ["ci.yml", "policy.json"]
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
CTX = {
    "repository_id": 123,
    "repository": "fixture/project",
    "base_ref": "refs/heads/main",
    "pull_request": 7,
}


@pytest.mark.parametrize("expansion", ["digest", "design"])
def test_child_cannot_expand_an_exact_change_decision(fixture, expansion):
    f = fixture
    root, leaf = f[4:6]
    parent = copy.deepcopy(leaf)
    parent["decision_id"] = "exact-parent"
    parent["scope"]["protected_delta_sha256"] = "c" * 64
    leaf["parent_payload_sha256"] = hashlib.sha256(canon(parent)).hexdigest()
    nodes = [root, parent, leaf]
    if expansion == "design":
        branch = copy.deepcopy(root)
        branch["decision_id"] = "widened-design"
        branch["parent_payload_sha256"] = hashlib.sha256(canon(parent)).hexdigest()
        leaf["parent_payload_sha256"] = hashlib.sha256(canon(branch)).hexdigest()
        nodes.insert(2, branch)
    head = f[-1](nodes)
    assert verify(f, head)["status"] == "blocked"


def canon(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()


def git(repo, *args):
    return (
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )


def commit(repo, files):
    for name, value in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value if isinstance(value, bytes) else value.encode())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture", "--allow-empty")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    key = tmp_path / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    public = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    trust = {
        "schema": "repo-preflight.decision-trust/v1",
        **{k: CTX[k] for k in ("repository_id", "repository", "base_ref")},
        "max_validity_seconds": 86400 * 2,
        "signers": {"reviewer": public},
        "revoked_payload_sha256": [],
        "revoked_signers": [],
    }
    base = commit(repo, {TRUST: canon(trust), "ci.yml": "old", "policy.json": "{}"})
    head = commit(repo, {"ci.yml": "new"})

    def payload(kind, parent=None):
        return {
            "schema": "repo-preflight.protected-decision/v1",
            "decision_id": kind,
            "kind": kind,
            "signer": "reviewer",
            "purpose": "検査する変更の確認",
            "parent_payload_sha256": parent,
            "scope": {
                **CTX,
                "operation": "protected-change-check",
                "paths": PATHS,
                "protected_delta_sha256": (
                    protected_delta(repo, base, head, PATHS)
                    if kind == "change"
                    else None
                ),
                "policy_sha256": hashlib.sha256(canon(trust)).hexdigest(),
            },
            "issued_at": "2026-01-01T12:00:00Z",
            "expires_at": "2026-01-02T12:00:00Z",
        }

    design = payload("design")
    leaf = payload("change", hashlib.sha256(canon(design)).hexdigest())

    def signed(p, namespace="repo-preflight-protected-change"):
        raw = canon(p)
        source = tmp_path / "payload"
        source.write_bytes(raw)
        source.with_suffix(".sig").unlink(missing_ok=True)
        subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace, str(source)],
            capture_output=True,
            check=True,
        )
        return {
            "payload_base64": base64.b64encode(raw).decode(),
            "signature": source.with_suffix(".sig").read_text(),
        }

    def bundle(nodes=None, namespace="repo-preflight-protected-change"):
        return commit(
            repo,
            {
                BUNDLE: canon(
                    {
                        "schema": "repo-preflight.decision-bundle/v1",
                        "decisions": [
                            signed(p, namespace) for p in (nodes or [design, leaf])
                        ],
                    }
                )
            },
        )

    return repo, base, head, trust, design, leaf, signed, bundle


def verify(f, head=None, **kwargs):
    repo, base, _, _, _, _, _, bundle = f
    return verify_decisions(
        repo,
        base,
        head or bundle(),
        kwargs.pop("context", CTX),
        kwargs.pop("paths", PATHS),
        now=NOW,
        **kwargs,
    )


def test_valid_chain_and_nonprotected_rerun(fixture):
    f = fixture
    head = f[-1]()
    result = verify(f, head)
    assert result["status"] == "approved"
    assert result["decision_ids"] == ["design", "change"]
    assert result["assurance"] == "registered-signature"
    assert verify(f, head) == result
    newer = commit(f[0], {"README.md": "unprotected"})
    assert verify(f, newer) == result


def test_current_base_advancement_reuses_only_unchanged_authority_and_delta(fixture):
    f = fixture
    head = f[-1]()
    git(f[0], "switch", "-c", "current-base", f[1])
    current_base = commit(f[0], {"README.md": "unprotected base advancement"})
    result = verify_decisions(f[0], current_base, head, CTX, PATHS, NOW)
    assert result["status"] == "approved"
    trust = dict(f[3], revoked_signers=["reviewer"])
    revoked_base = commit(f[0], {TRUST: canon(trust)})
    assert (
        verify_decisions(f[0], revoked_base, head, CTX, PATHS, NOW)["status"]
        == "blocked"
    )


@pytest.mark.parametrize("mutation", ["bytes", "mode", "path"])
def test_protected_mutation_blocks(fixture, mutation):
    f = fixture
    f[-1]()
    if mutation == "bytes":
        head = commit(f[0], {"ci.yml": "altered"})
    elif mutation == "mode":
        os.chmod(f[0] / "ci.yml", 0o755)
        head = commit(f[0], {})
    else:
        head = commit(f[0], {"policy.json": "changed"})
    assert verify(f, head)["status"] == "blocked"


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "future",
        "expired",
        "missing",
        "wrongparent",
        "expandexpiry",
        "crossrepo",
        "crosspr",
        "crossbranch",
        "wrongdigest",
        "duplicateid",
        "designonly",
        "wrongnamespace",
    ],
)
def test_bad_chain_blocks(fixture, change):
    f = fixture
    design, leaf = f[4], f[5]
    nodes = [design, leaf]
    if change == "unknown":
        leaf["unknown"] = True
    elif change == "future":
        leaf["issued_at"] = "2026-01-03T00:00:00Z"
    elif change == "expired":
        leaf["expires_at"] = "2026-01-01T23:00:00Z"
    elif change == "missing":
        nodes = [leaf]
    elif change == "wrongparent":
        leaf["parent_payload_sha256"] = "a" * 64
    elif change == "expandexpiry":
        leaf["expires_at"] = "2026-01-03T00:00:00Z"
    elif change == "crossrepo":
        leaf["scope"]["repository"] = "other/project"
    elif change == "crosspr":
        leaf["scope"]["pull_request"] = 8
    elif change == "crossbranch":
        leaf["scope"]["base_ref"] = "refs/heads/other"
    elif change == "wrongdigest":
        leaf["scope"]["protected_delta_sha256"] = "b" * 64
    elif change == "duplicateid":
        leaf["decision_id"] = design["decision_id"]
    elif change == "designonly":
        nodes = [design]
    head = f[-1](
        nodes,
        namespace=(
            "wrong" if change == "wrongnamespace" else "repo-preflight-protected-change"
        ),
    )
    assert verify(f, head)["status"] == "blocked"


def test_head_trust_copy_is_ignored_but_complete_inventory_blocks(fixture):
    f = fixture
    f[-1]()
    trust = dict(f[3], signers={})
    head = commit(f[0], {TRUST: canon(trust)})
    # 低水準 API は受け取った inventory を検証する。実呼出元は必ず次の全件を渡す。
    assert verify(f, head)["status"] == "approved"
    assert verify(f, head, paths=PATHS + [TRUST])["status"] == "blocked"


@pytest.mark.parametrize(
    "carrier", ["empty", "oversize", "duplicate", "symlink", "forged"]
)
def test_bad_carrier_blocks(fixture, carrier):
    f = fixture
    if carrier == "empty":
        raw = canon({"schema": "repo-preflight.decision-bundle/v1", "decisions": []})
    elif carrier == "oversize":
        raw = b" " * (256 * 1024 + 1)
    elif carrier == "duplicate":
        raw = b'{"schema":"x","schema":"y","decisions":[]}'
    elif carrier == "forged":
        node = f[6](f[4])
        node["signature"] = node["signature"].replace("A", "B")
        raw = canon(
            {"schema": "repo-preflight.decision-bundle/v1", "decisions": [node]}
        )
    else:
        p = f[0] / BUNDLE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to("../ci.yml")
        head = commit(f[0], {})
        assert verify(f, head)["status"] == "blocked"
        return
    head = commit(f[0], {BUNDLE: raw})
    assert verify(f, head)["status"] == "blocked"


def test_unsorted_caller_paths_are_canonicalized(fixture):
    assert verify(fixture, paths=list(reversed(PATHS)))["status"] == "approved"


def test_carrier_only_is_rejected(fixture):
    f = fixture
    head = f[-1]()
    assert verify(f, head, paths=[BUNDLE])["reason"] == "no_protected_delta"


@pytest.mark.parametrize(
    "mutation",
    [
        "revokedkey",
        "revokedroot",
        "wrongkey",
        "unknowntrust",
        "boolid",
        "float",
        "commentkey",
        "duplicatetrust",
        "nonfinite",
        "unboundtrust",
    ],
)
def test_base_authority_is_strict(fixture, tmp_path, mutation):
    f = fixture
    trust = f[3]
    if mutation == "revokedkey":
        trust["revoked_signers"] = ["reviewer"]
    elif mutation == "revokedroot":
        trust["revoked_payload_sha256"] = [hashlib.sha256(canon(f[4])).hexdigest()]
    elif mutation == "wrongkey":
        key = tmp_path / "otherkey"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)],
            check=True,
            capture_output=True,
        )
        trust["signers"]["reviewer"] = " ".join(
            key.with_suffix(".pub").read_text().split()[:2]
        )
    elif mutation == "unknowntrust":
        trust["extra"] = 1
    elif mutation == "boolid":
        trust["repository_id"] = True
    elif mutation == "float":
        trust["max_validity_seconds"] = 1000.0
    elif mutation == "commentkey":
        trust["signers"]["reviewer"] += " comment"
    elif mutation == "unboundtrust":
        trust["repository"] = "other/project"
    raw = canon(trust)
    if mutation == "duplicatetrust":
        raw = raw[:-1] + b',"schema":"repo-preflight.decision-trust/v1"}'
    elif mutation == "nonfinite":
        raw = raw.replace(b"172800", b"NaN")
    base = commit(f[0], {TRUST: raw, "ci.yml": "old"})
    head = commit(f[0], {"ci.yml": "new"})
    # 取消を追加した trust は旧 payload の policy hash も失効させる。
    if mutation != "revokedroot":
        for p in (f[4], f[5]):
            p["scope"]["policy_sha256"] = hashlib.sha256(raw).hexdigest()
    f[5]["parent_payload_sha256"] = hashlib.sha256(canon(f[4])).hexdigest()
    f[5]["scope"]["protected_delta_sha256"] = protected_delta(f[0], base, head, PATHS)
    head = f[-1]()
    result = verify_decisions(f[0], base, head, CTX, PATHS, NOW)
    assert result["status"] == "blocked"


@pytest.mark.parametrize(
    "mutation",
    [
        "paths",
        "issued",
        "expires",
        "rootchange",
        "payloadbytes",
        "payloadunknown",
        "duplicatenode",
        "invalidpath",
        "badcontext",
    ],
)
def test_additional_chain_invariants(fixture, mutation):
    f = fixture
    root, leaf = f[4:6]
    if mutation == "paths":
        root["scope"]["paths"] = ["ci.yml"]
    elif mutation == "issued":
        root["issued_at"] = "2026-01-01T18:00:00Z"
    elif mutation == "expires":
        root["expires_at"] = "2026-01-02T06:00:00Z"
    elif mutation == "rootchange":
        root["kind"] = "change"
        root["scope"]["protected_delta_sha256"] = leaf["scope"][
            "protected_delta_sha256"
        ]
    elif mutation == "payloadunknown":
        root["scope"]["unknown"] = True
    elif mutation == "invalidpath":
        leaf["scope"]["paths"] = ["../ci.yml"]
    leaf["parent_payload_sha256"] = hashlib.sha256(canon(root)).hexdigest()
    if mutation in ("payloadbytes", "duplicatenode"):
        nodes = [f[6](root), f[6](leaf)]
        if mutation == "payloadbytes":
            leaf["purpose"] = "署名後の改変"
            nodes[1]["payload_base64"] = base64.b64encode(canon(leaf)).decode()
        else:
            nodes.append(nodes[0])
        head = commit(
            f[0],
            {
                BUNDLE: canon(
                    {"schema": "repo-preflight.decision-bundle/v1", "decisions": nodes}
                )
            },
        )
    else:
        head = f[-1]()
    assert (
        verify(
            f,
            head,
            context=dict(CTX, unknown=True) if mutation == "badcontext" else CTX,
        )["status"]
        == "blocked"
    )


def test_cycle_guard_with_synthetic_hashes(fixture, monkeypatch):
    # 実 SHA256 の固定点は作らず、署名済み payload の hash だけを置換して cycle を注入する。
    import protected_change_decisions as module

    f = fixture
    root, leaf = f[4:6]
    leaf["kind"] = "design"
    leaf["scope"]["protected_delta_sha256"] = None
    root["parent_payload_sha256"] = "b" * 64
    leaf["parent_payload_sha256"] = "a" * 64
    values = {canon(root): "a" * 64, canon(leaf): "b" * 64}
    original = hashlib.sha256

    class SyntheticDigest:
        def __init__(self, value):
            self.value = value

        def hexdigest(self):
            return self.value

    monkeypatch.setattr(
        module.hashlib,
        "sha256",
        lambda raw: SyntheticDigest(values[raw]) if raw in values else original(raw),
    )
    assert verify(f)["reason"] == "decision_cycle"


def test_missing_ssh_keygen_is_blocked(fixture, monkeypatch):
    import protected_change_decisions as module

    head = fixture[-1]()
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    assert verify(fixture, head)["reason"] == "ssh_keygen_missing"


@pytest.mark.parametrize("operation", ["add", "delete", "rename"])
def test_changed_protected_inventory_detects_add_delete_rename(fixture, operation):
    f = fixture
    signed_head = f[-1]()
    paths = list(PATHS)
    if operation == "add":
        paths.append("extra-ci.yml")
        head = commit(f[0], {"extra-ci.yml": "new protected file"})
    elif operation == "delete":
        (f[0] / "ci.yml").unlink()
        head = commit(f[0], {})
    else:
        paths.append("moved-ci.yml")
        (f[0] / "ci.yml").rename(f[0] / "moved-ci.yml")
        head = commit(f[0], {})
    assert protected_delta(f[0], f[1], signed_head, PATHS) != protected_delta(
        f[0], f[1], head, sorted(paths)
    )
    assert verify(f, head, paths=sorted(paths))["status"] == "blocked"
