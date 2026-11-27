from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote


def _is_coauthor_line(line: str) -> bool:
    """`作者|記録者` の行ではない行は Co-authored-by の署名行（値だけ）。"""
    return "|" not in line


def _coauthor_line_blocked(
    line: str,
    policy=None,
    expected_identity: str | None = None,
    *,
    load_gate,
) -> bool:
    """共著者行は受け入れる。止めるのは形不正・伏せ字・個人メール・名簿にないメール。

    通すメールは、名簿（policy）か期待名義にある、道具の noreply・明示の一覧、予約ドメイン。
    """
    gate = load_gate()
    parsed = gate.parse_trailer(line)
    if not parsed or gate.masked_identity_name(parsed[0]):
        return True
    listed = set(policy.emails) if policy is not None else set()
    if expected_identity:
        listed.add(expected_identity.rsplit("<", 1)[-1].rstrip(">").casefold())
    return gate.coauthor_email_problem(parsed[1], listed) is not None


def _identity_line_mismatch(
    line: str, expected_identity: str | None, policy=None, *, load_gate
) -> bool:
    """期待名義との不一致。共著者行は期待名義と比べず、止める条件だけ見る。"""
    if _is_coauthor_line(line):
        return _coauthor_line_blocked(
            line, policy, expected_identity, load_gate=load_gate
        )
    return bool(expected_identity) and (
        line.split("|")[0] != expected_identity
        or line.split("|")[-1] != expected_identity
    )


def _identity_line_allowed(policy, line: str, *, load_gate) -> bool:
    """名義の1行を役割ごとに完全一致で見る（identity_gate.check_public_range と同じ規則）。

    `作者|記録者` の行は、作者を「作者」、記録者を「コミッター」として判定する。
    `|` の無い行は Co-authored-by の署名行。名簿には縛らず、止める条件（形不正・伏せ字・個人メール・名簿にないメール）だけ見る。
    committer_only の名義（GitHub の Web 操作の記録者など）は記録者の位置でだけ通る。
    """
    if _is_coauthor_line(line):
        return not _coauthor_line_blocked(line, policy, load_gate=load_gate)
    parts = line.split("|")
    return policy.allows(parts[0], "作者") and policy.allows(parts[-1], "コミッター")


def effective_identity(value: str) -> str:
    return re.sub(r"\s+\d+\s+[+-]\d{4}$", "", value).strip()


def normalized_personal_path_matches(
    data: bytes, normalizer=None, *, load_normalizer, personal_path_matches
) -> Counter:
    """公開正本の静的viewsを既存path規則へ渡し、追加検出だけを件数へ加える。"""
    if normalizer is None:
        normalizer = load_normalizer()
    raw = personal_path_matches(data)
    matches = raw.copy()
    for encoding in ("utf-8", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        extra = Counter()
        path_views = getattr(
            normalizer, "private_path_views", normalizer.static_string_views
        )
        for value, original in path_views(text):
            # Valid source can describe an unpaired surrogate in a test literal.
            # Keep that derived view UTF-8 scannable, without dropping text or
            # losing the boundary before an adjacent/URL-encoded absolute path.
            # Raw bytes and the strict source decoders above remain unchanged.
            extra += personal_path_matches(
                value.encode("utf-8", errors="xmlcharrefreplace")
            ) - personal_path_matches(
                original.encode("utf-8", errors="xmlcharrefreplace")
            )
        matches |= raw + extra
    return matches


class PrivateIdentityScan:
    """登録済み個人情報の本文検査。入力の読み込みは窓口の派生クラスが行う。

    窓口の判定（媒体の種類・秘密情報の規則・報告名の伏せ方）は、窓口の派生クラスが
    class 属性 port に置く口から、呼ぶたびに読む。
    """

    def __init__(self, required: bool):
        self.findings = 0
        self.finding_locations = []
        self.finding_locations_truncated = 0
        self.objects = 0
        self.skipped = 0
        self._verified_caff_blobs = set()
        self.report = {"status": "not_configured", "required": required}
        self.checker = None
        self.path_normalizer = None

    def check(
        self,
        data: bytes,
        *,
        literal_only: bool = False,
        repository_name: bool = False,
        reviewed_raw_paths: bool = False,
        defer_unverified_jpeg: bool = False,
        source_names=(),
        previous_text: str | None = None,
    ):
        # 例外の助けなしに合格した本文だけ、使った正規化器のhashを残す (控えの材料)。
        self.last_clean_pass_digests = None
        self.last_media_secret_matches = 0
        self.last_text_path_match = False
        if self.checker is None:
            return
        seen_normalizer_digests: set[str] = set()
        texts = {data.decode("utf-8", errors="replace")}
        try:
            texts.add(data.decode("utf-16"))
        except UnicodeDecodeError:
            pass
        found = False
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                kind = (
                    None
                    if repository_name
                    else self.port.media_kind(data, source_names)
                )
                if kind is not None:
                    if not callable(getattr(self, "media_checker", None)):
                        raise ValueError(
                            "private_identity_media_external_provider_unsupported"
                        )

                    def image_check(value, patterns):
                        checker = (
                            self.png_checker
                            if value.startswith(b"\x89PNG")
                            else self.jpeg_checker
                        )
                        if not callable(checker):
                            raise ValueError(
                                "private_identity_media_callback_unavailable"
                            )
                        secret_hits = Counter()

                        def observe_metadata(text):
                            nonlocal secret_hits
                            secret_hits |= self.port.secret_matches(
                                text.encode("utf-8")
                            )

                        result = dict(
                            checker(value, patterns, metadata_observer=observe_metadata)
                        )
                        if result.pop("metadata_observer_complete", None) is not True:
                            raise ValueError(
                                "private_identity_media_callback_unavailable"
                            )
                        result["secret_match_count"] = sum(secret_hits.values())
                        return result

                    def media_text_check(value, patterns):
                        result = dict(self.checker(value, patterns))
                        result["secret_match_count"] = int(
                            self.port.secret_text_has(value.encode("utf-8"))
                        )
                        return result

                    results = [
                        self.media_checker(
                            data,
                            self.patterns,
                            kind=kind,
                            check_text=media_text_check,
                            check_image=image_check,
                        )
                    ]
                    evidence = results[0]
                    secret_count = evidence.get("secret_match_count", 0)
                    if type(secret_count) is not int or secret_count < 0:
                        raise ValueError("private_identity_media_unverified")
                    self.last_media_secret_matches = secret_count
                    if (
                        evidence.get("adapter_sha256")
                        != self.report["media_adapter_sha256"]
                    ):
                        raise ValueError("private_identity_media_adapter_changed")
                    self.report.setdefault("media_decoder_versions", {}).update(
                        evidence["decoder_versions"]
                    )
                    self.report["media_objects"] = (
                        self.report.get("media_objects", 0) + 1
                    )
                    type1_metrics = evidence.get("type1_metrics", [])
                    if not isinstance(type1_metrics, list):
                        raise ValueError("private_identity_media_unverified")
                    self.report["type1_fonts_scanned"] = self.report.get(
                        "type1_fonts_scanned", 0
                    ) + len(type1_metrics)
                    checksum_count = evidence.get("font_checksum_mismatches", 0)
                    if type(checksum_count) is not int or checksum_count < 0:
                        raise ValueError("private_identity_media_unverified")
                    self.report["font_checksum_mismatches"] = (
                        self.report.get("font_checksum_mismatches", 0) + checksum_count
                    )
                    free_count = evidence.get("pdf_known_free_references", 0)
                    if type(free_count) is not int or free_count < 0:
                        raise ValueError("private_identity_media_unverified")
                    self.report["pdf_known_free_references"] = (
                        self.report.get("pdf_known_free_references", 0) + free_count
                    )
                elif (
                    not repository_name
                    and data.startswith(b"CAFF")
                    and callable(getattr(self, "caff_checker", None))
                ):
                    results = [
                        self.caff_checker(data, self.patterns, self.caff_image_cache)
                    ]
                elif data.startswith(b"8BPS") and callable(
                    getattr(self, "psd_checker", None)
                ):
                    results = [self.psd_checker(data, self.patterns)]
                elif data.startswith((b"GIF87a", b"GIF89a")) and callable(
                    getattr(self, "gif_checker", None)
                ):
                    results = [self.gif_checker(data, self.patterns)]
                elif data.startswith(b"\x89PNG") and callable(
                    getattr(self, "png_checker", None)
                ):
                    results = [self.png_checker(data, self.patterns)]
                elif data.startswith(b"\xff\xd8") and callable(
                    getattr(self, "jpeg_checker", None)
                ):
                    results = [self.jpeg_checker(data, self.patterns)]
                elif (
                    previous_text is not None
                    and self.report["provider_kind"] == "bundled_canonical"
                ):
                    delta_checker = getattr(
                        self.path_normalizer, "scan_private_text_delta", None
                    )
                    if not callable(delta_checker):
                        raise ValueError("private_identity_normalization_incomplete")
                    current_text = data.decode("utf-8")
                    results = [
                        delta_checker(old, new, self.patterns)
                        for old, new in {
                            (previous_text, current_text),
                            (unquote(previous_text), unquote(current_text)),
                        }
                    ]
                else:
                    results = [
                        (
                            self.repository_name_checker
                            if repository_name
                            and callable(getattr(self, "repository_name_checker", None))
                            else self.checker
                        )(candidate, self.patterns)
                        for text in texts
                        for candidate in {text, unquote(text)}
                    ]
                for result in results:
                    if (
                        not isinstance(result, dict)
                        or result.get("status") not in {"pass", "fail"}
                        or type(result.get("literal_match_count")) is not int
                        or result["literal_match_count"] < 0
                        or type(result.get("local_path_match")) is not bool
                    ):
                        raise ValueError("private_identity_provider_result_invalid")
                    if (result["status"] == "pass") != (
                        result["literal_match_count"] == 0
                        and not result["local_path_match"]
                    ):
                        raise ValueError(
                            "private_identity_provider_result_inconsistent"
                        )
                    reconstructed = result.get(
                        "reconstructed_path_match", result["local_path_match"]
                    )
                    if type(reconstructed) is not bool or (
                        reconstructed and not result["local_path_match"]
                    ):
                        raise ValueError(
                            "private_identity_provider_result_inconsistent"
                        )
                    metadata_match = result.get("metadata_path_match", False)
                    if type(metadata_match) is not bool or (
                        metadata_match and not result["local_path_match"]
                    ):
                        raise ValueError(
                            "private_identity_provider_result_inconsistent"
                        )
                    normalizer_digest = result.get("normalizer_sha256")
                    if normalizer_digest is not None:
                        if not isinstance(normalizer_digest, str) or not re.fullmatch(
                            r"[0-9a-f]{64}", normalizer_digest
                        ):
                            raise ValueError("private_identity_normalizer_unverified")
                        if (
                            self.report.get("normalizer_sha256", normalizer_digest)
                            != normalizer_digest
                        ):
                            raise ValueError("private_identity_normalizer_changed")
                        self.report["normalizer_sha256"] = normalizer_digest
                        seen_normalizer_digests.add(normalizer_digest)
                    if result.get("delta_full_fallback") is True:
                        self.report["text_delta_full_fallbacks"] = (
                            self.report.get("text_delta_full_fallbacks", 0) + 1
                        )
                    # literal_onlyは旧caller互換だけ。未承認pathの抑止には使わない。
                    same_normalizer = (
                        self.report["provider_kind"] == "bundled_canonical"
                        or normalizer_digest == self.path_normalizer_sha256
                    )
                    effective_path_match = result["local_path_match"] and (
                        not reviewed_raw_paths or not same_normalizer or metadata_match
                    )
                    self.last_text_path_match |= effective_path_match
                    found |= bool(result["literal_match_count"] or effective_path_match)
        except (Exception, SystemExit) as exc:
            code = str(exc)
            # 送信に含まれない既存JPEGの構造不正だけは、そのfileを未展開として続ける。
            # 容量上限は本文照合より前に出るため、mainと同じtool_errorのままにする。
            if defer_unverified_jpeg and code == "private_identity_jpeg_unverified":
                self.report["not_expanded_count"] = (
                    int(self.report.get("not_expanded_count", 0)) + 1
                )
                return
            safe_codes = {
                "private_identity_jpeg_unverified",
                "private_identity_jpeg_image_limit",
                "private_identity_jpeg_text_limit",
                "private_identity_jpeg_decoder_unavailable",
                "private_identity_caff_unverified",
                "private_identity_caff_size_limit",
                "private_identity_gif_unverified",
                "private_identity_gif_size_limit",
                "private_identity_gif_decoder_unavailable",
                "private_identity_psd_unverified",
                "private_identity_psd_decoder_unavailable",
                "private_identity_psd_size_limit",
                "private_identity_psd_text_limit",
                "private_identity_png_unverified",
                "private_identity_png_text_limit",
                "private_identity_png_image_limit",
                "private_identity_png_animation_unverified",
                "private_identity_normalization_incomplete",
            }
            safe_codes.update(
                "private_identity_media_" + reason
                for reason in (
                    "unsupported_structural_coverage",
                    "format_mismatch",
                    "external_provider_unsupported",
                    "dependency_unavailable",
                    "unverified",
                    "unsupported_metadata",
                    "unsupported_region",
                    "unsupported_stream",
                    "unsupported_image",
                    "unsupported_font",
                    "unsupported_font_table",
                    "unsupported_active_or_attachment",
                    "unsupported_attachment",
                    "encrypted",
                    "external_reference",
                    "data_limit",
                    "text_limit",
                    "image_limit",
                    "decoded_limit",
                    "object_limit",
                    "page_limit",
                    "stream_limit",
                    "duration_limit",
                    "timeout",
                    "output_limit",
                    "callback_unavailable",
                    "adapter_changed",
                )
            )
            reason = (
                str(exc)
                if str(exc) in safe_codes
                else "private_identity_execution_unverified"
            )
            self.report.update(status="tool_error", reason=reason)
            self.checker = None
            return
        if (
            not repository_name
            and data.startswith(b"CAFF")
            and callable(getattr(self, "caff_checker", None))
        ):
            self._remember_verified_caff(data)
        self.objects += 1
        self.findings += int(found)
        if (
            not found
            and not reviewed_raw_paths
            and not self.port.media_candidate(data, source_names)
        ):
            self.last_clean_pass_digests = tuple(sorted(seen_normalizer_digests))

    def _remember_verified_caff(self, data: bytes):
        """形式検査済みの同一byteをGit blob IDへ束縛する。"""
        if (
            not callable(getattr(self, "caff_checker", None))
            or data[:14] != b"CAFF\0\0\0----\0\0\0"
        ):
            return
        header = f"blob {len(data)}\0".encode("ascii")
        for algorithm in ("sha1", "sha256"):
            digest = hashlib.new(algorithm, usedforsecurity=False)
            digest.update(header)
            digest.update(data)
            self._verified_caff_blobs.add(digest.hexdigest())

    def reuse_clean_pass(self, normalizer_digests: tuple[str, ...]) -> bool:
        """控えた合格を、本文検査と同じ数え方・正規化器の一致確認で反映する。"""
        if self.checker is None or self.report.get("status") == "tool_error":
            return False
        for digest in normalizer_digests:
            if self.report.get("normalizer_sha256", digest) != digest:
                # 食い違いは控えを使わず本文検査へ回し、通常の失敗経路に任せる。
                return False
        for digest in normalizer_digests:
            self.report["normalizer_sha256"] = digest
        self.objects += 1
        return True

    def label_status(self, label: str) -> str:
        """同じ検査器で報告名だけを確認し、file/objectの検出件数へ混ぜない。"""
        if self.checker is None or self.report.get("status") == "tool_error":
            return "unknown"
        probe = copy.copy(self)
        probe.report = dict(self.report)
        probe.objects = probe.findings = probe.skipped = 0
        probe.check(label.encode("utf-8", errors="surrogatepass"))
        if probe.checker is None or probe.report.get("status") == "tool_error":
            return "unknown"
        return "private" if probe.findings else "public"

    def record_location(self, source, *, path=None, field=None):
        if path is not None:
            path = self.port.diagnostic_evidence_label(path, self)
        finding = {
            "kind": "登録済み個人情報に一致",
            "source": source,
            "path": path,
            "line": None,
            "field": field,
            "rule": "private_identity",
        }
        if path not in (None, "<redacted-path>") and finding in self.finding_locations:
            return
        if len(self.finding_locations) >= 50:
            self.finding_locations_truncated += 1
        else:
            self.finding_locations.append(finding)

    def result(self):
        report = {
            **self.report,
            "objects_scanned": self.objects,
            "finding_count": self.findings,
            "oversize_objects_unscanned": self.skipped,
            "scope": "worktree_and_reachable_git_objects",
        }
        if self.report["status"] == "tool_error":
            return report
        if self.findings:
            report["status"] = "fail"
            report["findings"] = self.finding_locations or [
                {
                    "kind": "登録済み個人情報に一致",
                    "source": "private_scan",
                    "path": None,
                    "line": None,
                    "field": None,
                    "rule": "private_identity",
                }
            ]
            report["findings_truncated"] = self.finding_locations_truncated
        elif self.skipped and self.checker is not None:
            report["status"] = "unknown"
        if self.report.get("not_expanded_count"):
            report["not_expanded_count"] = self.report["not_expanded_count"]
        return report


IDENTITY_SCAN_CACHE_VERSION = "identity-scan-pass-cache-v1"
_IDENTITY_SCAN_CACHE_DISABLED = {"0", "off", "false", "no"}
_IDENTITY_SCAN_CACHE_MAX_ENTRIES = 500_000
_IDENTITY_SCAN_CACHE_MAX_BYTES = 128 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}")


class IdentityScanCache:
    """本文の個人識別子検査で合格した内容hashだけを控え、同じ鍵の再検査を省く。

    鍵は検査器・方針・正規化器・reviewed例外・remote・実行環境のhash。1つでも違えば
    別の控えになり、控えが壊れている・所有者や権限が違う・repo内にあるときは使わない。
    控えるのは例外の助けなしの合格だけで、本文・path・検出値は書かない。
    """

    def __init__(self, path: Path, key: str, entries: dict[str, tuple[str, ...]]):
        self.path = path
        self.key = key
        self.entries = entries
        self.dirty = False

    @classmethod
    def open(
        cls,
        repo: Path,
        scan: "PrivateIdentityScan",
        reviewed_secret_exceptions: str | Path | None,
        remote: str,
        *,
        setting,
        cache_home,
        scanner_sha256,
        distribution_version,
    ) -> "IdentityScanCache | None":
        if setting().strip().lower() in _IDENTITY_SCAN_CACHE_DISABLED:
            return None
        # 同梱の正規化器だけを対象にする。外部providerはimport先の変化をhashで縛れない。
        if (
            os.name != "posix"
            or scan.checker is None
            or scan.report.get("status") != "pass"
            or scan.report.get("provider_kind") != "bundled_canonical"
        ):
            return None
        try:
            reviewed_digest = None
            if reviewed_secret_exceptions is not None:
                reviewed_digest = hashlib.sha256(
                    Path(reviewed_secret_exceptions).read_bytes()
                ).hexdigest()
            material = {
                "version": IDENTITY_SCAN_CACHE_VERSION,
                "scanner_sha256": scanner_sha256(),
                "provider_sha256": scan.report["provider_sha256"],
                "policy_sha256": scan.report["policy_sha256"],
                "provider_kind": scan.report["provider_kind"],
                "normalized_path_provider_sha256": scan.path_normalizer_sha256,
                "caff_checker": callable(getattr(scan, "caff_checker", None)),
                "caff_decoder_sha256": scan.report.get("caff_decoder_sha256"),
                "gif_checker": callable(getattr(scan, "gif_checker", None)),
                "gif_decoder_version": scan.report.get("gif_decoder_version"),
                "gif_decoder_settings": scan.report.get("gif_decoder_settings"),
                "psd_checker": callable(getattr(scan, "psd_checker", None)),
                "psd_decoder_version": scan.report.get("psd_decoder_version"),
                "png_checker": callable(getattr(scan, "png_checker", None)),
                "jpeg_checker": callable(getattr(scan, "jpeg_checker", None)),
                "reviewed_exceptions_sha256": reviewed_digest,
                "remote_sha256": hashlib.sha256(remote.encode("utf-8")).hexdigest(),
                "python": sys.version,
                "simplejpeg": distribution_version("simplejpeg"),
            }
            key = hashlib.sha256(
                json.dumps(material, sort_keys=True).encode("utf-8")
            ).hexdigest()
            base = cache_home()
            base_path = Path(base)
            if not base_path.is_absolute():
                return None
            directory = base_path / "repo-preflight" / "identity-scan"
            repo_root = repo.resolve()
            if directory.resolve().is_relative_to(repo_root) or (
                base_path.resolve().is_relative_to(repo_root)
            ):
                return None
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                return None
            if stat.S_IMODE(info.st_mode) & 0o077:
                # 権限が緩いフォルダは、自動で直さず控えを一切使わない。その場で直して
                # 中身を消す処理は、入れ子や競合で古い控えを残す抜け道になる (fail-closed)。
                return None
            path = directory / f"{key}.json"
            return cls(path, key, cls._load(path, key))
        except Exception:
            return None

    @staticmethod
    def _load(path: Path, key: str) -> dict[str, tuple[str, ...]]:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return {}
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > _IDENTITY_SCAN_CACHE_MAX_BYTES
            ):
                return {}
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                payload = json.loads(handle.read().decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"version", "key", "entries"}
                or payload["version"] != IDENTITY_SCAN_CACHE_VERSION
                or payload["key"] != key
                or not isinstance(payload["entries"], dict)
                or len(payload["entries"]) > _IDENTITY_SCAN_CACHE_MAX_ENTRIES
            ):
                return {}
            entries: dict[str, tuple[str, ...]] = {}
            for name, digests in payload["entries"].items():
                if (
                    not _HEX64.fullmatch(name)
                    or not isinstance(digests, list)
                    or len(digests) > 1
                    or not all(
                        isinstance(value, str) and _HEX64.fullmatch(value)
                        for value in digests
                    )
                ):
                    # 1件でも形が違えば控え全体を信用しない。
                    return {}
                entries[name] = tuple(digests)
            return entries
        except Exception:
            return {}
        finally:
            if fd >= 0:
                os.close(fd)

    def lookup(self, content_sha256: str) -> tuple[str, ...] | None:
        return self.entries.get(content_sha256)

    def record(self, content_sha256: str, normalizer_digests: tuple[str, ...]) -> None:
        if len(normalizer_digests) > 1:
            return
        if self.entries.get(content_sha256) != normalizer_digests:
            self.entries[content_sha256] = normalizer_digests
            self.dirty = True

    def save(self) -> None:
        if not self.dirty or len(self.entries) > _IDENTITY_SCAN_CACHE_MAX_ENTRIES:
            return
        temp = None
        try:
            import tempfile

            fd, temp = tempfile.mkstemp(
                prefix=".identity-scan-", suffix=".tmp", dir=self.path.parent
            )
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(
                    json.dumps(
                        {
                            "version": IDENTITY_SCAN_CACHE_VERSION,
                            "key": self.key,
                            "entries": {
                                name: list(digests)
                                for name, digests in sorted(self.entries.items())
                            },
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            os.replace(temp, self.path)
            temp = None
            self.dirty = False
        except Exception:
            pass
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass


def _guard_worktree_parent_components(repo: Path, path: Path) -> None:
    """正規化済みrepo rootより下の全親componentを、参照先を辿らず確認する。"""
    try:
        relative = path.relative_to(repo)
        if ".." in relative.parts:
            raise ValueError("outside_inventory")
        parent = repo
        for component in relative.parts[:-1]:
            parent = parent / component
            try:
                info = parent.lstat()
            except FileNotFoundError:
                # 消えたtracked fileは既存のdeleted inventoryが扱う。
                return
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise ValueError("linked_parent")
    except (OSError, ValueError) as exc:
        raise RuntimeError("git_worktree_inventory_failed") from exc
