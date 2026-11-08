#!/usr/bin/env python3
"""信頼済み同梱検査器で bare Git の保存データを検査し、値なし結果だけ返す。

公開secret/path検査とprivate検査を、対象のworkflow・baseline・例外から独立して行う。
検査範囲は同梱helperが対応するbytes・文字列復号・正規化のみ。画像の画素、
暗号化、未対応コンテナの意味内容について個人情報の不存在を保証しない。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

SCHEMA = "repo-preflight.git-data-privacy/v1"
SCOPE = {
    "objects": "all_stored_commit_tree_blob_tag_bytes",
    "refs": "all_refs_and_symbolic_head",
    "identity": "all_reachable_commits_and_expected_head",
    "capability": "existing_decoded_text_normalization_only",
    "not_covered": ["image_pixels", "encrypted_content", "unsupported_containers"],
}
_LOCK = threading.RLock()
_OID = re.compile(r"[0-9a-f]{40}")
_SOURCE = Path(__file__).resolve().parent


class GateError(Exception):
    """値を含まない固定reason codeだけを運ぶ。"""


@contextlib.contextmanager
def _policy_environment(denylist: Path, identity_policy: Path):
    values = {
        "PUBLIC_IDENTITY_GUARD_SCRIPT": None,
        "PUBLIC_IDENTITY_DENYLIST": str(denylist),
        "PREFLIGHT_PRIVATE_PATTERNS_FILE": str(identity_policy),
        "PREFLIGHT_PRIVATE_PATTERNS": None,
    }
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load(name: str):
    # candidate の sys.path / 設定による module 選択を許さない。
    path = _SOURCE / (name + ".py")
    spec = importlib.util.spec_from_file_location("_git_data_" + name, path)
    if spec is None or spec.loader is None:
        raise GateError("bundled_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    return module


def _git_env():
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_ATTR_NOSYSTEM="1",
        LC_ALL="C",
    )
    return env


@contextlib.contextmanager
def _process(repo: Path, *args: str):
    command = [
        "git",
        "--no-replace-objects",
        "--no-pager",
        "--git-dir=" + str(repo),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "log.showSignature=false",
        "-c",
        "i18n.logOutputEncoding=UTF-8",
        "-c",
        "protocol.allow=never",
        *args,
    ]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_git_env(),
        cwd=repo,
        shell=False,
    )
    try:
        yield proc
        if proc.wait(timeout=30) != 0:
            raise GateError("git_read_failed")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


def _read(repo: Path, *args: str, limit: int) -> bytes:
    with _process(repo, *args) as proc:
        data = proc.stdout.read(limit + 1)
        if len(data) > limit:
            raise GateError("git_output_limit")
        return data


def _policy_path(value: Path, repo: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise GateError("policy_input_invalid")
    resolved = path.resolve(strict=True)
    if path.is_relative_to(repo) or resolved.is_relative_to(repo):
        raise GateError("policy_inside_store")
    if not resolved.is_file() or not 0 < resolved.stat().st_size <= 1024 * 1024:
        raise GateError("policy_input_invalid")
    return resolved


def _report(expected_head):
    return {
        "schema": SCHEMA,
        "status": "tool_error",
        "expected_head": (
            expected_head
            if isinstance(expected_head, str) and _OID.fullmatch(expected_head)
            else None
        ),
        "scope": dict(SCOPE),
        "object_count": 0,
        "ref_count": 0,
        "byte_count": 0,
        "finding_counts": {
            "private_text": 0,
            "identity": 0,
            "public_secret": 0,
            "public_path": 0,
        },
        "reason_codes": [],
        "hashes": {},
    }


def scan_store(
    repo: Path,
    denylist: Path,
    identity_policy: Path,
    expected_head: str,
    *,
    max_objects=100000,
    max_object_bytes=16 * 1024 * 1024,
    max_total_bytes=512 * 1024 * 1024,
) -> dict:
    """全件が検査できた時だけ pass / blocked。欠落・上限超過は tool_error。

    環境変数を使う既存APIの呼出しはプロセス内で直列化し、必ず復元する。
    同じプロセス内の他の利用者は、検査中にpolicy環境を書き換えないこと。
    """
    report = _report(expected_head)
    with (
        _LOCK,
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        try:
            if report["expected_head"] is None:
                raise GateError("expected_head_invalid")
            if any(
                type(value) is not int or value <= 0
                for value in (max_objects, max_object_bytes, max_total_bytes)
            ):
                raise GateError("limits_invalid")
            repo = Path(repo).resolve(strict=True)
            if not repo.is_dir():
                raise GateError("bare_store_required")
            provider = os.environ.get("PUBLIC_IDENTITY_GUARD_SCRIPT")
            if (
                provider
                and Path(provider).resolve() != _SOURCE / "identity_normalization.py"
            ):
                raise GateError("external_provider_rejected")
            denylist = _policy_path(denylist, repo)
            identity_policy = _policy_path(identity_policy, repo)
            report["hashes"] = {
                "denylist_sha256": hashlib.sha256(denylist.read_bytes()).hexdigest(),
                "identity_policy_sha256": hashlib.sha256(
                    identity_policy.read_bytes()
                ).hexdigest(),
            }
            for name in (
                "readiness_scan",
                "identity_gate",
                "identity_normalization",
                "git_data_privacy_gate",
            ):
                report["hashes"][name + "_sha256"] = hashlib.sha256(
                    (_SOURCE / (name + ".py")).read_bytes()
                ).hexdigest()
            if _read(repo, "rev-parse", "--is-bare-repository", limit=16) != b"true\n":
                raise GateError("bare_store_required")
            # 別store / 不完全履歴へ検査対象を暗黙に拡張・縮小しない。
            for relative in (
                "objects/info/alternates",
                "objects/info/http-alternates",
                "info/grafts",
                "shallow",
            ):
                if (repo / relative).exists():
                    raise GateError("unsupported_store_indirection")
            if _read(repo, "cat-file", "-t", expected_head, limit=16) != b"commit\n":
                raise GateError("expected_head_not_commit")
            scanner_module = _load("readiness_scan")
            identity = _load("identity_gate")
            normalizer = _load("identity_normalization")
            with _policy_environment(denylist, identity_policy):
                scanner = scanner_module.PrivateIdentityScan(repo, required=True)
                if scanner.result().get("status") != "pass":
                    raise GateError("private_scanner_unavailable")
                policy = identity.load_policy()
                metadata = []
                seen = set()
                total = 0
                with _process(
                    repo,
                    "cat-file",
                    "--batch-all-objects",
                    "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                ) as proc:
                    while line := proc.stdout.readline(256):
                        parts = line.rstrip(b"\n").split(b" ")
                        if (
                            len(parts) != 3
                            or not re.fullmatch(rb"[0-9a-f]{40}", parts[0])
                            or not parts[2].isdigit()
                        ):
                            raise GateError("object_metadata_invalid")
                        oid, kind, size = (
                            parts[0].decode("ascii"),
                            parts[1].decode("ascii"),
                            int(parts[2]),
                        )
                        if kind not in {"commit", "tree", "blob", "tag"}:
                            raise GateError("object_type_unsupported")
                        if oid in seen:
                            raise GateError("object_metadata_invalid")
                        seen.add(oid)
                        total += size
                        if len(seen) > max_objects:
                            raise GateError("object_count_limit")
                        if size > max_object_bytes:
                            raise GateError("object_size_limit")
                        if total > max_total_bytes:
                            raise GateError("total_bytes_limit")
                        metadata.append((oid, kind, size))
                if expected_head not in seen:
                    raise GateError("expected_head_unscanned")
                # 保存object一覧だけでは、欠落したreachable blobを検出できない。
                # pathを出力させず、走査対象が全て実在することを先に確認する。
                reachable_count = 0
                with _process(
                    repo,
                    "rev-list",
                    "--objects",
                    "--no-object-names",
                    "--all",
                    expected_head,
                ) as proc:
                    while line := proc.stdout.readline(64):
                        oid = line.rstrip(b"\n").decode("ascii")
                        if oid not in seen:
                            raise GateError("reachable_object_unscanned")
                        reachable_count += 1
                        if reachable_count > max_objects:
                            raise GateError("object_count_limit")

                def check(data):
                    # 対象の公開CIや例外設定を信頼せず、同梱規則で独立検査する。
                    # helperが返す値由来のhashもreportへ出さず、件数だけを集計する。
                    report["finding_counts"]["public_secret"] += sum(
                        scanner_module.secret_matches(data).values()
                    )
                    report["finding_counts"]["public_path"] += sum(
                        scanner_module.normalized_personal_path_matches(
                            data, normalizer=normalizer
                        ).values()
                    )
                    scanner.check(data)
                    if scanner.result().get("status") not in {"pass", "fail"}:
                        raise GateError("private_scanner_failed")
                    report["byte_count"] += len(data)
                    report["finding_counts"]["private_text"] = scanner.findings

                for oid, kind, size in metadata:
                    data = _read(repo, "cat-file", kind, oid, limit=size)
                    if len(data) != size:
                        raise GateError("object_size_changed")
                    check(data)
                    report["object_count"] += 1
                with _process(
                    repo,
                    "for-each-ref",
                    "--format=%(objectname)%00%(refname)%00%(symref)",
                ) as proc:
                    while line := proc.stdout.readline(max_object_bytes + 1):
                        if len(line) > max_object_bytes:
                            raise GateError("ref_size_limit")
                        fields = line.rstrip(b"\n").split(b"\0")
                        if len(fields) != 3 or fields[0].decode("ascii") not in seen:
                            raise GateError("ref_target_unscanned")
                        if report["ref_count"] >= max_objects:
                            raise GateError("ref_count_limit")
                        if report["byte_count"] + len(line) > max_total_bytes:
                            raise GateError("total_bytes_limit")
                        check(line)
                        report["ref_count"] += 1
                # HEADは通常のrefs一覧に現れない。ファイル内容もboundedで照合。
                with (repo / "HEAD").open("rb") as stream:
                    head_data = stream.read(max_object_bytes + 1)
                if len(head_data) > max_object_bytes:
                    raise GateError("ref_size_limit")
                if report["byte_count"] + len(head_data) > max_total_bytes:
                    raise GateError("total_bytes_limit")
                check(head_data)
                report["ref_count"] += 1

                commit_sizes = {
                    oid: size for oid, kind, size in metadata if kind == "commit"
                }

                def git_fn(_repo, *args):
                    # 同梱identity APIだけに渡す。整形文字列ではなくraw commitを読む。
                    allowed = (
                        len(args) == 2
                        and args[0] == "rev-list"
                        and (args[1] == "--all" or _OID.fullmatch(args[1]))
                    ) or (
                        len(args) == 3
                        and args[0] == "cat-file"
                        and args[1] in {"commit", "-s"}
                        and _OID.fullmatch(args[2])
                    )
                    if not allowed:
                        raise GateError("identity_git_operation_invalid")
                    if args[0] == "rev-list":
                        limit = 41 * len(seen)
                    else:
                        if args[2] not in commit_sizes:
                            raise GateError("identity_commit_unscanned")
                        limit = 64 if args[1] == "-s" else commit_sizes[args[2]]
                    return _read(repo, *args, limit=limit).decode("utf-8")

                issues = identity.check_range(policy, str(repo), "--all", git_fn=git_fn)
                issues += identity.check_range(
                    policy, str(repo), expected_head, git_fn=git_fn
                )
                report["finding_counts"]["identity"] = len(issues)
                # 初期snapshot以降にpolicyが変わった場合、異なる入力の結果にしない。
                for path, key in (
                    (denylist, "denylist_sha256"),
                    (identity_policy, "identity_policy_sha256"),
                ):
                    if (
                        hashlib.sha256(path.read_bytes()).hexdigest()
                        != report["hashes"][key]
                    ):
                        raise GateError("policy_input_changed")
                if scanner.result().get("status") == "fail":
                    report["reason_codes"].append("private_text_detected")
                elif scanner.result().get("status") != "pass":
                    raise GateError("private_scanner_failed")
                if issues:
                    report["reason_codes"].append("identity_policy_violation")
                if report["finding_counts"]["public_secret"]:
                    report["reason_codes"].append("public_secret_detected")
                if report["finding_counts"]["public_path"]:
                    report["reason_codes"].append("public_path_detected")
                report["status"] = "blocked" if report["reason_codes"] else "pass"
        except GateError as error:
            report["status"] = "tool_error"
            report["reason_codes"] = [str(error)]
        except (Exception, SystemExit):
            report["status"] = "tool_error"
            report["reason_codes"] = ["scan_input_or_execution_failed"]
    return report


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise GateError("cli_arguments_invalid")


def main(argv=None):
    try:
        parser = _Parser(description="bare Gitデータの個人情報検査（値なしJSON）")
        for option in ("repo", "denylist", "identity-policy", "expected-head"):
            parser.add_argument("--" + option, required=True)
        args = parser.parse_args(argv)
        report = scan_store(
            Path(args.repo),
            Path(args.denylist),
            Path(args.identity_policy),
            args.expected_head,
        )
    except GateError:
        report = _report(None)
        report["reason_codes"] = ["cli_arguments_invalid"]
    except SystemExit as error:
        if error.code == 0:
            return 0
        report = _report(None)
        report["reason_codes"] = ["cli_arguments_invalid"]
    except Exception:
        report = _report(None)
        report["reason_codes"] = ["scan_input_or_execution_failed"]
    print(json.dumps(report, sort_keys=True))
    return {"pass": 0, "blocked": 1, "tool_error": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
