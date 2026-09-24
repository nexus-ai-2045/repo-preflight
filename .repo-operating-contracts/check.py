import hashlib
import json
from pathlib import Path


MANAGED_DIR = ".repo-operating-contracts"


def _sha256(path: Path) -> str:
    normalized = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _managed_path(root: Path, relative: str) -> Path | None:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts or candidate.parts[0] != MANAGED_DIR:
        return None
    return root / candidate


def check(root: Path) -> dict[str, object]:
    manifest_path = root / ".repo-operating-contracts" / "manifest.json"
    missing: list[str] = []
    drifted: list[str] = []
    errors: list[str] = []

    if not manifest_path.exists():
        missing.append(".repo-operating-contracts/manifest.json")
        manifest: dict[str, object] = {}
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(f"invalid_manifest:{exc}")
            manifest = {}

    managed_files = manifest.get("managed_files", {})
    if not isinstance(managed_files, dict):
        errors.append("invalid_managed_files")
        managed_files = {}
    for relative, expected_hash in managed_files.items():
        path = _managed_path(root, str(relative))
        if path is None:
            errors.append(f"invalid_managed_path:{relative}")
            continue
        if not path.exists():
            missing.append(str(relative))
        elif _sha256(path) != expected_hash:
            drifted.append(str(relative))

    if not (root / "REPO_GOAL.md").exists():
        missing.append("REPO_GOAL.md")
    agents_path = root / "AGENTS.md"
    if not agents_path.exists():
        missing.append("AGENTS.md")
    elif ".repo-operating-contracts/manifest.json" not in agents_path.read_text(encoding="utf-8"):
        errors.append("agent_entrypoint_not_integrated")
    if manifest.get("hook_installed") is not False:
        errors.append("hook_install_state_must_be_false")
    if manifest.get("runtime_skill_installed") is not False:
        errors.append("runtime_skill_install_state_must_be_false")

    return {
        "schema": "repo_operating_contracts.consumer_check.v1",
        "ok": not missing and not drifted and not errors,
        "missing": sorted(missing),
        "drifted": sorted(drifted),
        "errors": errors,
        "external_actions_performed": False,
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = check(root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
