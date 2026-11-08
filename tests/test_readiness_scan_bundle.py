import ast
import hashlib
import importlib.util
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FACADE = SCRIPTS / "readiness_scan.py"
PART_PREFIX = "readiness_scan_"
FORBIDDEN_NAMES = {
    "__file__",
    "__import__",
    "compile",
    "eval",
    "exec",
    "globals",
    "importlib",
    "runpy",
    "subprocess",
}
FORBIDDEN_ATTRIBUTES = {("os", "environ"), ("sys", "modules"), ("os", "getenv")}


def load_facade(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_facade(FACADE, "readiness_scan_bundle_tests")


def part_files():
    return sorted(SCRIPTS.glob(PART_PREFIX + "*.py"))


def expected_digest(entries):
    lines = "".join(
        name + "\0" + hashlib.sha256(data).hexdigest() + "\n"
        for name, data in sorted(entries.items())
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def test_bundle_names_match_part_files():
    assert MODULE.scanner_bundle_names() == tuple(
        sorted([FACADE.name] + [path.name for path in part_files()])
    )


def test_bundle_digest_covers_facade_and_every_part():
    entries = {FACADE.name: FACADE.read_bytes()}
    entries.update({path.name: path.read_bytes() for path in part_files()})
    assert MODULE.scanner_bundle_sha256() == expected_digest(entries)


def test_identity_cache_key_uses_bundle_digest():
    assert MODULE._scanner_source_sha256() == MODULE.scanner_bundle_sha256()


def bundle_entries(scripts, **extra):
    entries = {FACADE.name: (scripts / FACADE.name).read_bytes()}
    entries.update(
        {
            path.name: path.read_bytes()
            for path in sorted(scripts.glob(PART_PREFIX + "*.py"))
        }
    )
    entries.update(extra)
    return entries


def shipped_part_names():
    return [path.name for path in part_files()]


def copied_scripts(tmp_path):
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, scripts)
    return scripts


def test_loading_a_part_extends_the_bundle(tmp_path):
    scripts = copied_scripts(tmp_path)
    facade = load_facade(scripts / FACADE.name, "readiness_scan_bundle_probe")
    before = facade.scanner_bundle_sha256()
    probe = scripts / (PART_PREFIX + "probe.py")
    probe.write_bytes(b"VALUE = 1\n")

    first = facade._part(PART_PREFIX + "probe")
    assert first.VALUE == 1
    assert probe.name in facade.scanner_bundle_names()
    with_probe = facade.scanner_bundle_sha256()
    assert with_probe != before
    assert with_probe == expected_digest(bundle_entries(scripts))

    assert facade._part(PART_PREFIX + "probe") is first

    probe.write_bytes(b"VALUE = 2\n")
    second = facade._part(PART_PREFIX + "probe")
    assert second is not first
    assert second.VALUE == 2
    assert facade.scanner_bundle_sha256() not in (before, with_probe)


def test_executed_part_is_the_hashed_part(tmp_path):
    scripts = copied_scripts(tmp_path)
    facade = load_facade(scripts / FACADE.name, "readiness_scan_bundle_same_size")
    probe = scripts / (PART_PREFIX + "probe.py")
    probe.write_bytes(b"VALUE = 1\n")
    facade._part(PART_PREFIX + "probe")
    stamp = probe.stat()
    probe.write_bytes(b"VALUE = 9\n")
    os.utime(probe, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert facade._part(PART_PREFIX + "probe").VALUE == 9
    assert facade.scanner_bundle_sha256() == expected_digest(
        bundle_entries(scripts, **{probe.name: b"VALUE = 9\n"})
    )


def test_unloaded_part_stops_the_bundle_digest(tmp_path):
    scripts = copied_scripts(tmp_path)
    facade = load_facade(scripts / FACADE.name, "readiness_scan_bundle_unloaded")
    (scripts / (PART_PREFIX + "stray.py")).write_bytes(b"VALUE = 1\n")
    with pytest.raises(RuntimeError, match="scanner_part_not_loaded"):
        facade.scanner_bundle_sha256()


def test_only_scanner_parts_can_be_loaded():
    for name in ("identity_gate", "local_check", "readiness_scan_../x", "x"):
        with pytest.raises(ImportError, match="scanner_part_name_rejected"):
            MODULE._part(name)


def test_failed_part_is_not_registered(tmp_path):
    scripts = copied_scripts(tmp_path)
    facade = load_facade(scripts / FACADE.name, "readiness_scan_bundle_broken")
    (scripts / (PART_PREFIX + "broken.py")).write_bytes(b"raise RuntimeError('x')\n")
    with pytest.raises(RuntimeError, match="x"):
        facade._part(PART_PREFIX + "broken")
    assert "_" + PART_PREFIX + "broken" not in sys.modules
    (scripts / (PART_PREFIX + "broken.py")).unlink()
    assert facade.scanner_bundle_names() == tuple(
        sorted([FACADE.name] + shipped_part_names())
    )


def test_part_loading_works_from_executed_bytes(tmp_path):
    scripts = copied_scripts(tmp_path)
    scanner = scripts / FACADE.name
    module = types.ModuleType("_managed_readiness_bundle_tests")
    module.__file__ = str(scanner)
    exec(compile(scanner.read_bytes(), str(scanner), "exec"), module.__dict__)
    (scripts / (PART_PREFIX + "probe.py")).write_bytes(b"VALUE = 3\n")
    assert module._part(PART_PREFIX + "probe").VALUE == 3
    assert module.scanner_bundle_names() == tuple(
        sorted([FACADE.name, PART_PREFIX + "probe.py"] + shipped_part_names())
    )


def seams(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            yield node.id
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_NAMES:
                    yield alias.name
                if root in ("os", "sys") and alias.asname not in (None, root):
                    yield alias.name + " as " + alias.asname
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in FORBIDDEN_NAMES:
                yield node.module
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES or (root, alias.name) in (
                    FORBIDDEN_ATTRIBUTES
                ):
                    yield node.module + "." + alias.name
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in FORBIDDEN_ATTRIBUTES
        ):
            yield node.value.id + "." + node.attr


def test_seam_detector_catches_indirect_access():
    leaks = {
        "from os import environ": "os.environ",
        "from os import getenv": "os.getenv",
        "from sys import modules": "sys.modules",
        "import os as o": "os as o",
        "import sys as s": "sys as s",
        "import os\nos.environ": "os.environ",
        "__import__('subprocess')": "__import__",
        "globals()['x']": "globals",
        "from importlib import util": "importlib",
        "exec('x = 1')": "exec",
    }
    for source, hit in leaks.items():
        assert hit in set(seams(ast.parse(source))), source
    assert list(seams(ast.parse("import os\nfrom pathlib import Path\n"))) == []


def test_failed_reload_keeps_the_previous_part(tmp_path):
    scripts = copied_scripts(tmp_path)
    facade = load_facade(scripts / FACADE.name, "readiness_scan_bundle_reload")
    probe = scripts / (PART_PREFIX + "probe.py")
    probe.write_bytes(b"VALUE = 1\n")
    first = facade._part(PART_PREFIX + "probe")
    probe.write_bytes(b"raise RuntimeError('y')\n")
    with pytest.raises(RuntimeError, match="y"):
        facade._part(PART_PREFIX + "probe")
    assert sys.modules["_" + PART_PREFIX + "probe"] is first


def test_parts_have_no_seams():
    found = {
        path.name: sorted(set(seams(ast.parse(path.read_text(encoding="utf-8")))))
        for path in part_files()
    }
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_parts_are_reached_only_through_the_facade():
    names = [path.stem for path in part_files()]
    readers = [
        path
        for path in sorted(SCRIPTS.glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
        if path != FACADE
        and path not in part_files()
        and path.resolve() != Path(__file__).resolve()
        and any(name in path.read_text(encoding="utf-8") for name in names)
    ]
    assert readers == []


REPORT = MODULE._report
STATUSES = ("pass", "fail", "unknown", "tool_error", "not_configured")


def gate(**overrides):
    arguments = {
        "identity_gate": {"status": "pass"},
        "allowed_identities_configured": False,
        "require_private_identity": False,
        "private_identity_report": {"status": "not_configured"},
        "target_diff": False,
        "release": False,
    }
    arguments.update(overrides)
    return REPORT.gating(**arguments)


GATING_CASES = {
    "repository": gate(),
    "target_diff": gate(target_diff=True),
    "release": gate(release=True),
    "attribution": gate(allowed_identities_configured=True),
    "required_private": gate(require_private_identity=True, target_diff=True),
    "covered_identity": gate(
        identity_gate={"status": "fail", "reason": "private_patterns_not_configured"},
        allowed_identities_configured=True,
    ),
}


def test_gating_sets_follow_scan_mode():
    always = set(REPORT.ALWAYS_GATED_CHECKS) | {"repository_consistency"}
    repository = set(REPORT.REPOSITORY_SCOPE_CHECKS)
    assert GATING_CASES["repository"].names == always | repository
    assert GATING_CASES["target_diff"].names == always
    assert GATING_CASES["release"].names == always | repository | set(
        REPORT.RELEASE_CHECKS
    )
    assert GATING_CASES["attribution"].names == always | repository | {
        "public_attribution"
    }
    assert GATING_CASES["required_private"].names == always | {"private_identity_scan"}
    assert GATING_CASES["covered_identity"].names == (
        always | repository | {"public_attribution"}
    ) - {"identity_gate"}


@pytest.mark.parametrize("reason_matches", [True, False])
@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("required", [True, False])
def test_identity_gate_is_covered_only_by_public_attribution(
    reason_matches, allowed, required
):
    reason = "private_patterns_not_configured" if reason_matches else "other"
    result = gate(
        identity_gate={"status": "fail", "reason": reason},
        allowed_identities_configured=allowed,
        require_private_identity=required,
    )
    covered = reason_matches and allowed and not required
    assert ("identity_gate" not in result.names) is covered
    if covered:
        assert result.identity_gate_override == {
            "status": "not_configured",
            "reason": "private_patterns_not_configured",
            "covered_by": "public_attribution",
        }
    else:
        assert result.identity_gate_override is None


@pytest.mark.parametrize("status", STATUSES)
def test_required_private_identity_is_always_gated(status):
    required = gate(
        require_private_identity=True, private_identity_report={"status": status}
    )
    assert "private_identity_scan" in required.names
    optional = gate(private_identity_report={"status": status})
    assert ("private_identity_scan" in optional.names) is (status != "not_configured")


def test_covered_identity_override_is_a_fresh_value():
    first = GATING_CASES["covered_identity"].identity_gate_override
    first["status"] = "changed"
    assert GATING_CASES["covered_identity"].identity_gate_override is first
    assert (
        gate(
            identity_gate={
                "status": "fail",
                "reason": "private_patterns_not_configured",
            },
            allowed_identities_configured=True,
        ).identity_gate_override["status"]
        == "not_configured"
    )


def expected_status(statuses):
    if "tool_error" in statuses:
        return "tool_error"
    if "fail" in statuses or "unknown" in statuses:
        return "blocked"
    return "pass"


@pytest.mark.parametrize("case", sorted(GATING_CASES))
@pytest.mark.parametrize("tool_error", [True, False])
@pytest.mark.parametrize("fail", [True, False])
@pytest.mark.parametrize("unknown", [True, False])
def test_aggregate_status_priority_for_each_gated_set(case, tool_error, fail, unknown):
    names = sorted(GATING_CASES[case].names)
    planted = [
        status
        for status, wanted in (
            ("tool_error", tool_error),
            ("fail", fail),
            ("unknown", unknown),
        )
        if wanted
    ]
    checks = {name: {"status": "pass"} for name in names}
    checks["human_visual_review"] = {"status": "fail"}
    checks["outside_gate"] = {"status": "tool_error"}
    for offset, status in enumerate(planted):
        checks[names[offset * 3]] = {"status": status}
    assert REPORT.aggregate_status(checks, GATING_CASES[case].names) == (
        expected_status(planted)
    )


@pytest.mark.parametrize("status", STATUSES)
def test_aggregate_status_reads_every_gated_check(status):
    names = GATING_CASES["release"].names
    for name in sorted(names):
        checks = {other: {"status": "pass"} for other in names}
        checks[name] = {"status": status}
        assert REPORT.aggregate_status(checks, names) == expected_status([status])


def test_moved_report_names_are_republished_by_the_facade():
    for name in (
        "SCHEMA",
        "GUARANTEES",
        "NON_GUARANTEES",
        "boundary_sections",
        "format_boundary_text",
        "dialogue_exit_code",
        "format_check_line",
        "format_human_report",
        "unexpected_exception_issue",
        "report_exit_code",
    ):
        assert getattr(MODULE, name) is getattr(REPORT, name)
