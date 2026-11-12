from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/create_repository.py"
FAKE_BOOTSTRAP = """import argparse
import json

BOOTSTRAP_CAPABILITIES = {"repo-preflight-development-lint/v1"}

def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name")
    parser.add_argument("--visibility", choices=["public", "private"])
    parser.add_argument("--description")
    parser.add_argument("--owner")
    parser.add_argument("--repo-dir")
    parser.add_argument("--preflight-script")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-push", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser

def main(argv=None, *, env=None):
    print(json.dumps({"argv": argv, "owner": env.get("REPO_PREFLIGHT_ROOT"), "sentinel": env.get("SENTINEL")}))
    return 0
"""


@pytest.fixture
def entry(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "test_repository_creation_entry", SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    owner = tmp_path / "owner"
    (owner / "scripts").mkdir(parents=True)
    (owner / "scripts/readiness_scan.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", owner)
    return module


@pytest.fixture
def bootstrap(tmp_path):
    source = tmp_path / "bootstrap_repo.py"
    source.write_text(FAKE_BOOTSTRAP, encoding="utf-8")
    return source


@pytest.mark.parametrize("confirmed", [False, True])
def test_plan_and_confirm_forward_to_bootstrap(entry, bootstrap, capsys, confirmed):
    args = [
        "--bootstrap-script",
        str(bootstrap),
        "--name",
        "sample",
        "--visibility",
        "private",
        "--description",
        "試作",
        "--json",
    ]
    if confirmed:
        args.extend(["--confirm", "--resume", "--skip-push"])
    assert entry.main(args, env={"SENTINEL": "kept"}) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["argv"] == [
        *args[2:],
        "--preflight-script",
        str(entry.ROOT / "scripts/readiness_scan.py"),
    ]
    assert ("--confirm" in result["argv"]) == confirmed
    assert result["owner"] == str(entry.ROOT)
    assert result["sentinel"] == "kept"


def test_delegation_uses_scoped_environment(entry, bootstrap, monkeypatch, capsys):
    monkeypatch.setenv("REPO_PREFLIGHT_ROOT", "outer-owner")
    before = dict(os.environ)
    assert (
        entry.main(
            [
                "--bootstrap-script",
                str(bootstrap),
                "--name",
                "sample",
                "--visibility",
                "private",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["owner"] == str(entry.ROOT)
    assert dict(os.environ) == before


@pytest.mark.parametrize("runtime", [".codex", ".claude", ".agents"])
def test_adopted_runtime_bootstrap_is_discovered(entry, tmp_path, capsys, runtime):
    source = tmp_path / runtime / "skills/new-repo-bootstrap/scripts/bootstrap_repo.py"
    source.parent.mkdir(parents=True)
    source.write_text(FAKE_BOOTSTRAP, encoding="utf-8")
    assert (
        entry.main(
            ["--name", "sample", "--visibility", "private"], home=tmp_path, env={}
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["owner"] == str(entry.ROOT)


def test_missing_bootstrap_is_structured_and_does_not_disclose_paths(
    entry, tmp_path, capsys
):
    assert entry.main([], home=tmp_path, env={}) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["cause"] == "bootstrap_unavailable"
    assert str(tmp_path) not in output.out + output.err


@pytest.mark.parametrize("mode", ["explicit", "environment"])
def test_invalid_explicit_bootstrap_never_falls_back(entry, tmp_path, capsys, mode):
    installed = tmp_path / ".codex/skills/new-repo-bootstrap/scripts/bootstrap_repo.py"
    installed.parent.mkdir(parents=True)
    installed.write_text(FAKE_BOOTSTRAP, encoding="utf-8")
    invalid = tmp_path / "private-value/missing.py"
    args = ["--bootstrap-script", str(invalid)] if mode == "explicit" else []
    env = {"REPO_BOOTSTRAP_SCRIPT": str(invalid)} if mode == "environment" else {}
    assert entry.main(args, home=tmp_path, env=env) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["status"] == "BLOCKED"
    assert str(invalid) not in output.out + output.err


@pytest.mark.parametrize(
    "option",
    [
        "--allow-no-preflight",
        "--allow-no-preflight=true",
        "--allow-no",
        "--preflight-script=foreign.py",
        "--preflight-scr",
        "--settings-only",
        "--install-development-lint",
    ],
)
def test_owner_and_creation_guards_cannot_be_overridden(
    entry, bootstrap, capsys, option
):
    assert entry.main(["--bootstrap-script", str(bootstrap), option]) == 1
    assert (
        json.loads(capsys.readouterr().out)["cause"]
        == "creation_bypass_option_rejected"
    )


def test_confirm_requires_explicit_creation_target(entry, bootstrap, capsys):
    assert entry.main(["--bootstrap-script", str(bootstrap), "--confirm"]) == 1
    assert json.loads(capsys.readouterr().out)["cause"] == "creation_target_required"


def test_bootstrap_diagnostics_do_not_disclose_exception_details(
    entry, bootstrap, capsys
):
    bootstrap.write_text('raise RuntimeError("private-value")\n', encoding="utf-8")
    assert entry.main(["--bootstrap-script", str(bootstrap)]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["cause"] == "bootstrap_execution_failed"
    assert "private-value" not in output.out + output.err


def test_help_does_not_echo_import_output(entry, bootstrap, capsys):
    bootstrap.write_text('print("private-value")\n' + FAKE_BOOTSTRAP, encoding="utf-8")
    assert entry.main(["--bootstrap-script", str(bootstrap), "--help"]) == 0
    output = capsys.readouterr()
    assert "--name" in output.out
    assert "private-value" not in output.out + output.err


def test_advertised_delegated_options_require_executor_support(
    entry, bootstrap, capsys
):
    source = bootstrap.read_text().replace(
        '    parser.add_argument("--description")\n', ""
    )
    bootstrap.write_text(source)
    assert entry.main(["--bootstrap-script", str(bootstrap)], env={}) == 1
    assert (
        json.loads(capsys.readouterr().out)["cause"]
        == "bootstrap_contract_incompatible"
    )


def test_legacy_executor_stops_before_creation(entry, bootstrap, capsys):
    bootstrap.write_text(
        bootstrap.read_text().replace(
            'BOOTSTRAP_CAPABILITIES = {"repo-preflight-development-lint/v1"}',
            "BOOTSTRAP_CAPABILITIES = set()",
        )
    )
    assert (
        entry.main(
            [
                "--bootstrap-script",
                str(bootstrap),
                "--name",
                "sample",
                "--visibility",
                "private",
                "--confirm",
            ],
            env={},
        )
        == 1
    )
    assert (
        json.loads(capsys.readouterr().out)["cause"]
        == "bootstrap_contract_incompatible"
    )
