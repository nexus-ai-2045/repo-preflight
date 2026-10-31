from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "consistency_gate.py"
SPEC = importlib.util.spec_from_file_location("consistency_gate", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text(
        "# Demo\n\n[guide](docs/guide.md)\n\n`python src/app.py`\n",
        encoding="utf-8",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("guide\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "generated.txt").write_text("generated\n", encoding="utf-8")
    digest = hashlib.sha256((repo / "generated.txt").read_bytes()).hexdigest()
    config = {
        "schema": "repo-preflight.consistency/v1",
        "mode": "shadow",
        "markdown": {"include": ["README.md", "docs/**/*.md"]},
        "readme_contracts": {
            "required_paths": ["src/app.py"],
            "commands": [{"text": "python src/app.py", "paths": ["src/app.py"]}],
        },
        "impact_map": [
            {"change": ["src/**"], "requires_any": ["README.md", "docs/**"]}
        ],
        "generated_artifacts": [
            {"path": "generated.txt", "sources": ["src/**"], "sha256": digest}
        ],
    }
    (repo / ".repo-preflight-consistency.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    return repo, base


def test_passes_complete_contract(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    assert MODULE.check(repo, base_ref=base)["status"] == "pass"


def portable_repo(tmp_path, value, *, pointers=None, allow_home_relative=False):
    repo, base = make_repo(tmp_path)
    (repo / "paths.json").write_text(json.dumps({"path": value}), encoding="utf-8")
    git(repo, "add", "paths.json")
    config_path = repo / MODULE.CONFIG_NAME
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "enforce"
    config["portable_paths"] = [
        {
            "path": "paths.json",
            "pointers": pointers or ["/path"],
            "allow_home_relative": allow_home_relative,
        }
    ]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return repo, base


@pytest.mark.parametrize(
    "value,reason",
    [
        ("C:/project/draft.md", "absolute"),
        (r"D:\project\draft.md", "absolute"),
        ("C:draft.md", "absolute"),
        (r"\\server\share\draft.md", "absolute"),
        ("//server/share/draft.md", "absolute"),
        (r"\project\draft.md", "absolute"),
        ("/Library/project/draft.md", "absolute"),
        ("/opt/project/draft.md", "absolute"),
        (r"drafts\article.md", "backslash"),
        ("$HOME/draft.md", "environment"),
        ("${HOME}/draft.md", "environment"),
        ("%USERPROFILE%/draft.md", "environment"),
        ("file:///opt/project/draft.md", "non_path"),
        ("~/project/draft.md", "home_relative"),
        ("~other/project", "home_relative"),
    ],
)
def test_portable_paths_reject_host_specific_paths_without_disclosure(
    tmp_path, value, reason
):
    repo, base = portable_repo(tmp_path, value)
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "fail"
    assert report["findings"] == [f"portable_path_{reason}:rule-1:field-1"]
    assert value not in json.dumps(report)


@pytest.mark.parametrize(
    "value",
    [
        "drafts/article.md",
        "./drafts/article.md",
        "article.md",
        "..",
        "../shared/draft.md",
        "drafts/../draft.md",
    ],
)
def test_portable_paths_accept_forward_slash_relative_paths(tmp_path, value):
    repo, base = portable_repo(tmp_path, value)
    assert MODULE.check(repo, base_ref=base)["status"] == "pass"


@pytest.mark.parametrize(
    "value,accepted",
    [
        ("~/project/draft.md", True),
        ("~/", False),
        ("~other/project", False),
        ("~/../project", True),
        (r"~/project\draft.md", False),
    ],
)
def test_portable_paths_home_policy_is_explicit_and_narrow(tmp_path, value, accepted):
    repo, base = portable_repo(tmp_path, value, allow_home_relative=True)
    assert MODULE.check(repo, base_ref=base)["status"] == (
        "pass" if accepted else "fail"
    )


@pytest.mark.parametrize(
    "value", [None, 2, True, [], {}, "", "  ", " draft.md", "draft\n.md"]
)
def test_portable_paths_invalid_values_are_tool_errors_even_in_shadow(tmp_path, value):
    repo, base = portable_repo(tmp_path, value)
    config_path = repo / MODULE.CONFIG_NAME
    config = json.loads(config_path.read_text())
    config["mode"] = "shadow"
    config_path.write_text(json.dumps(config))
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["portable_paths_invalid_value:rule-1:field-1"]


@pytest.mark.parametrize("pointer", ["path", "/bad~", "/bad~2", True, 4, None])
def test_portable_paths_malformed_pointer_config_fails_closed(tmp_path, pointer):
    repo, base = portable_repo(tmp_path, "draft.md", pointers=[pointer])
    assert MODULE.check(repo, base_ref=base)["findings"] == [
        "invalid_consistency_config"
    ]


@pytest.mark.parametrize(
    "rule",
    [
        None,
        {},
        {"path": "paths.json", "pointers": []},
        {"path": "paths.json", "pointers": ["/path", "/path"]},
        {"path": "paths.json", "pointers": ["/path"], "allow_home_relative": "true"},
        {"path": "paths.json", "pointers": ["/path"], "extra": True},
        {"path": "../paths.json", "pointers": ["/path"]},
        {"path": "C:/paths.json", "pointers": ["/path"]},
        {"path": r"config\paths.json", "pointers": ["/path"]},
    ],
)
def test_portable_paths_invalid_rules_fail_closed(tmp_path, rule):
    repo, base = portable_repo(tmp_path, "draft.md")
    config_path = repo / MODULE.CONFIG_NAME
    config = json.loads(config_path.read_text())
    config["portable_paths"] = [rule]
    config_path.write_text(json.dumps(config))
    assert MODULE.check(repo, base_ref=base)["findings"] == [
        "invalid_consistency_config"
    ]


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "untracked",
        "invalid_json",
        "duplicate_keys",
        "invalid_utf8",
        "unreadable",
        "symlink",
        "symlink_loop",
    ],
)
def test_portable_paths_unreadable_selected_files_fail_closed(
    tmp_path, kind, monkeypatch
):
    repo, base = portable_repo(tmp_path, "draft.md")
    path = repo / "paths.json"
    if kind == "missing":
        path.unlink()
    elif kind == "untracked":
        git(repo, "rm", "--cached", "paths.json")
    elif kind == "invalid_json":
        path.write_text("{broken")
    elif kind == "duplicate_keys":
        path.write_text('{"path":"draft.md","path":"other.md"}')
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif kind == "symlink":
        target = repo / "actual.json"
        target.write_text(path.read_text())
        path.unlink()
        path.symlink_to(target.name)
    elif kind == "symlink_loop":
        path.unlink()
        path.symlink_to(path.name)
    else:
        read = Path.read_text

        def guarded_read(self, *args, **kwargs):
            if self == path:
                raise PermissionError("private value must never be printed")
            return read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded_read)
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["portable_paths_unreadable:rule-1"]


@pytest.mark.parametrize(
    "pointer", ["/missing", "/path/01", "/path/-", "/path/2", "/path/" + "9" * 5000]
)
def test_portable_paths_missing_fields_and_bad_array_indexes_fail_closed(
    tmp_path, pointer
):
    repo, base = portable_repo(tmp_path, ["draft.md"], pointers=[pointer])
    assert MODULE.check(repo, base_ref=base)["findings"] == [
        "portable_paths_field_missing:rule-1:field-1"
    ]


def test_portable_paths_explicit_pointers_preserve_unselected_source(tmp_path):
    repo, base = portable_repo(tmp_path, "draft.md", pointers=["/a~1b/~0/0"])
    path = repo / "paths.json"
    body = json.dumps(
        {"a/b": {"~": ["drafts/one.md"]}, "raw_source": "/opt/host/original"}
    )
    path.write_text(body)
    assert MODULE.check(repo, base_ref=base)["status"] == "pass"
    assert path.read_text() == body


def test_portable_paths_root_pointer_and_existing_modes(tmp_path):
    repo, base = portable_repo(tmp_path, "draft.md", pointers=[""])
    (repo / "paths.json").write_text(json.dumps("/opt/project/draft.md"))
    config_path = repo / MODULE.CONFIG_NAME
    config = json.loads(config_path.read_text())
    config["mode"] = "shadow"
    config_path.write_text(json.dumps(config))
    assert MODULE.check(repo, base_ref=base)["status"] == "shadow_findings"
    config["mode"] = "ratchet"
    config["ratchet"] = {"baseline": ["portable_path_absolute:rule-1:field-1"]}
    config_path.write_text(json.dumps(config))
    assert MODULE.check(repo, base_ref=base)["status"] == "pass"


def test_cli_returns_nonzero_for_enforced_findings(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "enforce"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (repo / "docs" / "guide.md").unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--base-ref",
            base,
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "fail"


def test_cli_required_config_cannot_be_removed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / ".repo-preflight-consistency.json").unlink()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--base-ref",
            base,
            "--require-config",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["findings"] == [
        "required_consistency_config_missing"
    ]


def test_cli_required_mode_cannot_be_weakened(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--base-ref",
            base,
            "--require-mode",
            "enforce",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["findings"] == ["required_consistency_mode_mismatch"]
    assert report["finding_count"] == 1


def test_broken_markdown_link_is_reported(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").unlink()
    report = MODULE.check(repo, base_ref=base)
    assert "markdown_link_missing:README.md:docs/guide.md" in report["findings"]


def test_readme_declared_command_must_be_present_and_paths_exist(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "src" / "app.py").unlink()
    report = MODULE.check(repo, base_ref=base)
    assert any(
        finding.startswith("readme_command_missing:command-1-")
        for finding in report["findings"]
    )
    assert "python src/app.py" not in str(report)
    assert "readme_path_missing:src/app.py" in report["findings"]


def test_impact_map_requires_related_docs_change(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["impact"][0]["status"] == "fail"
    assert "related_docs_update_missing:impact-1" in report["findings"]


def configure_workflow_impact(repo: Path) -> Path:
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "1" * 40
        + " # v5\n      - run: python -m pytest\n",
        encoding="utf-8",
    )
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["impact_map"] = [
        {
            "change": [".github/workflows/ci.yml", "src/**"],
            "requires_any": ["docs/**", "tests/**"],
            "allow_github_action_ref_updates": True,
        }
    ]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add workflow contract")
    return workflow


def test_impact_map_allows_only_same_action_full_sha_update(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "2" * 40
        + " # v7\n      - run: python -m pytest\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert report["impact"][0]["status"] == "pass"
    assert report["impact"][0]["github_action_ref_update_only"] is True


def test_impact_map_allows_sha_update_on_named_step(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: Checkout\n        uses: actions/checkout@"
        + "1" * 40
        + " # v5\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "use named action step")
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - name: Checkout\n        uses: actions/checkout@"
        + "2" * 40
        + " # v7\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert report["impact"][0]["status"] == "pass"
    assert report["impact"][0]["github_action_ref_update_only"] is True


def test_impact_map_does_not_exempt_workflow_mode_change(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "2" * 40
        + " # v7\n      - run: python -m pytest\n",
        encoding="utf-8",
    )
    workflow_rel = workflow.relative_to(repo).as_posix()
    git(repo, "add", workflow_rel)
    git(repo, "update-index", "--chmod=+x", workflow_rel)

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


@pytest.mark.skipif(
    os.name == "nt", reason="Windows does not expose Git executable bits"
)
def test_impact_map_does_not_exempt_unstaged_workflow_mode_change(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "2" * 40
        + " # v7\n      - run: python -m pytest\n",
        encoding="utf-8",
    )
    workflow.chmod(workflow.stat().st_mode | 0o100)

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_impact_map_requires_whitespace_before_version_comment(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "2" * 40
        + "#not-a-yaml-comment\n      - run: python -m pytest\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


@pytest.mark.parametrize(
    "workflow_body",
    [
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    env:\n      uses: actions/checkout@{sha}\n    steps:\n      - run: echo ok\n",
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: |\n          uses: actions/checkout@{sha}\n",
    ],
)
def test_impact_map_does_not_exempt_uses_outside_action_key(
    tmp_path: Path, workflow_body: str
):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    workflow.write_text(workflow_body.format(sha="1" * 40), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add non-action uses value")
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(workflow_body.format(sha="2" * 40), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_impact_map_does_not_exempt_uses_inside_multiline_flow_mapping(
    tmp_path: Path,
):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    workflow_body = (
        "jobs:\n"
        "  test:\n"
        "    env: {{\n"
        "      FOO: bar,\n"
        "      uses: actions/checkout@{sha}\n"
        "    }}\n"
        "    steps:\n"
        "      - run: echo ok\n"
    )
    workflow.write_text(workflow_body.format(sha="1" * 40), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add flow mapping")
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(workflow_body.format(sha="2" * 40), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


@pytest.mark.parametrize(
    "workflow_body",
    [
        "jobs:\n  test:\n    env: &values {{\n      uses: actions/checkout@{sha}\n    }}\n    steps:\n      - run: echo ok\n",
        "jobs:\n  test:\n    steps:\n      - run: &script |\n          uses: actions/checkout@{sha}\n",
        'jobs:\n  test:\n    steps:\n      - run: "echo\n        uses: actions/checkout@{sha}\n"\n',
    ],
)
def test_impact_map_does_not_exempt_ambiguous_yaml_scalar_context(
    tmp_path: Path, workflow_body: str
):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    workflow.write_text(workflow_body.format(sha="1" * 40), encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add ambiguous scalar context")
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(workflow_body.format(sha="2" * 40), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_impact_map_does_not_exempt_nested_workflow_template(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    configure_workflow_impact(repo)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["impact_map"][0]["change"] = [".github/workflows/*.yml"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    template = repo / ".github" / "workflows" / "templates" / "ci.yml"
    template.parent.mkdir()
    template.write_text(
        "jobs:\n  test:\n    uses: owner/workflows/.github/workflows/test.yml@"
        + "1" * 40
        + "\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add nested workflow template")
    base = git(repo, "rev-parse", "HEAD")
    template.write_text(
        "jobs:\n  test:\n    uses: owner/workflows/.github/workflows/test.yml@"
        + "2" * 40
        + "\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_impact_map_allows_reusable_workflow_sha_update(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    workflow.write_text(
        "jobs:\n  reusable:\n    uses: owner/workflows/.github/workflows/test.yml@"
        + "1" * 40
        + " # v1\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "use reusable workflow")
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  reusable:\n    uses: owner/workflows/.github/workflows/test.yml@"
        + "2" * 40
        + " # v2\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert report["impact"][0]["github_action_ref_update_only"] is True


def test_impact_map_does_not_exempt_action_identity_change(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: evil/checkout@"
        + "2" * 40
        + " # v7\n      - run: python -m pytest\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_impact_map_does_not_exempt_workflow_logic_change(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "2" * 40
        + " # v7\n      - run: python -m unittest\n",
        encoding="utf-8",
    )

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_impact_map_does_not_exempt_mixed_affected_files(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    workflow = configure_workflow_impact(repo)
    base = git(repo, "rev-parse", "HEAD")
    workflow.write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@"
        + "2" * 40
        + " # v7\n      - run: python -m pytest\n",
        encoding="utf-8",
    )
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert "related_docs_update_missing:impact-1" in report["findings"]


def test_action_ref_update_exemption_must_be_boolean(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["impact_map"][0]["allow_github_action_ref_updates"] = "true"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert report["status"] == "tool_error"
    assert report["findings"] == ["invalid_consistency_config"]


def test_generated_hash_drift_is_reported(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "generated.txt").write_text("drift\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert "generated_artifact_drift:generated.txt" in report["findings"]


def test_changed_ssot_requires_generated_artifact_refresh(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert "generated_artifact_update_missing:generated.txt" in report["findings"]


def test_declared_path_cannot_escape_repository(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["readme_contracts"]["required_paths"] = ["../outside"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert MODULE.check(repo, base_ref=base)["status"] == "tool_error"


def test_shadow_and_enforce_have_same_findings_but_different_gate_status(
    tmp_path: Path,
):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").unlink()
    shadow = MODULE.check(repo, base_ref=base)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "enforce"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    enforce = MODULE.check(repo, base_ref=base)
    assert shadow["findings"] == enforce["findings"]
    assert shadow["status"] == "shadow_findings"
    assert enforce["status"] == "fail"


def test_invalid_config_fails_closed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / ".repo-preflight-consistency.json").write_text("{}", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["invalid_consistency_config"]


def test_ratchet_accepts_only_current_baseline_findings(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").unlink()
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "ratchet"
    config["ratchet"] = {"baseline": ["markdown_link_missing:README.md:docs/guide.md"]}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert report["status"] == "pass"
    assert report["ratchet"]["accepted"] == [
        "markdown_link_missing:README.md:docs/guide.md"
    ]
    assert report["ratchet"]["new"] == []


def test_ratchet_blocks_new_regression(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").unlink()
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "ratchet"
    config["ratchet"] = {"baseline": []}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert report["status"] == "fail"
    assert report["ratchet"]["new"] == ["markdown_link_missing:README.md:docs/guide.md"]


def test_ratchet_blocks_stale_baseline_after_improvement(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["mode"] = "ratchet"
    config["ratchet"] = {"baseline": ["old:finding"]}
    config_path.write_text(json.dumps(config), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert report["status"] == "fail"
    assert report["ratchet"]["resolved"] == ["old:finding"]


def test_capability_routes_recommend_only_relevant_plugins(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "README.md").write_text("# Improved\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    ids = {item["id"] for item in report["capability_recommendations"]}
    assert "readability-template" in ids
    assert "product-design-audit" in ids
    assert "security-guidance" not in ids


def test_unknown_config_key_fails_closed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["unknown"] = True
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert MODULE.check(repo, base_ref=base) == {
        "status": "tool_error",
        "mode": None,
        "findings": ["invalid_consistency_config"],
    }


def test_bool_is_not_accepted_as_string_or_array(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["markdown"]["include"] = [True]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert MODULE.check(repo, base_ref=base)["status"] == "tool_error"


def test_nested_unknown_key_and_wrong_array_item_fail_closed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["impact_map"][0]["extra"] = "no"
    config["generated_artifacts"][0]["sources"] = [1]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert MODULE.check(repo, base_ref=base)["findings"] == [
        "invalid_consistency_config"
    ]


def test_type_error_in_config_never_escapes(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["readme_contracts"]["commands"] = [{"text": True, "paths": []}]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["invalid_consistency_config"]


def test_markdown_symlink_is_not_followed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("[missing](secret.md)\n", encoding="utf-8")
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=str(outside),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},docs/link.md")
    (repo / "docs" / "link.md").write_text(str(outside), encoding="utf-8")

    report = MODULE.check(repo, base_ref=base)

    assert not any("docs/link.md" in finding for finding in report["findings"])


def test_gitlink_markdown_like_path_is_not_read(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", "160000", head, "docs/vendor.md")
    report = MODULE.check(repo, base_ref=base)
    assert not any("docs/vendor.md" in finding for finding in report["findings"])


def test_change_sensitive_checks_require_base_ref(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    git(repo, "commit", "--allow-empty", "-m", "second")
    report = MODULE.check(repo)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["change_sensitive_scope_unavailable"]


def test_single_root_commit_checks_whole_initial_tree(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    report = MODULE.check(repo)
    assert report["status"] == "pass"
    assert report["comparison_scope"] == "initial_tree"
    assert report["impact"][0]["status"] == "pass"


def test_github_zero_before_checks_complete_initial_tree(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    report = MODULE.check(repo, base_ref="0" * 40)
    assert report["status"] == "pass"
    assert report["comparison_scope"] == "initial_tree"


@pytest.mark.parametrize("history", ["multiple", "shallow"])
def test_github_zero_before_does_not_exempt_existing_or_shallow_history(
    tmp_path, history
):
    repo, head = make_repo(tmp_path)
    if history == "multiple":
        git(repo, "commit", "--allow-empty", "-m", "second")
    else:
        (repo / ".git/shallow").write_text(head + "\n", encoding="ascii")
    report = MODULE.check(repo, base_ref="0" * 40)
    assert report["status"] == "tool_error"


def test_initial_tree_still_rejects_missing_required_document(tmp_path: Path):
    repo, _ = make_repo(tmp_path)
    (repo / "README.md").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "--amend", "--no-edit")
    report = MODULE.check(repo)
    assert report["comparison_scope"] == "initial_tree"
    assert report["findings"]


def test_single_shallow_commit_is_not_an_initial_tree(tmp_path: Path):
    repo, head = make_repo(tmp_path)
    (repo / ".git/shallow").write_text(head + "\n", encoding="ascii")
    report = MODULE.check(repo)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["change_sensitive_scope_unavailable"]


def test_config_edit_alone_does_not_refresh_generated_artifact(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["ratchet"] = {"baseline": []}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert "generated_artifact_update_missing:generated.txt" in report["findings"]


def test_invalid_utf8_markdown_is_tool_error_even_in_shadow(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").write_bytes(b"\xff\xfe")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert "markdown_unreadable:docs/guide.md" in report["findings"]


def test_globstar_matches_file_directly_under_docs(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "docs" / "guide.md").write_text("updated\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "pass"


def test_readme_symlink_is_not_followed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    outside = tmp_path / "outside.md"
    secret = "github_pat_" + "S" * 30
    outside.write_text(secret, encoding="utf-8")
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=str(outside),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},README.md")
    (repo / "README.md").write_text(str(outside), encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert secret not in str(report)


def test_rename_exposes_old_and_new_paths(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    git(repo, "mv", "docs/guide.md", "docs/renamed.md")
    changed = MODULE._changed_files(repo, base)
    assert "docs/guide.md" in changed
    assert "docs/renamed.md" in changed


def test_markdown_target_must_be_tracked(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    (repo / "untracked.md").write_text("not committed\n", encoding="utf-8")
    (repo / "README.md").write_text("[untracked](untracked.md)\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert "markdown_link_missing:README.md:untracked.md" in report["findings"]


def test_secret_command_text_is_never_exposed(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    secret = "github_pat_" + "T" * 30
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["readme_contracts"]["commands"] = [{"text": secret, "paths": []}]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert secret not in str(report)


def test_each_impact_rule_has_distinct_finding_id(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["impact_map"].append({"change": ["src/**"], "requires_any": ["tests/**"]})
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (repo / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    ids = [
        finding
        for finding in report["findings"]
        if finding.startswith("related_docs_update_missing:")
    ]
    assert ids == [
        "related_docs_update_missing:impact-1",
        "related_docs_update_missing:impact-2",
    ]


def test_without_readme_contracts_readme_is_not_loaded(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.pop("readme_contracts")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    (repo / "README.md").write_bytes(b"\xff\xfe")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "pass"


def test_empty_ratchet_object_requires_baseline(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    config_path = repo / ".repo-preflight-consistency.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["ratchet"] = {}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"


def test_git_dir_env_does_not_inventory_another_repository(tmp_path: Path, monkeypatch):
    """GIT_DIR で別 repository を指しても、--repo 側の inventory を差し替えない。"""
    left_home = tmp_path / "left"
    right_home = tmp_path / "right"
    left_home.mkdir()
    right_home.mkdir()
    left, base = make_repo(left_home)
    right, _ = make_repo(right_home)
    (right / "only-in-right.md").write_text("secret-side\n", encoding="utf-8")
    git(right, "add", "only-in-right.md")
    git(right, "commit", "-m", "right-only")

    monkeypatch.setenv("GIT_DIR", str(right / ".git"))
    report = MODULE.check(left, base_ref=base)

    assert report["status"] == "pass"
    tracked = MODULE._tracked_files(left)
    assert "only-in-right.md" not in tracked
    assert "README.md" in tracked


# --- fence 内のリンク例を実在要求しない回帰 --------------------------------
#
# 以前は fence の内外を区別せず本文全体から [x](path) を拾っていたため、
# Markdown の書き方を例示している文書が enforce を通れなかった。
# fence 判定は readme_release_gate.outside_fences を正本として再利用する。


def _write_guide(repo: Path, body: str) -> None:
    (repo / "docs" / "guide.md").write_text(body, encoding="utf-8")


def test_link_example_inside_a_fence_is_not_required_to_exist(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n```markdown\n[書き方の例](docs/does-not-exist.md)\n```\n",
    )
    report = MODULE.check(repo, base_ref=base)
    assert not [f for f in report["findings"] if f.startswith("markdown_link_missing")]


def test_link_outside_a_fence_is_still_reported(tmp_path: Path):
    """fence 対応で検知力を落としていないこと。"""
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n[本物のリンク切れ](docs/missing-real.md)\n\n"
        "```markdown\n[例示](docs/example-only.md)\n```\n",
    )
    report = MODULE.check(repo, base_ref=base)
    missing = [f for f in report["findings"] if f.startswith("markdown_link_missing")]
    assert missing == ["markdown_link_missing:docs/guide.md:docs/missing-real.md"]


def test_tilde_fence_is_honoured(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    _write_guide(repo, "# ガイド\n\n~~~markdown\n[例](docs/tilde.md)\n~~~\n")
    report = MODULE.check(repo, base_ref=base)
    assert not [f for f in report["findings"] if f.startswith("markdown_link_missing")]


def test_indented_fence_is_honoured(tmp_path: Path):
    """インデントされた fence。2026-08-16 review F6 で事故った形。"""
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n1. 手順\n\n   ```markdown\n   [例](docs/indented.md)\n   ```\n",
    )
    report = MODULE.check(repo, base_ref=base)
    assert not [f for f in report["findings"] if f.startswith("markdown_link_missing")]


def test_nested_fence_is_honoured(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n````markdown\n```\n[入れ子](docs/nested.md)\n```\n````\n",
    )
    report = MODULE.check(repo, base_ref=base)
    assert not [f for f in report["findings"] if f.startswith("markdown_link_missing")]


def test_fence_reader_is_the_readme_release_gate_one(tmp_path: Path):
    """fence 判定を consistency_gate 側で数え直していないこと。"""
    reader = MODULE._load_fence_reader()
    assert reader.__module__ == "readme_release_gate"
    assert reader.__name__ == "outside_fences"


def test_link_label_wrapping_across_lines_is_still_reported(tmp_path: Path):
    """fence 対応で行またぎラベルを取り落とさないこと (2026-08-29 review)。

    LINK_RE のラベル部は改行を跨げる。行ごとに findall すると、
    fence を正しく除外する代わりにこの形の検知を静かに失う。
    """
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n[長いラベルが\n行をまたぐ](docs/wrapped-missing.md)\n",
    )
    report = MODULE.check(repo, base_ref=base)
    missing = [f for f in report["findings"] if f.startswith("markdown_link_missing")]
    assert missing == ["markdown_link_missing:docs/guide.md:docs/wrapped-missing.md"]


def test_fence_masking_does_not_leak_links_out_of_a_fence(tmp_path: Path):
    """行を空行へ潰す実装が、fence 内のリンクを外へ漏らさないこと。"""
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n```markdown\n[例](docs/inside-only.md)\n```\n\n本文\n",
    )
    report = MODULE.check(repo, base_ref=base)
    assert not [f for f in report["findings"] if f.startswith("markdown_link_missing")]


def test_fence_masking_does_not_join_link_fragments_across_a_fence(tmp_path: Path):
    """fence 前後の未完結 `[` と `](path)` を結合しないこと (Codex P2)。

    空行マスクだと LINK_RE のラベル部が fence を跨いでマッチし、
    実在しないリンク切れを誤検知する。
    """
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n"
        "未完結の括弧 [\n"
        "```markdown\n"
        "fence 内\n"
        "```\n"
        "](docs/should-not-join.md)\n",
    )
    report = MODULE.check(repo, base_ref=base)
    missing = [f for f in report["findings"] if f.startswith("markdown_link_missing")]
    assert missing == []


def test_four_space_indented_fence_marker_does_not_swallow_later_links(
    tmp_path: Path,
):
    """4 空白インデントの ``` は fence にならず、後続のリンク切れを隠さない (Codex P2)。"""
    repo, base = make_repo(tmp_path)
    _write_guide(
        repo,
        "# ガイド\n\n"
        "    ```\n"
        "    indented code looking like a fence\n"
        "\n"
        "[後続の本物](docs/after-fake-fence.md)\n",
    )
    report = MODULE.check(repo, base_ref=base)
    missing = [f for f in report["findings"] if f.startswith("markdown_link_missing")]
    assert missing == ["markdown_link_missing:docs/guide.md:docs/after-fake-fence.md"]


def test_missing_fence_reader_becomes_tool_error_not_a_traceback(
    tmp_path: Path, monkeypatch
):
    """正本が読めない時に scan 全体を道連れにしないこと。

    check() の契約は「所見を JSON で返す」。traceback を投げると
    readiness_scan.run_consistency_gate 側に受け口が無く scan が落ちる。
    """
    repo, base = make_repo(tmp_path)

    def _boom():
        raise RuntimeError("fence_reader_unavailable:FileNotFoundError")

    monkeypatch.setattr(MODULE, "_load_fence_reader", _boom)
    report = MODULE.check(repo, base_ref=base)
    assert report["status"] == "tool_error"
    assert report["findings"] == ["fence_reader_unavailable:FileNotFoundError"]


def test_fence_reader_loader_raises_runtime_error_when_the_file_is_gone(tmp_path: Path):
    """実際に file を消した時に出るのが RuntimeError であること。"""
    fake_scripts = tmp_path / "scripts"
    fake_scripts.mkdir()
    source = Path(MODULE.__file__).read_text(encoding="utf-8")
    (fake_scripts / "consistency_gate.py").write_text(source, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "cg_without_sibling", fake_scripts / "consistency_gate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(RuntimeError, match="fence_reader_unavailable"):
        module._load_fence_reader()


def test_check_does_not_run_fsmonitor_command_from_target_config(tmp_path: Path):
    repo, base = make_repo(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    (repo / "docs" / "guide.md").write_text("guide changed\n", encoding="utf-8")

    MODULE.check(repo, base_ref=base)

    assert not marker.exists()


def test_cli_output_is_utf8_even_when_stdio_encoding_is_ascii():
    # Windows の既定コードページでパイプへ日本語を出しても落ちない。
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_cli_utf8_stdio import assert_utf8_help

    assert_utf8_help("consistency_gate.py")


REPO_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "workflow",
    sorted(
        path.name
        for path in (REPO_ROOT / ".github" / "workflows").glob("*.yml")
        if path.name != "repository-preflight.yml"
    ),
)
def test_own_workflows_are_readable_by_action_ref_scanner(workflow: str):
    """Dependabot の Action SHA 更新を免除できるよう、自前の workflow は読み取り器が読める形を保つ。

    流れ形式（`[main]` など）や jobs 内のスカラー列があると読み取り器は None を返し、
    allow_github_action_ref_updates があっても免除されず、Dependabot PR が止まる。
    repository-preflight.yml はどの impact 規則の対象でもないので除く。
    """
    source = (REPO_ROOT / ".github" / "workflows" / workflow).read_bytes()
    assert MODULE._action_uses_lines(source), workflow


def test_ci_workflow_rule_allows_action_ref_updates():
    config = json.loads(
        (REPO_ROOT / ".repo-preflight-consistency.json").read_text(encoding="utf-8")
    )
    rules = [
        r for r in config["impact_map"] if ".github/workflows/ci.yml" in r["change"]
    ]
    assert rules and all(
        r.get("allow_github_action_ref_updates") is True for r in rules
    )
