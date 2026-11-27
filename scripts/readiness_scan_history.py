from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path, PurePosixPath


def fixed_history_revisions(
    repo: Path, rev_args: tuple[str, ...], *, port
) -> tuple[str, ...]:
    """履歴の省略を拒否し、可変refをraw objectの固定OID閉包へ束縛する。"""
    shallow_code, shallow = port.run(
        repo, "git", "rev-parse", "--is-shallow-repository"
    )
    if shallow_code == 0 and shallow == "true":
        raise RuntimeError("shallow_history_unavailable")
    graft_code, graft_path = port.run(
        repo, "git", "rev-parse", "--git-path", "info/grafts"
    )
    if shallow_code or shallow != "false" or graft_code or not graft_path:
        raise RuntimeError("git_raw_history_context_unverified")
    path = Path(graft_path)
    if not path.is_absolute():
        path = repo / path
    if path.exists():
        raise RuntimeError("git_grafts_history_unverified")
    args = (*rev_args, "HEAD") if "--all" in rev_args else rev_args
    commit_tips = ""
    if "--all" in rev_args:
        tip_code, commit_tips = port.run(
            repo, "git", "rev-list", "--no-walk=unsorted", "--all", "HEAD"
        )
        if tip_code:
            raise RuntimeError("git_history_roots_unverified")
    code, values = port.run(repo, "git", "rev-parse", "--revs-only", *args)
    revisions = tuple(dict.fromkeys(values.splitlines() + commit_tips.splitlines()))
    if (
        code
        or not revisions
        or any(
            not re.fullmatch(r"\^?(?:[0-9a-f]{40}|[0-9a-f]{64})", value)
            for value in revisions
        )
    ):
        raise RuntimeError("git_history_roots_unverified")
    return revisions


def changed_committed_entries(
    repo: Path, revisions: tuple[str, ...], *, port
) -> list[tuple]:
    bases = [revision[1:] for revision in revisions if revision.startswith("^")]
    if len(bases) != 1:
        raise RuntimeError("git_history_inventory_failed")
    commits = port.run_subprocess(
        ["git", "rev-list", "--reverse", *revisions],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if commits.returncode:
        raise RuntimeError("git_history_inventory_failed")
    entries = []
    for commit in commits.stdout.splitlines():
        parent = comparison_parent(repo, bases[0], commit, port=port)
        changed = port.run_subprocess(
            [
                "git",
                "diff",
                "--raw",
                "--no-abbrev",
                "--no-renames",
                "-z",
                parent,
                commit,
            ],
            cwd=repo,
            capture_output=True,
        )
        if changed.returncode:
            raise RuntimeError("git_history_inventory_failed")
        records = changed.stdout.split(b"\0")
        if records[-1] != b"" or (len(records) - 1) % 2:
            raise RuntimeError("git_history_inventory_failed")
        for offset in range(0, len(records) - 1, 2):
            fields = records[offset].split()
            if len(fields) != 5 or not fields[0].startswith(b":"):
                raise RuntimeError("git_history_inventory_failed")
            old_oid, new_oid = (value.decode("ascii") for value in fields[2:4])
            if any(
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                for oid in (old_oid, new_oid)
            ):
                raise RuntimeError("git_history_inventory_failed")
            name = records[offset + 1].decode("utf-8", errors="surrogateescape")
            entries.append(
                (parent, commit, name, fields[0][1:], fields[1], old_oid, new_oid)
            )
    return entries


def submission_text(
    data: bytes, names, opaque_suffixes=frozenset(), *, port
) -> str | None:
    suffixes = {PurePosixPath(name).suffix.lower() for name in names}
    if suffixes & opaque_suffixes:
        return None
    if suffixes & {
        ".pdf",
        ".mp3",
        ".mp4",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".psd",
        ".caff",
        ".cmo3",
    }:
        return None
    if data.startswith(
        (b"CAFF", b"8BPS", b"GIF87a", b"GIF89a", b"\x89PNG", b"\xff\xd8")
    ) or port.media_candidate(data, names):
        return None
    text = _decode_scannable_text(data)
    if text is None or any(ord(char) < 32 and char not in "\t\r\n\f" for char in text):
        return None
    return text


def verified_parent_blob(repo: Path, oid: str, *, port) -> bytes | None:
    size = port.run(repo, "git", "cat-file", "-s", oid)
    if size[0] or not size[1].isdigit():
        raise RuntimeError("git_history_inventory_failed")
    if int(size[1]) > port._HISTORY_BATCH_BUDGET_BYTES:
        return None
    blob = port.run_subprocess(
        ["git", "cat-file", "blob", oid], cwd=repo, capture_output=True
    )
    if blob.returncode:
        raise RuntimeError("git_history_inventory_failed")
    data = blob.stdout
    digest = hashlib.new("sha1" if len(oid) == 40 else "sha256", usedforsecurity=False)
    digest.update(f"blob {len(data)}\0".encode("ascii") + data)
    if digest.hexdigest() != oid:
        raise RuntimeError("git_raw_object_digest_mismatch")
    return data


def private_commit_identity_view(data: bytes, expected_identity: str, *, port) -> bytes:
    """Mask only exact approved identity fields for the private denylist view."""
    gate = port.load_identity_gate()
    try:
        expected = gate.parse_commit_identity(expected_identity + " 0 +0000")
        raw = data.decode("utf-8")
        identities, _ = gate.parse_raw_commit(raw)
        if any(value != expected for value in identities.values()):
            raise ValueError("private_commit_identity_mismatch")
        headers, body = data.split(b"\n\n", 1)
        expected_bytes = expected_identity.encode("utf-8")
        lines = []
        for line in headers.split(b"\n"):
            key, separator, value = line.partition(b" ")
            if key in {b"author", b"committer"}:
                # Keep byte lengths and offsets, including other encoding views.
                if not value.startswith(expected_bytes + b" "):
                    raise ValueError("private_commit_identity_mismatch")
                value = b" " * len(expected_bytes) + value[len(expected_bytes) :]
                line = key + separator + value
            lines.append(line)
        return b"\n".join(lines) + b"\n\n" + body
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("private_commit_identity_unverified") from exc


def history_hits(
    repo: Path,
    rev_args: tuple[str, ...] = ("--all",),
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    private_identity: PrivateIdentityScan | None = None,
    *,
    check_repository_names: bool = False,
    approved_private_commit_identity: str | None = None,
    check_raw_commit_payloads: bool = False,
    check_full_history_metadata: bool = False,
    initial_private_save: bool = False,
    port,
) -> tuple[list[str], list[str]]:
    revisions = port.fixed_history_revisions(repo, rev_args)
    check_history_names = initial_private_save or check_full_history_metadata
    if private_identity is not None:
        private_identity.report["closure_revisions_sha256"] = hashlib.sha256(
            "\n".join(revisions).encode("ascii")
        ).hexdigest()
        private_identity.report["replacement_objects_disabled"] = True
        private_identity.report["verified_large_caff_objects_read"] = 0
    objects = port.run_subprocess(
        ["git", "rev-list", "--objects", "--no-object-names", *revisions],
        cwd=repo,
        capture_output=True,
    )
    if objects.returncode:
        raise RuntimeError("git_history_inventory_failed")
    inventory_by_id: dict[str, str] = {}
    for line in objects.stdout.splitlines():
        object_id, _, raw_name = line.partition(b" ")
        try:
            oid = object_id.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if oid:
            inventory_by_id.setdefault(
                oid, raw_name.decode("utf-8", errors="surrogateescape")
            )
    submission_entries = (
        changed_committed_entries(repo, revisions, port=port)
        if check_repository_names
        else []
    )
    opaque_suffixes = (
        {"." + extension for extension in port.load_identity_gate().OPAQUE_EXTENSIONS}
        if check_repository_names
        else frozenset()
    )
    entries_by_blob: dict[str, list[tuple]] = {}
    if check_repository_names:
        # New entries are sent even when their blob already exists at base.
        for entry in submission_entries:
            _, _, name, _, mode, _, oid = entry
            if mode != b"000000" and private_identity is not None:
                for view in {name, PurePosixPath(name).with_suffix("").as_posix()}:
                    before_name_findings = getattr(private_identity, "findings", 0)
                    private_identity.check(
                        view.encode("utf-8", errors="surrogateescape"),
                        repository_name=True,
                    )
                    if getattr(
                        private_identity, "findings", 0
                    ) > before_name_findings and hasattr(
                        private_identity, "record_location"
                    ):
                        private_identity.record_location("filename", field="filename")
            if mode in {b"100644", b"100755", b"120000"}:
                inventory_by_id.setdefault(oid, name)
                entries_by_blob.setdefault(oid, []).append(entry)
    inventory = list(inventory_by_id.items())
    # 空range (base == HEAD 等) は検査対象ゼロの成功。空入力をcat-fileへ
    # 渡すと出力行数が合わず inventory 失敗と誤判定するため、ここで返す
    if not inventory:
        return [], []
    object_ids = [object_id for object_id, _ in inventory]
    check = port.run_subprocess(
        ["git", "cat-file", "--batch-check"],
        cwd=repo,
        input="\n".join(object_ids) + "\n",
        text=True,
        capture_output=True,
    )
    if check.returncode:
        raise RuntimeError("git_history_inventory_failed")
    metadata_lines = check.stdout.splitlines()
    if len(metadata_lines) != len(inventory):
        raise RuntimeError("git_history_inventory_failed")
    eligible: list[tuple[str, str, int, str]] = []
    metadata_eligible: list[tuple[str, int]] = []
    for source, metadata in port._zip_exact(inventory, metadata_lines):
        object_id, name = source
        fields = metadata.split()
        if len(fields) != 3 or fields[0] != object_id:
            raise RuntimeError("git_history_inventory_failed")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if fields[1] not in {"blob", "commit", "tree", "tag"} or size < 0:
            raise RuntimeError("git_history_inventory_failed")
        if (
            fields[1] == "blob"
            or (check_full_history_metadata and fields[1] in {"commit", "tag"})
            or (private_identity is not None and fields[1] in {"commit", "tag", "tree"})
        ):
            submitted_names = tuple(
                entry[2] for entry in entries_by_blob.get(object_id, ())
            )
            if (
                fields[1] == "blob"
                and private_identity is not None
                and getattr(private_identity, "media_inspection", "strict")
                == "metadata"
                and all(
                    entry[4] in {b"100644", b"100755"}
                    for entry in entries_by_blob.get(object_id, ())
                )
                and port._metadata_media_names(submitted_names)
            ):
                metadata_eligible.append((object_id, size))
                continue
            if size <= port._HISTORY_BATCH_BUDGET_BYTES:
                eligible.append((object_id, name, size, fields[1]))
            elif check_full_history_metadata and fields[1] in {"commit", "tag"}:
                raise RuntimeError("git_history_inventory_failed")
            elif private_identity is not None:
                if (
                    fields[1] == "blob"
                    and object_id in private_identity._verified_caff_blobs
                ):
                    eligible.append((object_id, name, size, fields[1]))
                    private_identity.report["verified_large_caff_objects_read"] += 1
                else:
                    private_identity.skipped += 1
    secret_hits: set[str] = set()
    path_hits: set[str] = set()
    checked_names: set[bytes] = set()
    aliases: dict[str, set[str]] = {}
    if (
        check_history_names
        or reviewed_exceptions is not None
        or private_identity is not None
    ):
        # rev-list --objectsでは同一blobの別pathが失われる。全到達treeをNUL区切りで読む。
        roots = port.run_subprocess(
            ["git", "log", "--format=%T", *revisions], cwd=repo, capture_output=True
        )
        if roots.returncode:
            raise RuntimeError("git_history_inventory_failed")
        root_trees = set(roots.stdout.splitlines())
        # A ref can point directly (or through an annotated tag) to a tree,
        # without any commit owning that tree. Preserve its root-relative paths.
        # Do not treat every nested inventory tree as a repository root.
        for revision in revisions:
            if revision.startswith("^"):
                continue
            resolved = port.run_subprocess(
                ["git", "rev-parse", "--verify", revision + "^{tree}"],
                cwd=repo,
                capture_output=True,
            )
            if resolved.returncode == 0:
                root_trees.add(resolved.stdout.strip())
        for tree in root_trees:
            tree_files = port.run_subprocess(
                [
                    "git",
                    "ls-tree",
                    "-r",
                    *(["-t"] if check_history_names else []),
                    "-z",
                    tree.decode("ascii"),
                ],
                cwd=repo,
                capture_output=True,
            )
            if tree_files.returncode:
                raise RuntimeError("git_history_inventory_failed")
            for record in tree_files.stdout.split(b"\0"):
                if not record:
                    continue
                metadata, sep, raw_path = record.partition(b"\t")
                fields = metadata.split()
                if not sep or len(fields) != 3:
                    raise RuntimeError("git_history_inventory_failed")
                if check_history_names:
                    path = raw_path.decode("utf-8", errors="surrogateescape")
                    stem = (
                        PurePosixPath(path)
                        .with_suffix("")
                        .as_posix()
                        .encode("utf-8", errors="surrogateescape")
                    )
                    for candidate in {raw_path, stem} - checked_names:
                        checked_names.add(candidate)
                        label = "history:" + tree.decode("ascii")[:12]
                        if initial_private_save and private_identity is not None:
                            before = private_identity.findings
                            private_identity.check(candidate, repository_name=True)
                            if private_identity.findings > before:
                                private_identity.record_location(
                                    "filename", field="filename"
                                )
                        if port.secret_matches(candidate):
                            secret_hits.add(label)
                        if port.personal_path_matches(candidate) or (
                            private_identity is not None
                            and port.normalized_personal_path_matches(
                                candidate, private_identity.path_normalizer
                            )
                        ):
                            path_hits.add(label)
                if fields[1] == b"blob":
                    aliases.setdefault(fields[2].decode("ascii"), set()).add(
                        raw_path.decode("utf-8", errors="surrogateescape")
                    )
    for object_id, size in metadata_eligible:
        names = tuple(sorted(aliases.get(object_id, ())))
        if not port._metadata_media_names(names):
            private_identity.skipped += 1
            continue
        try:
            secret_found, path_found = port._inspect_history_media(
                repo, object_id, size, names, private_identity
            )
        except (OSError, ValueError, RuntimeError, port.SubprocessError):
            private_identity.report.update(
                status="tool_error", reason="media_metadata_unverified"
            )
            private_identity.checker = None
            break
        label = f"history:{object_id[:12]}"
        if secret_found:
            secret_hits.add(label)
        if path_found:
            path_hits.add(label)
    eligible.reverse()
    while eligible:
        chunk: list[tuple[str, str, int, str]] = []
        chunk_bytes = 0
        while eligible and (
            not chunk
            or chunk_bytes + eligible[-1][2] <= port._HISTORY_BATCH_BUDGET_BYTES
        ):
            item = eligible.pop()
            chunk.append(item)
            chunk_bytes += item[2]
        batch = port.run_subprocess(
            ["git", "cat-file", "--batch"],
            cwd=repo,
            input=("\n".join(object_id for object_id, _, _, _ in chunk) + "\n").encode(
                "ascii"
            ),
            capture_output=True,
        )
        if batch.returncode:
            raise RuntimeError("git_history_inventory_failed")
        cursor = 0
        try:
            for object_id, name, expected_size, object_type in chunk:
                line_end = batch.stdout.index(b"\n", cursor)
                header = batch.stdout[cursor:line_end].decode("ascii").split()
                if (
                    len(header) != 3
                    or header[0] != object_id
                    or header[1] != object_type
                ):
                    raise RuntimeError("git_history_inventory_failed")
                size = int(header[2])
                if size != expected_size:
                    raise RuntimeError("git_history_inventory_failed")
                start = line_end + 1
                end = start + size
                if end >= len(batch.stdout) or batch.stdout[end : end + 1] != b"\n":
                    raise RuntimeError("git_history_inventory_failed")
                data = batch.stdout[start:end]
                cursor = end + 1
                # Gitのobject形式に対して、実際に読んだbytesと固定OIDの一致を確認。
                digest = hashlib.new(
                    "sha1" if len(object_id) == 40 else "sha256", usedforsecurity=False
                )
                digest.update(f"{object_type} {len(data)}\0".encode("ascii") + data)
                if digest.hexdigest() != object_id:
                    raise RuntimeError("git_raw_object_digest_mismatch")
                if check_repository_names and object_type == "blob":
                    names = aliases.get(object_id, {name})
                    text = submission_text(data, names, opaque_suffixes, port=port)
                    entries = entries_by_blob.get(object_id, [])
                    if text is not None and entries:
                        for (
                            parent,
                            commit,
                            path,
                            old_mode,
                            new_mode,
                            old_oid,
                            _,
                        ) in entries:
                            previous_text = None
                            if old_mode in {b"100644", b"100755"} and new_mode in {
                                b"100644",
                                b"100755",
                            }:
                                old_data = verified_parent_blob(
                                    repo, old_oid, port=port
                                )
                                if old_data is not None:
                                    previous_text = submission_text(
                                        old_data, (path,), opaque_suffixes, port=port
                                    )
                            if private_identity is not None:
                                before_text_findings = getattr(
                                    private_identity, "findings", 0
                                )
                                private_identity.check(
                                    text.encode("utf-8"),
                                    source_names=(path,),
                                    previous_text=previous_text,
                                    reviewed_raw_paths=(
                                        reviewed_exceptions is not None
                                        and reviewed_exceptions.all_paths_reviewed(
                                            path,
                                            data,
                                            record=False,
                                            normalized=True,
                                            normalizer=private_identity.path_normalizer,
                                        )
                                    ),
                                )
                                if getattr(
                                    private_identity, "findings", 0
                                ) > before_text_findings and hasattr(
                                    private_identity, "record_location"
                                ):
                                    private_identity.record_location(
                                        "history_object", field=f"blob:{object_id[:12]}"
                                    )
                                if private_identity.last_text_path_match:
                                    path_hits.add(f"history:{object_id[:12]}")
                            if previous_text is None:
                                secret_found = (
                                    reviewed_exceptions.has_unreviewed(path, data)
                                    if reviewed_exceptions is not None
                                    else port.text_has(port.SECRET_PATTERNS, data)
                                )
                                path_found = (
                                    reviewed_exceptions.has_unreviewed_paths(path, data)
                                    if reviewed_exceptions is not None
                                    else port.text_has(port.PATH_PATTERNS, data)
                                )
                            else:
                                secret_found = diff_added_lines_have(
                                    repo,
                                    port.SECRET_PATTERNS,
                                    parent,
                                    commit,
                                    reviewed_exceptions=reviewed_exceptions,
                                    scan_path=path,
                                    port=port,
                                )
                                path_found = (
                                    False
                                    if private_identity is not None
                                    and private_identity.report.get("provider_kind")
                                    == "bundled_canonical"
                                    else diff_added_lines_have(
                                        repo,
                                        port.PATH_PATTERNS,
                                        parent,
                                        commit,
                                        reviewed_exceptions=reviewed_exceptions,
                                        scan_path=path,
                                        port=port,
                                    )
                                )
                            if (
                                private_identity is not None
                                and private_identity.report.get("provider_kind")
                                != "bundled_canonical"
                            ):
                                path_found |= bool(
                                    port.normalized_personal_path_matches(
                                        data, private_identity.path_normalizer
                                    )
                                )
                            if secret_found:
                                secret_hits.add(f"history:{object_id[:12]}")
                            if path_found:
                                path_hits.add(f"history:{object_id[:12]}")
                        continue
                if (
                    (object_type == "commit" and check_raw_commit_payloads)
                    or (
                        initial_private_save
                        and object_type in {"commit", "tag", "tree"}
                    )
                    or (
                        check_full_history_metadata and object_type in {"commit", "tag"}
                    )
                ):
                    # Inspect immutable payload even when another identity gate fails.
                    if port.secret_matches(data):
                        secret_hits.add(f"history:{object_id[:12]}")
                    normalizer = (
                        private_identity.path_normalizer if private_identity else None
                    )
                    if port.personal_path_matches(
                        data
                    ) or port.normalized_personal_path_matches(data, normalizer):
                        path_hits.add(f"history:{object_id[:12]}")
                if private_identity is not None:
                    if check_repository_names and object_type == "tree":
                        # Tree structure is checked without reclassifying unchanged sibling names.
                        tree_cursor = 0
                        while tree_cursor < len(data):
                            space = data.index(b" ", tree_cursor)
                            nul = data.index(b"\0", space)
                            raw_name = data[space + 1 : nul]
                            raw_name.decode("utf-8", errors="surrogateescape")
                            tree_cursor = nul + 1 + len(object_id) // 2
                        if tree_cursor != len(data):
                            raise RuntimeError("git_history_inventory_failed")
                        continue
                    names = aliases.get(object_id)
                    reviewed_raw = bool(
                        object_type == "blob"
                        and reviewed_exceptions is not None
                        and names
                        and all(
                            reviewed_exceptions.all_paths_reviewed(
                                alias,
                                data,
                                record=False,
                                normalized=True,
                                normalizer=private_identity.path_normalizer,
                            )
                            for alias in names
                        )
                    )
                    identity_data = data
                    if object_type == "commit" and approved_private_commit_identity:
                        identity_data = private_commit_identity_view(
                            data, approved_private_commit_identity, port=port
                        )
                        private_identity.report[
                            "approved_private_commit_identity_fields"
                        ] += 2
                    before_object_findings = getattr(private_identity, "findings", 0)
                    private_identity.check(
                        identity_data,
                        reviewed_raw_paths=reviewed_raw,
                        source_names=(
                            tuple(names or ()) if object_type == "blob" else ()
                        ),
                    )
                    if getattr(
                        private_identity, "findings", 0
                    ) > before_object_findings and hasattr(
                        private_identity, "record_location"
                    ):
                        private_identity.record_location(
                            "history_object", field=f"{object_type}:{object_id[:12]}"
                        )
                if object_type != "blob":
                    continue
                label = (
                    f"history:{object_id[:12]}"
                    if check_repository_names
                    else f"history:{port.sanitized_evidence_label(name or object_id[:12])}"
                )
                if private_identity is not None and getattr(
                    private_identity, "last_media_secret_matches", 0
                ):
                    secret_hits.add(label)
                if reviewed_exceptions is None:
                    if port.text_has(port.SECRET_PATTERNS, data):
                        secret_hits.add(label)
                else:
                    names = aliases.get(object_id)
                    if not names:
                        # detached blob/tag等でpath根拠なしなら例外を使わない。
                        if port.text_has(port.SECRET_PATTERNS, data):
                            secret_hits.add(label)
                    for alias in sorted(names or ()):
                        if reviewed_exceptions.has_unreviewed(alias, data):
                            secret_hits.add(
                                label
                                if check_repository_names
                                else f"history:{object_id[:12]}:{port.sanitized_evidence_label(alias)}"
                            )
                if reviewed_exceptions is None:
                    if port.text_has(port.PATH_PATTERNS, data):
                        path_hits.add(label)
                else:
                    names = aliases.get(object_id)
                    if not names and port.personal_path_matches(data):
                        path_hits.add(label)
                    for alias in sorted(names or ()):
                        if reviewed_exceptions.has_unreviewed_paths(alias, data):
                            path_hits.add(
                                label
                                if check_repository_names
                                else f"history:{object_id[:12]}:{port.sanitized_evidence_label(alias)}"
                            )
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise RuntimeError("git_history_inventory_failed") from exc
        if cursor != len(batch.stdout):
            raise RuntimeError("git_history_inventory_failed")
    return sorted(secret_hits), sorted(path_hits)


def diff_added_lines_have(
    repo: Path,
    patterns: tuple[re.Pattern, ...],
    *diff_args: str,
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    scan_path: str | None = None,
    port,
) -> bool:
    """Return whether added text lines in one Git diff match ``patterns``.

    A target-diff scan must not reclassify unchanged baseline text merely because
    its file also contains a new, unrelated line.  Patch metadata is excluded;
    only actual ``+`` lines are decoded by ``text_has``.
    """
    pathspec = ["--", ":(literal)" + scan_path] if scan_path is not None else ["--"]
    reviewed_secrets = (
        reviewed_exceptions is not None
        and patterns == port.SECRET_PATTERNS
        and bool(reviewed_exceptions.entries)
    )
    reviewed_paths = (
        reviewed_exceptions is not None
        and patterns == port.PATH_PATTERNS
        and reviewed_exceptions.has_path_entries
    )
    if reviewed_secrets or reviewed_paths:
        inventory = port.run_subprocess(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                *diff_args,
                *pathspec,
            ],
            cwd=repo,
            capture_output=True,
        )
        if inventory.returncode:
            raise RuntimeError("git_target_diff_inventory_failed")
        for raw_path in inventory.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if len(diff_args) == 2:
                blob = port.run_subprocess(
                    ["git", "show", f"{diff_args[1]}:{path}"],
                    cwd=repo,
                    capture_output=True,
                )
                data = blob.stdout if blob.returncode == 0 else None
            else:
                candidate = repo / path
                if candidate.is_symlink():
                    data = None
                else:
                    try:
                        data = candidate.read_bytes()
                    except FileNotFoundError:
                        data = None
                    except OSError as exc:
                        raise RuntimeError("git_target_diff_inventory_failed") from exc
            if data is not None:
                # An approval binds one finding family as well as path/content.
                # A reviewed path can never waive an unrelated credential.
                approved = (
                    not reviewed_exceptions.has_unreviewed(path, data)
                    if reviewed_secrets
                    else reviewed_exceptions.all_paths_reviewed(path, data)
                )
                if approved:
                    continue
            if diff_added_lines_have(
                repo, patterns, *diff_args, scan_path=path, port=port
            ):
                return True
        return False
    result = port.run_subprocess(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--unified=0",
            *diff_args,
            *pathspec,
        ],
        cwd=repo,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    added_lines: list[bytes] = []
    in_hunk = False
    for line in result.stdout.splitlines():
        if line.startswith(b"diff --git "):
            in_hunk = False
        elif line.startswith(b"@@"):
            in_hunk = True
        elif line.startswith((b"--- ", b"+++ ")) and not in_hunk:
            continue
        elif in_hunk and line.startswith(b"+"):
            added_lines.append(line[1:])
    added = b"\n".join(added_lines)
    if added and port.text_has(patterns, added):
        return True

    # Git omits patch lines for UTF-16 and attribute-classified binary text.
    # Inspect only those binary entries and compare decoded blob lines, so an
    # unchanged baseline path in a modified file is not reclassified as new.
    numstat = port.run_subprocess(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--numstat",
            "-z",
            *diff_args,
            *pathspec,
        ],
        cwd=repo,
        capture_output=True,
    )
    if numstat.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    revisions = list(diff_args)
    old_revision = revisions[0] if len(revisions) == 2 else "HEAD"
    new_revision = revisions[1] if len(revisions) == 2 else None
    for entry in numstat.stdout.split(b"\0"):
        if not entry:
            continue
        additions, separator, remainder = entry.partition(b"\t")
        deletions, separator2, raw_path = remainder.partition(b"\t")
        if not separator or not separator2 or additions != b"-" or deletions != b"-":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        old_blob = port.run_subprocess(
            ["git", "show", f"{old_revision}:{path}"],
            cwd=repo,
            capture_output=True,
        )
        old_data = old_blob.stdout if old_blob.returncode == 0 else b""
        if new_revision is None:
            candidate = repo / path
            if candidate.is_symlink():
                continue
            try:
                new_data = candidate.read_bytes()
            except OSError as exc:
                raise RuntimeError("git_target_diff_inventory_failed") from exc
        else:
            new_blob = port.run_subprocess(
                ["git", "show", f"{new_revision}:{path}"],
                cwd=repo,
                capture_output=True,
            )
            if new_blob.returncode:
                continue
            new_data = new_blob.stdout
        old_text = _decode_scannable_text(old_data)
        new_text = _decode_scannable_text(new_data)
        if new_text is None:
            # Genuinely binary content (non-UTF-8/UTF-16 noise) that Git also
            # omits from the unified diff. Fall back to a raw-byte pattern
            # search so an embedded ASCII personal path is not skipped just
            # because the surrounding bytes cannot be decoded as text. Only
            # report a hit that is new relative to the comparison base, so an
            # unchanged baseline blob is not reclassified.
            if port._bytes_pattern_hit(
                patterns, new_data
            ) and not port._bytes_pattern_hit(patterns, old_data):
                return True
            continue
        old_lines = old_text.splitlines() if old_text is not None else []
        new_lines = new_text.splitlines()
        introduced = "\n".join(
            line
            for operation, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
                a=old_lines, b=new_lines, autojunk=False
            ).get_opcodes()
            if operation in {"insert", "replace"}
            for line in new_lines[j1:j2]
        ).encode("utf-8")
        if introduced and port.text_has(patterns, introduced):
            return True
    return False


def _decode_scannable_text(data: bytes) -> str | None:
    for encoding in ("utf-8", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def comparison_parent(repo: Path, base_ref: str, commit: str, *, port) -> str:
    """Prefer a base-containing parent, then a unique base ancestor."""
    parents = port.run_subprocess(
        ["git", "show", "-s", "--format=%P", commit],
        cwd=repo,
        text=True,
        encoding="ascii",
        errors="strict",
        capture_output=True,
    )
    if parents.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    candidates = parents.stdout.split()
    if not candidates:
        empty_tree = port.run_subprocess(
            ["git", "hash-object", "-t", "tree", "--stdin"],
            cwd=repo,
            input=b"",
            capture_output=True,
        )
        if empty_tree.returncode:
            raise RuntimeError("git_target_diff_inventory_failed")
        return empty_tree.stdout.decode("ascii").strip()
    for parent in candidates:
        contains_base = port.run_subprocess(
            ["git", "merge-base", "--is-ancestor", base_ref, parent],
            cwd=repo,
            capture_output=True,
        )
        if contains_base.returncode == 0:
            return parent
        if contains_base.returncode != 1:
            raise RuntimeError("git_target_diff_inventory_failed")
    if len(candidates) > 1:
        # mainが先へ進んでも、過去のmergeで取り込んだmain由来の追加を
        # branchの新規追加として再検出しない。曖昧ならfirst parentを保つ。
        base_ancestors = []
        for parent in candidates:
            ancestor_of_base = port.run_subprocess(
                ["git", "merge-base", "--is-ancestor", parent, base_ref],
                cwd=repo,
                capture_output=True,
            )
            if ancestor_of_base.returncode == 0:
                base_ancestors.append(parent)
            elif ancestor_of_base.returncode != 1:
                raise RuntimeError("git_target_diff_inventory_failed")
        if len(base_ancestors) == 1:
            return base_ancestors[0]
    return candidates[0]


def target_diff_personal_path_hits(
    repo: Path,
    base_ref: str,
    reviewed_exceptions: ReviewedSecretExceptions | None = None,
    *,
    port,
) -> list[str]:
    """Scan personal paths introduced anywhere after ``base_ref``.

    Each commit is compared with its first parent so a path that was introduced
    and later removed remains detectable.  The current index/worktree diff is
    checked separately.  Evidence labels intentionally avoid file contents and
    checkout paths.
    """
    commits = port.run_subprocess(
        ["git", "rev-list", "--reverse", f"{base_ref}..HEAD"],
        cwd=repo,
        text=True,
        encoding="ascii",
        errors="strict",
        capture_output=True,
    )
    if commits.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    hits: set[str] = set()
    for commit in commits.stdout.splitlines():
        parent = comparison_parent(repo, base_ref, commit, port=port)
        if diff_added_lines_have(
            repo,
            port.PATH_PATTERNS,
            parent,
            commit,
            reviewed_exceptions=reviewed_exceptions,
            port=port,
        ):
            hits.add(f"target-diff:commit:{commit[:12]}")
    if diff_added_lines_have(
        repo,
        port.PATH_PATTERNS,
        "HEAD",
        reviewed_exceptions=reviewed_exceptions,
        port=port,
    ):
        hits.add("target-diff:working-tree")
    untracked = port.run_subprocess(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
    )
    if untracked.returncode:
        raise RuntimeError("git_target_diff_inventory_failed")
    for raw_name in untracked.stdout.split(b"\0"):
        if not raw_name:
            continue
        path = repo / raw_name.decode("utf-8", errors="surrogateescape")
        port._guard_worktree_parent_components(repo, path)
        if path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise RuntimeError("git_target_diff_inventory_failed") from exc
        # 全体decode失敗時も raw bytes で拾う (診断は diff_added_lines_have と同じ)
        if (
            reviewed_exceptions.has_unreviewed_paths(
                path.relative_to(repo).as_posix(), data
            )
            if reviewed_exceptions is not None
            else port.text_has(port.PATH_PATTERNS, data)
            or port._bytes_pattern_hit(port.PATH_PATTERNS, data)
        ):
            hits.add("target-diff:untracked-worktree")
            break
    return sorted(hits)
