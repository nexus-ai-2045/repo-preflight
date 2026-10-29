#!/usr/bin/env python3
"""base に登録された SSH 公開鍵で、保護変更の判断 chain を検査する。

Git object だけを読み、HEAD のコードを実行しない。署名は登録鍵の使用を示すもので、
物理的な人間の操作を証明するものではない。base の鮮度は呼出元が保証する。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TRUST_PATH = ".repo-preflight/decision-trust.json"
BUNDLE_PATH = ".repo-preflight/decision-bundle.json"
NAMESPACE = "repo-preflight-protected-change"
MAX_BYTES = 256 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")


class DecisionError(ValueError):
    """値を含めない、固定の拒否理由。"""


def _require(condition, reason):
    if not condition:
        raise DecisionError(reason)


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _no_number(value):
    raise DecisionError("noninteger_json_number")


def _json(raw):
    _require(len(raw) <= MAX_BYTES, "json_too_large")
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_object,
        parse_float=_no_number,
        parse_constant=_no_number,
    )


def _fields(value, keys):
    _require(type(value) is dict and set(value) == set(keys.split()), "invalid_fields")


def _integer(value, maximum=2**63 - 1):
    _require(type(value) is int and 0 < value <= maximum, "invalid_positive_integer")


def _text(value, limit):
    _require(
        type(value) is str
        and 0 < len(value) <= limit
        and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value),
        "invalid_text",
    )


def _id(value):
    _require(
        type(value) is str and IDENTIFIER.fullmatch(value) is not None,
        "invalid_identifier",
    )


def _hash(value):
    _require(type(value) is str and HEX.fullmatch(value) is not None, "invalid_sha256")


def _path(value):
    _text(value, 4096)
    _require(
        not value.startswith("/")
        and "\\" not in value
        and all(p not in ("", ".", "..") for p in value.split("/")),
        "invalid_path",
    )


def _paths(value, ordered=True):
    _require(type(value) is list and 0 < len(value) <= 4096, "invalid_paths")
    for item in value:
        _path(item)
    _require(
        len(value) == len(set(value)) and (not ordered or value == sorted(value)),
        "paths_not_sorted_unique",
    )


def _identity(value):
    _integer(value["repository_id"])
    _require(
        type(value["repository"]) is str
        and len(value["repository"]) <= 256
        and REPOSITORY.fullmatch(value["repository"]) is not None,
        "invalid_repository",
    )
    _require(
        all(part not in (".", "..") for part in value["repository"].split("/")),
        "invalid_repository",
    )
    ref = value["base_ref"]
    _text(ref, 1024)
    _require(
        ref.startswith("refs/heads/")
        and not any(
            x in ref
            for x in ("..", "@{", "//", "\\", " ", "~", "^", ":", "?", "*", "[")
        )
        and not ref.endswith(("/", ".", ".lock"))
        and all(
            not part.startswith(".") and not part.endswith(".lock")
            for part in ref.split("/")
        ),
        "invalid_base_ref",
    )


def _git(repo, *args):
    env = dict(
        os.environ,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, timeout=5, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DecisionError("git_unavailable") from exc
    _require(result.returncode == 0, "git_object_unreadable")
    return result.stdout


def _blob(repo, revision, path):
    entry = _git(repo, "ls-tree", "-z", revision, "--", path)
    records = entry.split(b"\0")
    _require(len(records) == 2 and records[-1] == b"", "missing_or_ambiguous_blob")
    metadata, name = records[0].split(b"\t", 1)
    mode, kind, oid = metadata.split(b" ")
    _require(
        name == path.encode("utf-8") and mode == b"100644" and kind == b"blob",
        "invalid_blob_mode",
    )
    size = int(_git(repo, "cat-file", "-s", oid.decode("ascii")))
    _require(0 < size <= MAX_BYTES, "blob_size_limit")
    return _git(repo, "cat-file", "blob", oid.decode("ascii"))


def _delta_records(repo, base, head, paths):
    _require(
        type(base) is str
        and re.fullmatch("[0-9a-f]{40}", base) is not None
        and type(head) is str
        and re.fullmatch("[0-9a-f]{40}", head) is not None,
        "invalid_commit",
    )
    _paths(paths, ordered=False)
    merge = _git(repo, "merge-base", "--all", base, head).decode("ascii").splitlines()
    _require(len(merge) == 1, "ambiguous_merge_base")
    selected = [p for p in paths if p != BUNDLE_PATH]
    if not selected:
        return []
    raw = _git(
        repo,
        "diff",
        "--raw",
        "--no-abbrev",
        "--no-renames",
        "--no-ext-diff",
        "-z",
        merge[0],
        head,
        "--",
        *[":(literal)" + p for p in selected],
    )
    parts = raw.split(b"\0")
    _require(parts[-1] == b"" and len(parts) % 2 == 1, "invalid_raw_diff")
    result = []
    for index in range(0, len(parts) - 1, 2):
        metadata = parts[index].decode("ascii").split()
        _require(len(metadata) == 5 and metadata[0].startswith(":"), "invalid_raw_diff")
        path = parts[index + 1].decode("utf-8")
        _path(path)
        _require(path in selected, "unexpected_delta_path")
        result.append(
            {
                "path": path,
                "oldblob": metadata[2],
                "newblob": metadata[3],
                "oldmode": metadata[0][1:],
                "newmode": metadata[1],
            }
        )
    return sorted(result, key=lambda row: row["path"])


def protected_delta(repo: Path, base: str, head: str, paths: list[str]) -> str:
    """carrier を除く保護パスの raw Git 差分を canonical JSON として hash 化する。"""
    return hashlib.sha256(
        _canonical(_delta_records(repo, base, head, paths))
    ).hexdigest()


def _public_key(value):
    _require(
        type(value) is str
        and len(value) <= 256
        and re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", value) is not None,
        "invalid_public_key",
    )
    encoded = value.split(" ")[1]
    blob = base64.b64decode(encoded, validate=True)
    _require(
        base64.b64encode(blob).decode("ascii") == encoded
        and len(blob) == 51
        and blob[:19] == b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20",
        "invalid_public_key",
    )


def _trust(raw):
    value = _json(raw)
    _fields(
        value,
        "schema repository_id repository base_ref max_validity_seconds signers revoked_payload_sha256 revoked_signers",
    )
    _require(
        value["schema"] == "repo-preflight.decision-trust/v1", "invalid_trust_schema"
    )
    _identity(value)
    _integer(value["max_validity_seconds"], 2**31 - 1)
    _require(
        type(value["signers"]) is dict and 0 < len(value["signers"]) <= 128,
        "invalid_signers",
    )
    for signer, key in value["signers"].items():
        _id(signer)
        _public_key(key)
    for name, validator in (
        ("revoked_payload_sha256", _hash),
        ("revoked_signers", _id),
    ):
        items = value[name]
        _require(type(items) is list and len(items) <= 4096, "invalid_revocations")
        for item in items:
            validator(item)
        _require(len(items) == len(set(items)), "duplicate_revocation")
    return value


def _time(value):
    _require(
        type(value) is str and TIME.fullmatch(value) is not None, "invalid_timestamp"
    )
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _verify_signature(executable, public, signer, signature, raw):
    _require(
        type(signature) is str
        and 0 < len(signature) <= 16384
        and signature.isascii()
        and signature.startswith("-----BEGIN SSH SIGNATURE-----\n")
        and signature.rstrip("\n").endswith("-----END SSH SIGNATURE-----"),
        "invalid_signature",
    )
    with tempfile.TemporaryDirectory(prefix="decision-verify-") as directory:
        root = Path(directory)
        allowed = root / "allowed"
        sig = root / "signature"
        allowed.write_text(
            signer + ' namespaces="' + NAMESPACE + '" ' + public + "\n",
            encoding="ascii",
        )
        sig.write_text(signature, encoding="ascii")
        try:
            result = subprocess.run(
                [
                    executable,
                    "-Y",
                    "verify",
                    "-n",
                    NAMESPACE,
                    "-I",
                    signer,
                    "-f",
                    str(allowed),
                    "-s",
                    str(sig),
                ],
                input=raw,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DecisionError("signature_verifier_failed") from exc
    _require(result.returncode == 0, "signature_verification_failed")


def verify_decisions(
    repo: Path,
    base: str,
    head: str,
    context: dict,
    protected_paths: list[str],
    now: Optional[datetime] = None,
) -> dict:
    """全署名・期限・取消・親子範囲を検査し、現在の差分に一致する leaf を返す。

    protected_paths は呼出元が列挙した「変更された保護パスの全件」であり、
    部分的な allowlist ではない。列挙の完全性と base の鮮度は呼出元の責務。
    """
    result = {
        "status": "blocked",
        "reason": "invalid_decision",
        "decision_ids": [],
        "protected_delta_sha256": None,
        "assurance": None,
    }
    try:
        _fields(context, "repository_id repository base_ref pull_request")
        _identity(context)
        _integer(context["pull_request"])
        _paths(protected_paths, ordered=False)
        _require(
            TRUST_PATH not in protected_paths, "trust_change_requires_separate_review"
        )
        records = _delta_records(repo, base, head, protected_paths)
        _require(bool(records), "no_protected_delta")
        digest = hashlib.sha256(_canonical(records)).hexdigest()
        result["protected_delta_sha256"] = digest
        raw_trust = _blob(repo, base, TRUST_PATH)
        trust = _trust(raw_trust)
        for key in ("repository_id", "repository", "base_ref"):
            _require(trust[key] == context[key], "trust_context_mismatch")
        policy_hash = hashlib.sha256(raw_trust).hexdigest()
        bundle = _json(_blob(repo, head, BUNDLE_PATH))
        _fields(bundle, "schema decisions")
        _require(
            bundle["schema"] == "repo-preflight.decision-bundle/v1",
            "invalid_bundle_schema",
        )
        decisions = bundle["decisions"]
        _require(
            type(decisions) is list and 0 < len(decisions) <= 8,
            "invalid_decision_count",
        )
        executable = shutil.which("ssh-keygen")
        _require(executable is not None, "ssh_keygen_missing")
        current = now if now is not None else datetime.now(timezone.utc)
        _require(
            isinstance(current, datetime)
            and current.tzinfo is not None
            and current.utcoffset() is not None,
            "invalid_current_time",
        )
        nodes, ids = {}, set()
        for item in decisions:
            _fields(item, "payload_base64 signature")
            _require(
                type(item["payload_base64"]) is str
                and len(item["payload_base64"]) <= 65536,
                "invalid_payload_encoding",
            )
            raw = base64.b64decode(item["payload_base64"], validate=True)
            _require(
                base64.b64encode(raw).decode("ascii") == item["payload_base64"],
                "invalid_payload_encoding",
            )
            payload = _json(raw)
            _require(_canonical(payload) == raw, "noncanonical_payload")
            _fields(
                payload,
                "schema decision_id kind signer purpose parent_payload_sha256 scope issued_at expires_at",
            )
            _require(
                payload["schema"] == "repo-preflight.protected-decision/v1"
                and payload["kind"] in ("design", "change"),
                "invalid_payload_schema",
            )
            _id(payload["decision_id"])
            _id(payload["signer"])
            _text(payload["purpose"], 2048)
            _require(bool(payload["purpose"].strip()), "empty_purpose")
            parent = payload["parent_payload_sha256"]
            if parent is not None:
                _hash(parent)
            _require(
                parent is not None or payload["kind"] == "design", "root_must_be_design"
            )
            scope = payload["scope"]
            _fields(
                scope,
                "repository_id repository base_ref pull_request operation paths protected_delta_sha256 policy_sha256",
            )
            _identity(scope)
            _integer(scope["pull_request"])
            for key in context:
                _require(scope[key] == context[key], "decision_context_mismatch")
            _require(
                scope["operation"] == "protected-change-check"
                and scope["policy_sha256"] == policy_hash,
                "decision_policy_mismatch",
            )
            _paths(scope["paths"])
            _require(
                TRUST_PATH not in scope["paths"] and BUNDLE_PATH not in scope["paths"],
                "invalid_scope_path",
            )
            if payload["kind"] == "design":
                _require(
                    scope["protected_delta_sha256"] is None,
                    "design_digest_must_be_null",
                )
            else:
                _hash(scope["protected_delta_sha256"])
            issued, expires = _time(payload["issued_at"]), _time(payload["expires_at"])
            _require(
                issued <= current < expires
                and 0
                < (expires - issued).total_seconds()
                <= trust["max_validity_seconds"],
                "decision_not_current",
            )
            sha = hashlib.sha256(raw).hexdigest()
            _require(
                sha not in nodes and payload["decision_id"] not in ids,
                "duplicate_decision",
            )
            _require(
                sha not in trust["revoked_payload_sha256"]
                and payload["signer"] not in trust["revoked_signers"],
                "decision_revoked",
            )
            _require(payload["signer"] in trust["signers"], "signer_not_registered")
            _verify_signature(
                executable,
                trust["signers"][payload["signer"]],
                payload["signer"],
                item["signature"],
                raw,
            )
            nodes[sha] = (payload, issued, expires)
            ids.add(payload["decision_id"])
        chains = {}

        def visit(sha, pending):
            _require(sha not in pending, "decision_cycle")
            if sha in chains:
                return chains[sha]
            payload, issued, expires = nodes[sha]
            parent = payload["parent_payload_sha256"]
            if parent is None:
                chain = [payload["decision_id"]]
            else:
                _require(parent in nodes, "parent_missing")
                parent_payload, parent_issued, parent_expires = nodes[parent]
                _require(
                    set(payload["scope"]["paths"])
                    <= set(parent_payload["scope"]["paths"])
                    and issued >= parent_issued
                    and expires <= parent_expires,
                    "parent_scope_expanded",
                )
                if parent_payload["kind"] == "change":
                    _require(
                        payload["kind"] == "change"
                        and payload["scope"]["protected_delta_sha256"]
                        == parent_payload["scope"]["protected_delta_sha256"],
                        "parent_scope_expanded",
                    )
                chain = visit(parent, pending | {sha}) + [payload["decision_id"]]
            chains[sha] = chain
            return chain

        for sha in nodes:
            visit(sha, set())
        parents = {p[0]["parent_payload_sha256"] for p in nodes.values()}
        expected_paths = sorted(p for p in protected_paths if p != BUNDLE_PATH)
        for sha, (payload, _, _) in nodes.items():
            if (
                sha not in parents
                and payload["kind"] == "change"
                and payload["scope"]["paths"] == expected_paths
                and payload["scope"]["protected_delta_sha256"] == digest
            ):
                result.update(
                    status="approved",
                    reason="registered_signature_chain_verified",
                    decision_ids=chains[sha],
                    assurance="registered-signature",
                )
                return result
        raise DecisionError("no_matching_change_leaf")
    except DecisionError as exc:
        result["reason"] = str(exc)
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        OverflowError,
        binascii.Error,
    ):
        result["reason"] = "malformed_decision_data"
    return result
