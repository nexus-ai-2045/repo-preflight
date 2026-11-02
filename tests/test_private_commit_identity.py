"""Private author metadata consent never approves message, payload or public exposure."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from synthetic_paths import synthetic_home_path

from test_private_submission_scope import submission, commit
from test_readiness_scan import MODULE, git

IDENTITY = "Fixture Private Author <fixture-person@example.invalid>"


@pytest.fixture
def approved(submission, monkeypatch):
    repo, base, oid, calls = submission
    git(repo, "config", "user.name", "Fixture Private Author")
    git(repo, "config", "user.email", "fixture-person@example.invalid")
    monkeypatch.delenv("PUBLIC_IDENTITY_GUARD_SCRIPT", raising=False)
    policy = repo.parent / "private-author-policy.txt"
    policy.write_text("Fixture Private Author\nfixture-person@example.invalid\n")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {
                "allow_names": ["Fixture Private Author"],
                "allow_emails": ["fixture-person@example.invalid"],
                "reject_all_coauthors": True,
            }
        ),
    )
    return repo, base, oid, calls


def scan(approved, **overrides):
    repo, base, _, _ = approved
    args = dict(
        base_ref=base,
        private_submission=True,
        expected_identity=IDENTITY,
        require_private_identity=True,
        allow_expected_private_commit_identity=True,
    )
    args.update(overrides)
    return MODULE.scan(repo, **args)


def test_exact_private_identity_is_scoped_and_raw_commit_unchanged(approved):
    repo, _, _, _ = approved
    commit(repo)
    oid = MODULE.run(repo, "git", "rev-parse", "HEAD")[1]
    before = MODULE._identity_git_raw(repo, "cat-file", "commit", oid)
    report = scan(approved)
    assert report["status"] == "pass"
    identity = report["checks"]["private_identity_scan"]
    assert identity["approved_private_commit_identity_fields"] == 2
    assert (
        identity["approved_private_commit_identity_sha256"]
        == hashlib.sha256(IDENTITY.encode()).hexdigest()
    )
    assert IDENTITY not in json.dumps(report)
    assert MODULE.run(repo, "git", "rev-parse", "HEAD")[1] == oid
    assert MODULE._identity_git_raw(repo, "cat-file", "commit", oid) == before


def test_default_private_scan_still_denies_identity(approved):
    commit(approved[0])
    report = scan(approved, allow_expected_private_commit_identity=False)
    assert report["status"] == "blocked"
    assert report["checks"]["private_identity_scan"]["status"] == "fail"


@pytest.mark.parametrize(
    "overrides",
    [
        {"private_submission": False},
        {"expected_identity": None},
        {"base_ref": None},
        {"release": True},
    ],
)
def test_scope_without_private_submission_or_identity_is_rejected(approved, overrides):
    commit(approved[0])
    assert scan(approved, **overrides)["status"] == "tool_error"


@pytest.mark.parametrize("visibility", ["public", "internal", None])
def test_nonprivate_remote_never_receives_metadata_permission(
    approved, monkeypatch, visibility
):
    commit(approved[0])

    def api(endpoint):
        return {"full_name": "example/repo", "visibility": visibility, "private": False}

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=api)
    )
    report = scan(approved)
    assert report["status"] == "tool_error"


@pytest.mark.parametrize("failure", ["api", "repository", "base", "push"])
def test_live_binding_failures_are_closed(approved, monkeypatch, failure):
    repo, _, oid, _ = approved
    commit(repo)
    if failure == "push":
        git(
            repo,
            "remote",
            "set-url",
            "--push",
            "origin",
            "https://github.com/other/repo.git",
        )

    def api(endpoint):
        if failure == "api":
            raise RuntimeError("synthetic unavailable")
        if "/git/ref/" in endpoint:
            return {
                "ref": "refs/heads/main",
                "object": {
                    "type": "commit",
                    "sha": "0" * 40 if failure == "base" else oid,
                },
            }
        return {
            "full_name": "other/repo" if failure == "repository" else "example/repo",
            "private": True,
            "visibility": "private",
        }

    monkeypatch.setattr(
        MODULE, "load_github_settings_module", lambda: SimpleNamespace(gh_api_get=api)
    )
    assert scan(approved)["status"] == "tool_error"


@pytest.mark.parametrize(
    "where", ["blob", "filename", "message", "coauthor", "unknown-header"]
)
def test_metadata_permission_does_not_approve_other_locations(approved, where):
    repo, _, _, _ = approved
    marker = "Fixture Private Author"
    commit(
        repo,
        name=marker + ".txt" if where == "filename" else "change.txt",
        body=marker if where == "blob" else "public change\n",
    )
    if where in {"message", "coauthor", "unknown-header"}:
        oid = MODULE.run(repo, "git", "rev-parse", "HEAD")[1]
        raw = MODULE._identity_git_raw(repo, "cat-file", "commit", oid)
        header, body = raw.split("\n\n", 1)
        if where == "unknown-header":
            header += "\nprivate-note " + marker
        elif where == "message":
            body += marker
        else:
            body += "\nCo-authored-by: " + IDENTITY + "\n"
        import subprocess

        new_oid = (
            subprocess.check_output(
                ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
                cwd=repo,
                input=(header + "\n\n" + body).encode(),
            )
            .decode()
            .strip()
        )
        git(repo, "update-ref", "HEAD", new_oid)
    assert scan(approved)["status"] != "pass"


@pytest.mark.parametrize("role", ["author", "committer"])
def test_one_identity_mismatch_and_same_name_other_email_stay_blocked(approved, role):
    repo, _, _, _ = approved
    commit(repo)
    oid = MODULE.run(repo, "git", "rev-parse", "HEAD")[1]
    raw = MODULE._identity_git_raw(repo, "cat-file", "commit", oid)
    altered = raw.replace(
        role + " " + IDENTITY,
        role + " Fixture Private Author <other@example.invalid>",
        1,
    )
    import subprocess

    new_oid = (
        subprocess.check_output(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=repo,
            input=altered.encode(),
        )
        .decode()
        .strip()
    )
    git(repo, "update-ref", "HEAD", new_oid)
    assert scan(approved)["status"] != "pass"


@pytest.mark.parametrize(
    "payload",
    [
        "TOKEN=sk-" + "A" * 48,
        'PATH="' + synthetic_home_path("linux", "fixture-user/work") + '"',
    ],
)
def test_raw_commit_secret_and_path_survive_metadata_view(approved, payload):
    repo, _, _, _ = approved
    commit(repo)
    git(repo, "commit", "--allow-empty", "-m", payload)
    report = scan(approved)
    assert report["status"] != "pass"
    assert (
        report["checks"]["secret_scan"]["status"] == "fail"
        or report["checks"]["personal_path_scan"]["status"] == "fail"
    )


@pytest.mark.parametrize(
    "mutation", ["duplicate", "continuation", "timezone", "control"]
)
def test_projection_reuses_strict_header_parser(approved, mutation):
    repo, _, _, _ = approved
    commit(repo)
    raw = MODULE._identity_git_raw(repo, "cat-file", "commit", "HEAD")
    if mutation == "duplicate":
        raw = raw.replace(
            "\ncommitter ", "\nauthor " + IDENTITY + " 0 +0000\ncommitter ", 1
        )
    elif mutation == "continuation":
        raw = raw.replace("\ncommitter ", "\n continuation\ncommitter ", 1)
    elif mutation == "timezone":
        raw = (
            raw.replace(" +0000", " +2460")
            if " +0000" in raw
            else raw.replace(" +0900", " +2460")
        )
    else:
        raw = raw.replace("Fixture Private Author", "Fixture\rPrivate Author", 1)
    with pytest.raises(RuntimeError):
        MODULE.private_commit_identity_view(raw.encode(), IDENTITY)


def test_cli_option_reaches_private_scan_and_dialogue(approved, monkeypatch):
    repo, base, _, _ = approved
    commit(repo)
    args = MODULE.build_parser().parse_args(
        [
            "--repo",
            str(repo),
            "--intent",
            "open_pr",
            "--private-submission",
            "--base-ref",
            base,
            "--expected-identity",
            IDENTITY,
            "--allow-expected-private-commit-identity",
        ]
    )
    options = MODULE.resolve_options(args, stdin_is_tty=False)
    assert options.allow_expected_private_commit_identity is True
    seen = {}

    def checked(repo, **kwargs):
        seen.update(kwargs)
        return {"status": "pass", "checks": {}}

    monkeypatch.setattr(MODULE, "scan", checked)
    MODULE.build_intent_dialogue(options)
    assert seen["allow_expected_private_commit_identity"] is True


def test_typed_policy_must_independently_allow_identity(approved, monkeypatch):
    commit(approved[0])
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {
                "allow_names": ["Another Author"],
                "allow_emails": ["other@example.invalid"],
            }
        ),
    )
    report = scan(approved)
    assert report["status"] != "pass"
    assert report["checks"]["identity_gate"]["status"] == "fail"
    assert (
        "approved_private_commit_identity_fields"
        not in report["checks"]["private_identity_scan"]
    )


@pytest.mark.parametrize(
    "name", ["sk-" + "A" * 48, synthetic_home_path("linux", "fixture-user/work")]
)
def test_approved_identity_itself_cannot_hide_secret_or_path(
    approved, monkeypatch, name
):
    repo, _, _, _ = approved
    git(repo, "config", "user.name", name)
    monkeypatch.setenv(
        "PREFLIGHT_PRIVATE_PATTERNS",
        json.dumps(
            {"allow_names": [name], "allow_emails": ["fixture-person@example.invalid"]}
        ),
    )
    commit(repo)
    report = scan(
        approved, expected_identity=name + " <fixture-person@example.invalid>"
    )
    assert report["status"] != "pass"
    assert (
        report["checks"]["secret_scan"]["status"] == "fail"
        or report["checks"]["personal_path_scan"]["status"] == "fail"
    )


@pytest.mark.parametrize(
    "arguments", [[], ["--private-submission"], ["--expected-identity", IDENTITY]]
)
def test_cli_optin_rejects_missing_private_scope(approved, arguments):
    repo, _, _, _ = approved
    args = MODULE.build_parser().parse_args(
        [
            "--repo",
            str(repo),
            "--intent",
            "open_pr",
            "--allow-expected-private-commit-identity",
            *arguments,
        ]
    )
    with pytest.raises(SystemExit):
        MODULE.resolve_options(args, stdin_is_tty=False)


def test_cli_main_runs_real_scanner_and_emits_only_hashed_identity(approved):
    from io import StringIO

    repo, base, _, _ = approved
    commit(repo)
    output = StringIO()
    MODULE.main(
        [
            "--repo",
            str(repo),
            "--intent",
            "open_pr",
            "--private-submission",
            "--base-ref",
            base,
            "--expected-identity",
            IDENTITY,
            "--allow-expected-private-commit-identity",
        ],
        stdin_is_tty=False,
        stdout=output,
        console=StringIO(),
    )
    result = json.loads(output.getvalue())
    assert result["scan"]["status"] == "pass"
    assert result["scan"]["options"]["allow_expected_private_commit_identity"] is True
    assert (
        result["scan"]["checks"]["private_identity_scan"][
            "approved_private_commit_identity_fields"
        ]
        == 2
    )
    assert IDENTITY not in output.getvalue()


def test_typed_policy_uses_fixed_head_and_moving_head_cannot_pass(
    approved, monkeypatch
):
    repo, _, base_oid, _ = approved
    commit(repo)
    captured = MODULE.run(repo, "git", "rev-parse", "HEAD")[1]
    original = MODULE._identity_gate_result
    observed = []

    def moving(repo_arg, base_ref, *, head_ref="HEAD"):
        observed.append((base_ref, head_ref))
        result = original(repo_arg, base_ref, head_ref=head_ref)
        git(repo, "commit", "--allow-empty", "-m", "synthetic concurrent edit")
        return result

    monkeypatch.setattr(MODULE, "_identity_gate_result", moving)
    result = scan(approved)
    assert observed == [(base_oid, captured)]
    assert result["status"] == "tool_error"
    assert "private_submission_local_refs_changed" in result["issues"]


def test_non_ascii_identity_preserves_utf16_unknown_header_detection(
    approved, monkeypatch
):
    repo, _, _, _ = approved
    commit(repo)
    expected = "Éxample <fixture@example.invalid>"
    raw = (
        MODULE._identity_git_raw(repo, "cat-file", "commit", "HEAD")
        .replace(IDENTITY, expected)
        .encode("utf-8")
    )
    prefix, suffix = raw.split(b"committer ", 1)
    header = b"notes "
    if (len(prefix) + len(header)) % 2:
        header += b" "
    hidden = "HiddenPerson".encode("utf-16-le")
    data = prefix + header + hidden + b"\ncommitter " + suffix
    if len(data) % 2:
        data += b" "
    policy = repo.parent / "unicode-private-policy.txt"
    policy.write_text("HiddenPerson\n")
    monkeypatch.setenv("PUBLIC_IDENTITY_DENYLIST", str(policy))
    before = MODULE.PrivateIdentityScan(repo, True)
    before.check(data)
    assert before.result()["status"] == "fail"
    view = MODULE.private_commit_identity_view(data, expected)
    assert len(view) == len(data)
    assert view.index(hidden) == data.index(hidden)
    after = MODULE.PrivateIdentityScan(repo, True)
    after.check(view)
    assert after.result()["status"] == "fail"
    assert view.split(b"\n\n", 1)[1] == data.split(b"\n\n", 1)[1]
