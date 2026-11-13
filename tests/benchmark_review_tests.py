"""Compare the review test family in two trusted local Git trees (POSIX only)."""

import argparse
from collections import Counter
from hashlib import sha256
import importlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

import pytest

MATRIX = [
    "test_review_3c" + suffix
    for suffix in ("", "_extra", "_human", "_route", "_tighten")
]
COUNTS = Counter()
MATRIX_ACTIVE = False


class Invalid(Exception):
    pass


def command(argv, cwd, env=None):
    child = subprocess.run(argv, cwd=cwd, env=env, capture_output=True)
    if child.returncode:
        raise Invalid("child_failure")
    return child.stdout


def snapshot(repo, ref, dest):
    tree = (
        command(
            ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{tree}"], repo
        )
        .decode()
        .strip()
    )
    data = command(
        ["git", "-c", "core.autocrlf=false", "archive", "--format=tar", tree], repo
    )
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            path = dest / member.name
            if not path.resolve().is_relative_to(dest) or not (
                member.isdir() or member.isfile()
            ):
                raise Invalid("unsafe_archive")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
                path.chmod(member.mode & 0o755)
    return tree


def ledger():
    sys.path.insert(0, str(Path("tests").resolve()))
    rows = []
    for name in MATRIX:
        module = importlib.import_module(name)
        for expected, table in [(0, module.GREEN), (1, module.RED)]:
            rows.extend(
                (
                    sha256(("試験\n\n" + text + "\n").encode("utf-8")).hexdigest(),
                    expected,
                )
                for text in table.values()
            )
    unique = sorted(set(rows))
    digest = sha256(json.dumps(unique, separators=(",", ":")).encode()).hexdigest()
    return {"rows": len(rows), "unique": len(unique), "canonical_sha256": digest}


# Loaded only by explicit audit runs (-p), never by normal pytest discovery.
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    global MATRIX_ACTIVE
    previous = MATRIX_ACTIVE
    module = Path(str(item.path)).stem
    MATRIX_ACTIVE = module in MATRIX and (
        module != "test_review_3c"
        or item.originalname
        in {"test_review_3c_canary_is_red", "test_review_3c_control_is_green"}
    )
    if MATRIX_ACTIVE:
        COUNTS["matrix_cases"] += 1
    try:
        yield
    finally:
        MATRIX_ACTIVE = previous


def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput"):
        return
    gate = str(Path("scripts/identity_gate.py").resolve())

    def audit(event, args):
        if event == "subprocess.Popen":
            executable, argv = args[:2]
            if executable == sys.executable:
                if not isinstance(argv, (list, tuple)):
                    raise Invalid("unsupported_audit_argv")
                is_gate = len(argv) > 1 and argv[1] == gate
                COUNTS["gate" if is_gate else "other_python"] += 1
                if is_gate and MATRIX_ACTIVE:
                    COUNTS["matrix_gate"] += 1
                if is_gate and "--message-file" not in argv:
                    COUNTS["non_message_gate"] += 1
            elif Path(os.fsdecode(executable)).name == "git":
                COUNTS["git"] += 1

    def profile(frame, event, arg):
        if (
            event == "call"
            and frame.f_code.co_name == "main"
            and frame.f_code.co_filename == gate
        ):
            COUNTS["main"] += 1
            if MATRIX_ACTIVE:
                COUNTS["matrix_main"] += 1

    sys.addaudithook(audit)
    sys.setprofile(profile)


def pytest_testnodedown(node, error):
    COUNTS.update(node.workeroutput["review_benchmark_counts"])
    COUNTS["workers"] += 1


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):
        sys.setprofile(None)
        session.config.workeroutput["review_benchmark_counts"] = dict(COUNTS)
    else:
        Path(os.environ["REVIEW_BENCHMARK_AUDIT"]).write_text(
            json.dumps(COUNTS), encoding="utf-8"
        )


def checked_summary(output, code):
    match = re.search(r"(?m)^(?:=+ )?(\d+) passed in [\d.]+s(?:\s|=|$)", output)
    if code or not match or not int(match[1]):
        raise Invalid("incomplete_pytest_scope")
    return int(match[1])


def verify_work(label, counts, rows):
    if (
        counts.get("matrix_cases", 0) != rows
        or counts.get("matrix_gate", 0) + counts.get("matrix_main", 0) != rows
        or (label == "candidate" and counts.get("matrix_gate", 0))
    ):
        raise Invalid("insufficient_matrix_work")
    if min(counts.get(k, 0) for k in ("git", "gate", "non_message_gate")) <= 0:
        raise Invalid("insufficient_cold_work")


def run_family(root, env, workers, audit=None):
    files = sorted(
        p.relative_to(root).as_posix()
        for p in (root / "tests").glob("test_review_3c*.py")
    )
    expected_files = {"tests/" + name + ".py" for name in MATRIX}
    if set(files) not in (
        expected_files,
        expected_files | {"tests/test_review_3c_runner.py"},
    ):
        raise Invalid("unexpected_test_family")
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--color=no",
        "-p",
        "no:cacheprovider",
        "-n",
        workers,
    ]
    if audit:
        argv.extend(["-p", "review_benchmark_probe"])
        env = dict(env, REVIEW_BENCHMARK_AUDIT=str(audit), PYTHONPATH=str(audit.parent))
    start = time.perf_counter()
    result = subprocess.run(argv + files, cwd=root, env=env, capture_output=True)
    elapsed = time.perf_counter() - start
    return {
        "passed": checked_summary(
            result.stdout.decode(errors="replace"), result.returncode
        ),
        "seconds": elapsed,
        "files": files,
        "load1_after": os.getloadavg()[0],
    }


def allowed_comparison_paths(paths):
    metadata = {
        b".test-selection.json",
        b"OPERATIONS.md",
        b"docs/test-efficiency.md",
        b"docs/runtime-support.md",
    }
    return all(
        p in metadata or p.startswith((b"tests/", b".github/workflows/")) for p in paths
    )


def matched_ledgers(base, candidate):
    if (
        base["unique"] <= 0
        or base["unique"] != candidate["unique"]
        or base["canonical_sha256"] != candidate["canonical_sha256"]
        or base["rows"] < base["unique"]
        or candidate["rows"] != candidate["unique"]
    ):
        raise Invalid("matrix_changed")


def compare(args, repo, temp):
    if (
        os.name != "posix"
        or args.samples < 5
        or not re.fullmatch(r"auto|[1-9]\d*", args.workers)
    ):
        raise Invalid("unsupported_parameters")
    toolchain = (sys.version.split()[0], version("pytest"), version("pytest-xdist"))
    roots = {label: temp / label for label in ("base", "candidate")}
    trees = {k: snapshot(repo, getattr(args, k + "_ref"), v) for k, v in roots.items()}
    diff = command(["git", "diff", "--name-only", "-z", *trees.values()], repo)
    if not allowed_comparison_paths([p for p in diff.split(b"\0") if p]):
        raise Invalid("non_test_sources_changed")
    binary = Path(
        os.environ.get("REPO_PREFLIGHT_GITLEAKS") or shutil.which("gitleaks") or ""
    ).absolute()
    digest = sha256(binary.read_bytes()).hexdigest()
    pins = json.loads((roots["base"] / "references/gitleaks-pins.json").read_text())
    if (
        digest not in pins["binary_sha256"].values()
        or not os.access(binary, os.X_OK)
        or binary.name != "gitleaks"
    ):
        raise Invalid("unverified_gitleaks")
    env = dict(
        os.environ,
        REPO_PREFLIGHT_GITLEAKS=str(binary),
        PATH=os.pathsep.join(
            [str(binary.parent), str(Path(sys.executable).parent), os.environ["PATH"]]
        ),
    )
    for key in (
        "IDENTITY_GATE_SCRIPT",
        "PYTEST_ADDOPTS",
        "PYTHONPATH",
        "PYTEST_PLUGINS",
    ):
        env.pop(key, None)
    script = Path(__file__).resolve()
    shutil.copyfile(script, temp / "review_benchmark_probe.py")
    scopes = {
        label: json.loads(command([sys.executable, str(script), "_ledger"], root, env))
        for label, root in roots.items()
    }
    matched_ledgers(scopes["base"], scopes["candidate"])
    reports = {}
    for label, root in roots.items():
        scope = scopes[label]
        audit = temp / (label + ".counts")
        warm = run_family(root, env, args.workers, audit)
        if warm["passed"] < scope["rows"]:
            raise Invalid("unexpected_case_count")
        counts = dict.fromkeys(
            (
                "gate",
                "other_python",
                "git",
                "main",
                "non_message_gate",
                "workers",
                "matrix_cases",
                "matrix_gate",
                "matrix_main",
            ),
            0,
        )
        counts.update(json.loads(audit.read_text()))
        verify_work(label, counts, scope["rows"])
        reports[label] = {
            "tree": trees[label],
            "matrix": scope,
            "audit": counts,
            "warmup": warm,
            "samples": [],
        }
    if (
        reports["candidate"]["warmup"]["passed"] - scopes["candidate"]["rows"]
        < reports["base"]["warmup"]["passed"] - scopes["base"]["rows"]
    ):
        raise Invalid("non_matrix_cases_lost")
    old, new = (reports[k]["audit"] for k in roots)
    if any(old[k] != new[k] for k in ("non_message_gate", "git", "workers")):
        raise Invalid("unmatched_work")
    for i in range(args.samples):
        for label in list(roots)[:: 1 if i % 2 == 0 else -1]:
            sample = run_family(roots[label], env, args.workers)
            if sample["passed"] != reports[label]["warmup"]["passed"]:
                raise Invalid("scope_drift")
            reports[label]["samples"].append(sample)
    if (
        toolchain
        != (sys.version.split()[0], version("pytest"), version("pytest-xdist"))
        or sha256(binary.read_bytes()).hexdigest() != digest
    ):
        raise Invalid("toolchain_changed")
    for report in reports.values():
        times = [s["seconds"] for s in report["samples"]]
        report.update(min=min(times), median=statistics.median(times), max=max(times))
    base, candidate = reports.values()
    verdict = (
        "FASTER"
        if candidate["max"] < base["min"]
        else "SLOWER" if base["max"] < candidate["min"] else "INCONCLUSIVE"
    )
    return {
        "variants": reports,
        "verdict": verdict,
        "order": "alternating AB/BA",
        "audit_scope": "xdist workers only; no grandchildren",
        "mode": "matched-family",
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "cpu_count": os.cpu_count(),
        "workers": args.workers,
        "pytest": version("pytest"),
        "xdist": version("pytest-xdist"),
        "gitleaks_sha256": digest,
        "benchmark_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "cacheprovider": False,
        "instrumented_warmups_in_timings": False,
        "limits": "matched review family only; not overall CI speedup",
    }


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Invalid("invalid_arguments")


def main():
    parser = Parser(description=__doc__)
    for key in ("base-ref", "candidate-ref"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new report path")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--workers", default="auto")
    args = parser.parse_args()
    if args.output.exists():
        raise Invalid("output_exists")
    with tempfile.TemporaryDirectory() as tmp:
        report = compare(args, Path.cwd(), Path(tmp).resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as f:
        staged = Path(f.name)
    try:
        staged.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        os.link(staged, args.output)
    finally:
        staged.unlink()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["_ledger"]:
            print(json.dumps(ledger()))
        else:
            main()
    except Exception as exc:
        print(
            str(exc) if isinstance(exc, Invalid) else "benchmark_failure",
            file=sys.stderr,
        )
        sys.exit(1)
