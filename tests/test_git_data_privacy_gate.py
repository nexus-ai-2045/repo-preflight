"""外部policy・bare objectだけを使う合成回帰検査。実際の個人値は読まない。"""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "git_data_privacy_gate.py"
SPEC = importlib.util.spec_from_file_location("git_data_privacy_gate", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
CANARY = "synthetic-private-token"
EMAIL = "public-bot@example.invalid"
NAME = "Synthetic Public Bot"
# 合成値を分割し、テストsource自体を公開secret/path検出に一致させない。
PUBLIC_SECRET = "ghp_" + "SyntheticCredentialNeverValid1234567890"
PUBLIC_PATH = chr(47).join(("", "Users", "synthetic-person", "private.txt"))


class Store:
    def __init__(self, root):
        self.repo = root / "store.git"
        self.repo.mkdir()
        template = root / "empty-template"
        template.mkdir()
        self.env = GATE._git_env()
        self.env.update(
            GIT_AUTHOR_NAME=NAME,
            GIT_AUTHOR_EMAIL=EMAIL,
            GIT_COMMITTER_NAME=NAME,
            GIT_COMMITTER_EMAIL=EMAIL,
            GIT_AUTHOR_DATE="2026-01-01T00:00:00+00:00",
            GIT_COMMITTER_DATE="2026-01-01T00:00:00+00:00",
        )
        self.git("init", "--bare", "--template=" + str(template))
        self.denylist = root / "deny.txt"
        self.denylist.write_text(CANARY + "\n", encoding="utf-8")
        self.policy = root / "identity.json"
        self.policy.write_text(
            json.dumps(
                {
                    "allow_names": [NAME],
                    "allow_emails": [EMAIL],
                    "deny_patterns": [CANARY],
                }
            ),
            encoding="utf-8",
        )
        self.head = self.commit(b"clean\n")

    def git(self, *args, data=None, env=None):
        return subprocess.run(
            ["git", "--git-dir=" + str(self.repo), *args],
            env=self.env if env is None else env,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout

    def blob(self, data):
        return self.git("hash-object", "-w", "--stdin", data=data).decode().strip()

    def commit(self, data, parent=None, env=None, message=b"synthetic commit\n"):
        if data is None:
            tree_data = b""
        else:
            oid = self.blob(data)
            tree_data = f"100644 blob {oid}\tpayload.py\n".encode()
        tree = self.git("mktree", data=tree_data).decode().strip()
        parents = ["-p", parent] if parent else []
        head = (
            self.git("commit-tree", tree, *parents, data=message, env=env)
            .decode()
            .strip()
        )
        self.git("update-ref", "refs/heads/main", head)
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        return head

    def scan(self, **kwargs):
        result = GATE.scan_store(
            self.repo,
            self.denylist,
            self.policy,
            kwargs.pop("expected_head", self.head),
            **kwargs,
        )
        self.assert_safe(result)
        return result

    def commit_files(self, files, parent):
        nested = {}
        for path, data in files.items():
            directory = nested
            parts = path.split("/")
            for part in parts[:-1]:
                directory = directory.setdefault(part, {})
            directory[parts[-1]] = data

        def tree(directory):
            entries = []
            for name, value in sorted(directory.items()):
                if isinstance(value, dict):
                    entries.append(f"040000 tree {tree(value)}\t{name}\n")
                else:
                    entries.append(f"100644 blob {self.blob(value)}\t{name}\n")
            return self.git("mktree", data="".join(entries).encode()).decode().strip()

        head = (
            self.git(
                "commit-tree", tree(nested), "-p", parent, data=b"synthetic files\n"
            )
            .decode()
            .strip()
        )
        self.git("update-ref", "refs/heads/main", head)
        return head

    def assert_safe(self, result):
        text = json.dumps(result)
        for value in (
            CANARY,
            EMAIL,
            NAME,
            str(self.repo),
            str(self.policy),
            "payload.py",
            PUBLIC_SECRET,
            PUBLIC_PATH,
        ):
            assert value not in text
        assert result["schema"] == GATE.SCHEMA


@pytest.fixture
def store(tmp_path, monkeypatch):
    for key in (
        "PUBLIC_IDENTITY_GUARD_SCRIPT",
        "PUBLIC_IDENTITY_DENYLIST",
        "PREFLIGHT_PRIVATE_PATTERNS_FILE",
        "PREFLIGHT_PRIVATE_PATTERNS",
    ):
        monkeypatch.delenv(key, raising=False)
    return Store(tmp_path)


def test_clean_pass_and_hashes(store):
    report = store.scan()
    assert report["status"] == "pass"
    assert report["object_count"] == 3
    assert report["ref_count"] == 2
    assert report["byte_count"] > 0
    assert report["expected_head"] == store.head
    assert report["finding_counts"] == {
        "private_text": 0,
        "identity": 0,
        "public_secret": 0,
        "public_path": 0,
    }
    assert len(report["hashes"]) == 6
    assert report["scope"]["not_covered"] == [
        "image_pixels",
        "encrypted_content",
        "unsupported_containers",
    ]


def test_deleted_history_is_blocked(store):
    bad = store.commit(CANARY.encode(), parent=store.head)
    store.head = store.commit(None, parent=bad)
    assert store.scan()["status"] == "blocked"


@pytest.mark.parametrize(
    "workflow", [b"", b"jobs:\n  noop:\n    steps:\n      - run: true\n"]
)
@pytest.mark.parametrize(
    "value,kind", [(PUBLIC_SECRET, "public_secret"), (PUBLIC_PATH, "public_path")]
)
def test_public_findings_in_deleted_history_block_despite_noop_ci(
    store, workflow, value, kind
):
    bad = store.commit(value.encode(), parent=store.head)
    store.head = store.commit_files(
        {".github/workflows/public-preflight.yml": workflow}, parent=bad
    )
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["finding_counts"][kind] == 1
    assert report["finding_counts"]["private_text"] == (kind == "public_path")
    assert report["finding_counts"]["identity"] == 0
    assert report["reason_codes"] == (
        (["private_text_detected"] if kind == "public_path" else [])
        + [kind + "_detected"]
    )


@pytest.mark.parametrize(
    "payload,kind",
    [
        (PUBLIC_SECRET.encode("utf-16"), "public_secret"),
        (("%67" + PUBLIC_SECRET[1:]).encode(), "public_secret"),
        (PUBLIC_PATH.encode("utf-16"), "public_path"),
        (PUBLIC_PATH.replace("/", "%2F").encode(), "public_path"),
        (('"' + PUBLIC_PATH.replace("/", r"\u002f") + '"').encode(), "public_path"),
    ],
)
def test_public_findings_use_existing_decoded_formats(store, payload, kind):
    store.head = store.commit(payload, parent=store.head)
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["finding_counts"][kind] >= 1
    assert report["reason_codes"] == (
        (["private_text_detected"] if kind == "public_path" else [])
        + [kind + "_detected"]
    )


@pytest.mark.parametrize("location", ["blob", "commit", "tree", "tag", "ref", "head"])
def test_public_secret_scans_all_storage_locations(store, location):
    if location == "blob":
        store.blob(PUBLIC_SECRET.encode())
    elif location == "commit":
        store.head = store.commit(b"clean", message=PUBLIC_SECRET.encode())
    elif location == "tree":
        store.head = store.commit_files({PUBLIC_SECRET: b"clean"}, parent=store.head)
    elif location == "tag":
        tag = f"object {store.head}\ntype commit\ntag synthetic\ntagger {NAME} <{EMAIL}> 1767225600 +0000\n\n{PUBLIC_SECRET}\n".encode()
        store.git("hash-object", "-t", "tag", "-w", "--stdin", data=tag)
    elif location == "ref":
        store.git("update-ref", "refs/tags/" + PUBLIC_SECRET, store.head)
    else:
        store.git("symbolic-ref", "HEAD", "refs/heads/" + PUBLIC_SECRET)
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["finding_counts"]["public_secret"] == 1
    assert report["reason_codes"] == ["public_secret_detected"]


def test_candidate_baseline_and_matching_exception_cannot_waive_public_secret(store):
    payload = PUBLIC_SECRET.encode()
    digest = hashlib.sha256(payload).hexdigest()
    exception = {
        "version": 1,
        "entries": [
            {
                "id": "synthetic-reviewed-token",
                "repo": "synthetic/project",
                "path": "payload.py",
                "content_sha256": digest,
                "rule": "github_token",
                "match_sha256": digest,
                "occurrence_count": 1,
                "review_reason": "synthetic regression fixture",
            }
        ],
    }
    store.head = store.commit_files(
        {
            "payload.py": payload,
            ".repo-preflight-baseline.json": json.dumps({"findings": []}).encode(),
            "config/reviewed-secret-exceptions.json": json.dumps(exception).encode(),
            ".github/workflows/public-preflight.yml": b"jobs: {}\n",
        },
        parent=store.head,
    )
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["reason_codes"] == ["public_secret_detected"]
    assert report["finding_counts"]["public_secret"] == 1
    assert digest not in json.dumps(report)


@pytest.mark.parametrize(
    "helper", ["secret_matches", "normalized_personal_path_matches"]
)
def test_public_helper_failure_fails_closed_without_values(store, monkeypatch, helper):
    load = GATE._load

    def failing(data, **kwargs):
        raise RuntimeError(PUBLIC_SECRET + PUBLIC_PATH)

    def patched_load(name):
        module = load(name)
        if name == "readiness_scan":
            setattr(module, helper, failing)
        return module

    monkeypatch.setattr(GATE, "_load", patched_load)
    report = store.scan()
    assert report["status"] == "tool_error"
    assert report["reason_codes"] == ["scan_input_or_execution_failed"]


@pytest.mark.parametrize(
    "payload",
    [
        CANARY.encode("utf-16"),
        ('"\\u0073ynthetic-private-token"').encode(),
        ('"%73ynthetic-private-token"').encode(),
    ],
)
def test_encoded_markers_blocked(store, payload):
    store.head = store.commit(payload, parent=store.head)
    assert store.scan()["status"] == "blocked"


def test_ref_names_are_scanned(store):
    store.git("update-ref", "refs/tags/" + CANARY, store.head)
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["finding_counts"]["private_text"] >= 1


def test_symbolic_head_name_is_scanned(store):
    store.git("symbolic-ref", "HEAD", "refs/heads/" + CANARY)
    assert store.scan()["status"] == "blocked"


def test_unreachable_objects_and_tag_bytes_scanned(store):
    store.blob(CANARY.encode())
    tag = f"object {store.head}\ntype commit\ntag synthetic\ntagger {NAME} <{EMAIL}> 1767225600 +0000\n\n{CANARY}\n".encode()
    store.git("hash-object", "-t", "tag", "-w", "--stdin", data=tag)
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["object_count"] == 5


@pytest.mark.parametrize(
    "field,value",
    [
        ("GIT_AUTHOR_EMAIL", "forbidden@example.invalid"),
        ("GIT_COMMITTER_NAME", "REDACTED"),
        ("GIT_AUTHOR_NAME", "Unapproved Synthetic Author"),
    ],
)
def test_metadata_policy_blocks(store, field, value):
    store.head = store.commit(b"clean\n", env={**store.env, field: value})
    report = store.scan()
    assert report["status"] == "blocked"
    assert report["finding_counts"]["identity"] > 0
    assert value not in json.dumps(report)


def test_commit_body_identity_check(store):
    store.head = store.commit(
        b"clean", message=f"msg\n\nSigned-off-by: REDACTED <{EMAIL}>\n".encode()
    )
    assert store.scan()["status"] == "blocked"


@pytest.mark.parametrize(
    "kind", ["missing", "empty", "invalid", "inside", "symlink_inside"]
)
@pytest.mark.parametrize("target", ["denylist", "policy"])
def test_invalid_policy_fail_closed(store, kind, target):
    path = getattr(store, target)
    if kind == "missing":
        path.unlink()
    elif kind == "empty":
        path.write_text("")
    elif kind == "invalid":
        path.write_text("# comments only\n" if target == "denylist" else "{bad JSON}")
    else:
        inside = store.repo / "candidate-policy"
        inside.write_bytes(path.read_bytes())
        if kind == "inside":
            setattr(store, target, inside)
        else:
            path.unlink()
            path.symlink_to(inside)
    assert store.scan()["status"] == "tool_error"


@pytest.mark.parametrize("head", ["bad-private-input", "0" * 40, "--all", "", None])
def test_invalid_or_absent_expected_head(store, head):
    report = store.scan(expected_head=head)
    assert report["status"] == "tool_error"
    assert "bad-private-input" not in json.dumps(report)


def test_blob_expected_head_fails(store):
    report = store.scan(expected_head=store.blob(b"clean"))
    assert report["status"] == "tool_error"
    assert report["reason_codes"] == ["expected_head_not_commit"]


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"max_objects": 2}, "object_count_limit"),
        ({"max_object_bytes": 1}, "object_size_limit"),
        ({"max_total_bytes": 1}, "total_bytes_limit"),
        ({"max_objects": 0}, "limits_invalid"),
        ({"max_objects": True}, "limits_invalid"),
    ],
)
def test_limits_fail_closed_before_bodies(store, kwargs, reason):
    report = store.scan(**kwargs)
    assert report["status"] == "tool_error"
    assert report["reason_codes"] == [reason]
    assert report["object_count"] == 0


def test_candidate_python_and_provider_never_execute(store, tmp_path, monkeypatch):
    sentinel = tmp_path / "executed"
    monkeypatch.setenv("SYNTHETIC_EXECUTION_MARKER", str(sentinel))
    payload = (
        b"import os\nfrom pathlib import Path\n"
        b"Path(os.environ['SYNTHETIC_EXECUTION_MARKER']).touch()\n"
    )
    store.head = store.commit(payload)
    assert store.scan()["status"] == "pass"
    assert not sentinel.exists()
    provider = tmp_path / "provider.py"
    provider.write_bytes(payload)
    monkeypatch.setenv("PUBLIC_IDENTITY_GUARD_SCRIPT", str(provider))
    report = store.scan()
    assert report["status"] == "tool_error"
    assert report["reason_codes"] == ["external_provider_rejected"]
    assert not sentinel.exists()


def test_replace_ref_cannot_hide_original(store):
    bad = store.blob(CANARY.encode())
    clean = store.blob(b"clean")
    store.git("update-ref", "refs/replace/" + bad, clean)
    assert store.scan()["status"] == "blocked"


def test_environment_is_neutralized_and_restored(store, monkeypatch, tmp_path):
    poison = tmp_path / "poison-config"
    poison.write_text("invalid config " + CANARY)
    values = {
        "GIT_DIR": str(tmp_path / "absent"),
        "GIT_WORK_TREE": str(tmp_path),
        "GIT_CONFIG_GLOBAL": str(poison),
        "GIT_CONFIG_SYSTEM": str(poison),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "false",
        "PUBLIC_IDENTITY_DENYLIST": "invalid-original",
        "PREFLIGHT_PRIVATE_PATTERNS": "invalid-original",
        "PREFLIGHT_PRIVATE_PATTERNS_FILE": "invalid-original",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert store.scan()["status"] == "pass"
    assert all(os.environ[key] == value for key, value in values.items())
    store.policy.write_text("invalid")
    assert store.scan()["status"] == "tool_error"
    assert all(os.environ[key] == value for key, value in values.items())


def test_no_candidate_config_as_policy_or_hooks(store, tmp_path):
    marker = tmp_path / "hook-executed"
    hook = tmp_path / "monitor"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    store.git("config", "core.fsmonitor", str(hook))
    store.git("config", "log.showSignature", "true")
    store.head = store.commit(
        b'{"allow_emails": ["forbidden@example.invalid"]}',
        env={**store.env, "GIT_AUTHOR_EMAIL": "forbidden@example.invalid"},
    )
    assert store.scan()["status"] == "blocked"
    assert not marker.exists()


def test_nonbare_store_rejected(store):
    store.git("config", "core.bare", "false")
    assert store.scan()["status"] == "tool_error"


def test_missing_reachable_blob_fails_closed(store):
    oid = store.blob(b"clean\n")
    missing = store.repo / "objects" / oid[:2] / oid[2:]
    # Git object files are readonly on Windows; this disposable corruption fixture
    # must clear the readonly flag before removing the object.
    missing.chmod(0o600)
    missing.unlink()
    report = store.scan()
    assert report["status"] == "tool_error"
    assert report["reason_codes"] in (
        ["reachable_object_unscanned"],
        ["git_read_failed"],
    )


@pytest.mark.parametrize(
    "relative",
    [
        "objects/info/alternates",
        "objects/info/http-alternates",
        "info/grafts",
        "shallow",
    ],
)
def test_external_or_incomplete_store_rejected(store, relative):
    path = store.repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    assert store.scan()["status"] == "tool_error"


def test_cli_errors_are_sanitized(store):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--unexpected", CANARY],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not result.stderr
    assert CANARY not in result.stdout
    assert json.loads(result.stdout)["status"] == "tool_error"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert not result.stderr


def test_cli_clean_and_blocked_exit(store):
    args = [
        sys.executable,
        str(SCRIPT),
        "--repo",
        str(store.repo),
        "--denylist",
        str(store.denylist),
        "--identity-policy",
        str(store.policy),
        "--expected-head",
        store.head,
    ]
    for code in (0, 1):
        if code:
            store.blob(CANARY.encode())
        result = subprocess.run(args, capture_output=True, text=True)
        assert result.returncode == code
        assert not result.stderr
        store.assert_safe(json.loads(result.stdout))


@pytest.mark.parametrize("role", ["author", "committer"])
def test_raw_identity_delimiters_cannot_shift_unauthorized_email_into_body(store, role):
    tree = store.git("mktree", data=b"").decode().strip()
    foreign = "foreign-identity@example.invalid"
    injected = (
        (NAME + "\x1f" + EMAIL + "\x1f" + NAME + "\x1f" + EMAIL)
        if role == "author"
        else (NAME + "\x1f" + EMAIL)
    )
    normal = f"{NAME} <{EMAIL}> 1767225600 +0000"
    attacker = f"{injected} <{foreign}> 1767225600 +0000"
    raw = (
        f"tree {tree}\nauthor {attacker if role == 'author' else normal}\n"
        f"committer {attacker if role == 'committer' else normal}\n\nordinary body\n"
    ).encode()
    store.head = (
        store.git("hash-object", "-t", "commit", "-w", "--stdin", data=raw)
        .decode()
        .strip()
    )
    store.git("update-ref", "refs/heads/main", store.head)
    report = store.scan()
    assert report["status"] == "tool_error"
    assert report["finding_counts"]["private_text"] == 0
    assert foreign not in json.dumps(report)
