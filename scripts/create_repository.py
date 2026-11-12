from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_OPTIONS = {
    "--allow-no-preflight",
    "--preflight-script",
    "--settings-only",
    "--install-development-lint",
}


DELEGATED_OPTIONS = frozenset(
    {"--name", "--visibility", "--description", "--confirm", "--json"}
)


def blocked(code):
    print(
        json.dumps(
            {"mode": "create-repository", "status": "BLOCKED", "cause": code},
            ensure_ascii=False,
        )
    )
    return 1


def bootstrap_source(explicit, home, env):
    if explicit is not None:
        source = explicit.expanduser()
        if not source.is_file():
            raise ValueError("explicit_bootstrap_unavailable")
        return source
    if env.get("REPO_BOOTSTRAP_SCRIPT"):
        source = Path(env["REPO_BOOTSTRAP_SCRIPT"]).expanduser()
        if not source.is_file():
            raise ValueError("configured_bootstrap_unavailable")
        return source
    for runtime in (".codex", ".claude", ".agents"):
        source = home / runtime / "skills/new-repo-bootstrap/scripts/bootstrap_repo.py"
        if source.is_file():
            return source
    raise ValueError("bootstrap_unavailable")


def load_bootstrap(source):
    spec = importlib.util.spec_from_file_location(
        "repo_preflight_repository_bootstrap", source
    )
    if spec is None or spec.loader is None:
        raise ValueError("bootstrap_unreadable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "main", None)) or not callable(
        getattr(module, "build_parser", None)
    ):
        raise ValueError("bootstrap_entrypoint_unavailable")
    if "repo-preflight-development-lint/v1" not in getattr(
        module, "BOOTSTRAP_CAPABILITIES", ()
    ):
        raise ValueError("bootstrap_contract_incompatible")
    return module


def main(argv=None, *, home=None, env=None):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--bootstrap-script", type=Path)
    try:
        args, forwarded = parser.parse_known_args(argv)
    except SystemExit:
        return blocked("invalid_bootstrap_argument")
    for argument in forwarded:
        option = argument.split("=", 1)[0]
        if option.startswith("--") and any(
            name.startswith(option) for name in BLOCKED_OPTIONS
        ):
            return blocked("creation_bypass_option_rejected")
    scoped_env = dict(os.environ if env is None else env)
    scoped_env["REPO_PREFLIGHT_ROOT"] = str(ROOT)
    home = Path.home() if home is None else Path(home)
    readiness = ROOT / "scripts/readiness_scan.py"
    if not readiness.is_file():
        return blocked("owner_readiness_entrypoint_unavailable")
    output = io.StringIO()
    try:
        source = bootstrap_source(args.bootstrap_script, home, scoped_env)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            bootstrap = load_bootstrap(source)
        output = io.StringIO()
        delegated = [*forwarded, "--preflight-script", str(readiness)]
        validation = bootstrap.build_parser()
        validation.allow_abbrev = False
        accepted = {
            option for action in validation._actions for option in action.option_strings
        }
        if not DELEGATED_OPTIONS.issubset(accepted):
            return blocked("bootstrap_contract_incompatible")
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                options = validation.parse_args(delegated)
        except SystemExit as exc:
            if exc.code == 0:
                print(output.getvalue(), end="")
                return 0
            return blocked("invalid_creation_arguments")
        if getattr(options, "confirm", False):
            provided = {
                argument.split("=", 1)[0]
                for argument in forwarded
                if argument.startswith("--")
            }
            if not {"--name", "--visibility"}.issubset(provided) or not (
                getattr(options, "name", None) and getattr(options, "visibility", None)
            ):
                return blocked("creation_target_required")
        return bootstrap.main(delegated, env=scoped_env)
    except ValueError as exc:
        known = {
            "explicit_bootstrap_unavailable",
            "configured_bootstrap_unavailable",
            "bootstrap_unavailable",
            "bootstrap_unreadable",
            "bootstrap_entrypoint_unavailable",
            "bootstrap_contract_incompatible",
        }
        return blocked(str(exc) if str(exc) in known else "bootstrap_execution_failed")
    except Exception:
        return blocked("bootstrap_execution_failed")


if __name__ == "__main__":
    raise SystemExit(main())
