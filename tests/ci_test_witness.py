import argparse
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree


def collection_facts(path):
    if not path.exists():
        return {"schema": "ci-test-collection/v1", "status": "not_created"}
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return {"schema": "ci-test-collection/v1", "status": "unavailable"}
    cases = list(root.iter("testcase"))
    identities = sorted(
        (case.get("classname", ""), case.get("name", "")) for case in cases
    )
    return {
        "schema": "ci-test-collection/v1",
        "status": "complete",
        "case_count": len(cases),
        "case_sha256": hashlib.sha256(
            json.dumps(identities, separators=(",", ":")).encode()
        ).hexdigest(),
        "skipped": sum(case.find("skipped") is not None for case in cases),
        "failed": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(collection_facts(args.junit), sort_keys=True))
