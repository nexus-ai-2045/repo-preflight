"""Strict MP4 boundaries via optional Hachoir 3.4.0 (GNU GPL v2), not vendored.

Packet ranges are half-open byte intervals from a caller's successful ffprobe and
full audio/video decode of these exact source bytes. This helper cannot attest a
caller's decoder result. Every byte outside verified top-level mdat payloads is
returned for the existing identity callback, including opaque colr/unknown data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import base64
import hashlib
import importlib
import io
import json
import math
import multiprocessing
import os
import re
from pathlib import Path
import stat
import sys
import time


class MP4InspectionError(ValueError):
    """Fixed diagnostic codes only; no source paths, metadata, or parser errors."""


@dataclass(frozen=True)
class MP4Limits:
    max_bytes: int = 64_000_000
    max_metadata_bytes: int = 2_000_000
    max_fields: int = 100_000
    max_depth: int = 64
    max_packets: int = 100_000
    max_seconds: float = 20.0


def _limits(value: MP4Limits) -> None:
    for name, maximum in (
        ("max_bytes", 64_000_000),
        ("max_metadata_bytes", 2_000_000),
        ("max_fields", 100_000),
        ("max_depth", 64),
        ("max_packets", 100_000),
    ):
        number = getattr(value, name)
        if type(number) is not int or not 0 < number <= maximum:
            raise MP4InspectionError("mp4_invalid_limits")
    if (
        type(value.max_seconds) not in (int, float)
        or not math.isfinite(value.max_seconds)
        or not 0 < value.max_seconds <= 20
    ):
        raise MP4InspectionError("mp4_invalid_limits")


def _require_hachoir() -> None:
    try:
        dependency = importlib.import_module("hachoir")
    except ImportError:
        raise MP4InspectionError("mp4_hachoir_unavailable") from None
    if getattr(dependency, "__version__", None) != "3.4.0":
        raise MP4InspectionError("mp4_hachoir_version_unverified")


def _read_source(path: Path, maximum: int) -> bytes:
    try:
        flags = (
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise MP4InspectionError("mp4_source_size_or_type")
            data = source.read(maximum + 1)
        if not data or len(data) > maximum:
            raise MP4InspectionError("mp4_source_size_or_type")
        return data
    except OSError:
        raise MP4InspectionError("mp4_source_unreadable") from None


def _reserved_role(field):
    """Only proven Hachoir 3.4.0 all-one fields; not a replacement box schema."""
    from hachoir.field import NullBits
    from hachoir.parser.container.mp4 import AVCDecoderConfigurationRecord, Descriptor

    if type(field) is not NullBits:
        return None
    if (
        type(field.parent) is AVCDecoderConfigurationRecord
        and field.name.startswith("reserved[")
        and field.size in {6, 3, 5}
    ):
        return "hachoir_avc_reserved_ones"
    if (
        type(field.parent) is Descriptor
        and field.parent["tag"].value == 4
        and field.name == "reserved"
        and field.size == 1
    ):
        return "hachoir_es_reserved_one"
    return None


def _known_diagnostic(data: bytes, level, text, field):
    from hachoir.core.log import log
    from hachoir.field import String
    from hachoir.parser.container.mp4 import Atom

    if level != log.LOG_WARN:
        return None
    role = _reserved_role(field)
    if (
        role is not None
        and text == "padding contents doesn't look normal (invalid pattern)"
        and field.value == (1 << field.size) - 1
    ):
        code = role
    elif (
        type(field) is String
        and type(field.parent) is Atom
        and field.name == "tag"
        and field.size == 32
        and field.absolute_address % 8 == 0
        and text.startswith("Unable to convert string to Unicode:")
        and data[field.absolute_address // 8 : field.absolute_address // 8 + 4]
        == b"\xa9too"
    ):
        code = "hachoir_encoder_fourcc_ascii_fallback"
    else:
        return None
    return {
        "code": code,
        "offset_bits": field.absolute_address,
        "size_bits": field.size,
    }


def _strict_parse(data: bytes, limits: MP4Limits) -> dict:
    from hachoir.core import config
    from hachoir.core.log import log
    from hachoir.field import RawBytes
    from hachoir.field.generic_field_set import GenericFieldSet
    from hachoir.parser.container.mp4 import (
        Atom,
        MP4File,
        AVCDecoderConfigurationRecord,
    )
    from hachoir.stream import StringInputStream

    # These process-global settings live only in the isolated parser child.
    config.autofix = False
    config.quiet = False
    config.debug = False
    log.use_print = False
    log.use_buffer = False

    diagnostics = []

    def diagnostic(level, _prefix, text, field):
        if level < log.LOG_WARN:
            return
        recognized = _known_diagnostic(data, level, text, field)
        if recognized is None:
            raise MP4InspectionError("mp4_parser_diagnostic")
        if len(diagnostics) >= 4096:
            raise MP4InspectionError("mp4_diagnostic_limit")
        diagnostics.append(recognized)

    log.on_new_message = diagnostic
    stream = StringInputStream(data, source="MP4 identity inspection")
    parser = None
    deadline = time.monotonic() + limits.max_seconds
    state = {"fields": 0, "mdat": [], "avcc": []}

    def visit(field, depth, media_payload=False):
        state["fields"] += 1
        if state["fields"] > limits.max_fields or depth > limits.max_depth:
            raise MP4InspectionError("mp4_structure_limit")
        if time.monotonic() > deadline:
            raise MP4InspectionError("mp4_time_limit")
        if isinstance(field, GenericFieldSet):
            payload = None
            if (
                isinstance(field, Atom)
                and field.parent is parser
                and field["tag"].value == "mdat"
            ):
                payload = field["data"]
                if not isinstance(payload, RawBytes) or payload.size <= 0:
                    raise MP4InspectionError("mp4_unverified_mdat")
                first, bits = payload.absolute_address, payload.size
                if first % 8 or bits % 8:
                    raise MP4InspectionError("mp4_unverified_mdat")
                state["mdat"].append((first // 8, (first + bits) // 8))
            cursor = field.absolute_address
            for child in field:
                if child.absolute_address != cursor:
                    raise MP4InspectionError("mp4_structure_gap_or_overlap")
                visit(child, depth + 1, child is payload)
                cursor += child.size
            if not field.done or cursor != field.absolute_address + field.size:
                raise MP4InspectionError("mp4_structure_incomplete")
            if type(field) is AVCDecoderConfigurationRecord:
                if field.absolute_address % 8 or field.size % 8:
                    raise MP4InspectionError("mp4_avcc_boundary")
                state["avcc"].append((field.absolute_address // 8, cursor // 8))
        elif not media_payload:
            if field.size > limits.max_metadata_bytes * 8:
                raise MP4InspectionError("mp4_metadata_limit")
            # Do not evaluate arbitrary metadata values as semantic certification.
            # Only correct the two proven library reserved-bit expectations.
            if (
                _reserved_role(field) is not None
                and field.value != (1 << field.size) - 1
            ):
                raise MP4InspectionError("mp4_reserved_pattern_mismatch")

    try:
        parser = MP4File(stream, validate=True)
        parser._autofix = False
        if parser.autofix or parser.size != len(data) * 8:
            raise MP4InspectionError("mp4_unverified_parser")
        visit(parser, 0)
        if not state["mdat"]:
            raise MP4InspectionError("mp4_missing_mdat")
        return {
            "mdat_ranges": state["mdat"],
            "text_views": [],
            "diagnostics": diagnostics,
            "avcc_record_ranges": state["avcc"],
            "field_count": state["fields"],
        }
    finally:
        stream.close()


def _child_resources(limits: MP4Limits) -> dict:
    """Apply kernel caps before importing/allocating parser structures."""
    if os.name != "posix":
        return {
            "kernel_caps": False,
            "boundary": "process_timeout_and_logical_limits_only",
        }
    try:
        import resource

        cpu = min(45, math.ceil(limits.max_seconds) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16_000_000, 16_000_000))
        address_space = sys.platform.startswith("linux")
        if address_space:
            resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
    except (ImportError, OSError, ValueError):
        raise MP4InspectionError("mp4_resource_limits_unavailable") from None
    return {
        "kernel_caps": True,
        "cpu_seconds": cpu,
        "file_bytes": 16_000_000,
        "address_space_bytes": 2_000_000_000 if address_space else None,
        "boundary": "linux_as_cpu_fsize" if address_space else "posix_cpu_fsize_no_as",
    }


def _parse_worker(data: bytes, values: dict, connection) -> None:
    try:
        limits = MP4Limits(**values)
        resources = _child_resources(limits)
        _require_hachoir()
        result = _strict_parse(data, limits)
        result["resource_limits"] = resources
    except MP4InspectionError as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "mp4_strict_parse_failed"}
    try:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16_000_000:
            encoded = b'{"error":"mp4_result_limit"}'
        connection.send_bytes(encoded)
    finally:
        connection.close()


def _parse_bounded(data: bytes, limits: MP4Limits, seconds: float) -> dict:
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_parse_worker, args=(data, asdict(limits), sender), daemon=True
    )
    try:
        process.start()
        sender.close()
        if not receiver.poll(seconds):
            raise MP4InspectionError("mp4_time_limit")
        result = json.loads(receiver.recv_bytes(16_000_000))
        if "error" in result:
            raise MP4InspectionError(result["error"])
        return result
    except (EOFError, OSError, json.JSONDecodeError):
        raise MP4InspectionError("mp4_parser_process_failed") from None
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            process.join(timeout=0.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()


def _verified_packets(data: bytes, packet_ranges, mdats, limits: MP4Limits) -> None:
    if (
        not isinstance(packet_ranges, (list, tuple))
        or not 0 < len(packet_ranges) <= limits.max_packets
    ):
        raise MP4InspectionError("mp4_invalid_packet_ranges")
    packets = []
    for pair in packet_ranges:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or any(type(x) is not int for x in pair)
            or not 0 <= pair[0] < pair[1] <= len(data)
        ):
            raise MP4InspectionError("mp4_invalid_packet_ranges")
        packets.append(tuple(pair))
    packets.sort()
    if any(a[1] > b[0] for a, b in zip(packets, packets[1:])):
        raise MP4InspectionError("mp4_packet_overlap")
    index, previous = 0, 0
    for start, end in mdats:
        if not 0 <= previous <= start < end <= len(data):
            raise MP4InspectionError("mp4_unverified_mdat")
        cursor = start
        while index < len(packets) and packets[index][0] < end:
            first, last = packets[index]
            if first < start or last > end:
                raise MP4InspectionError("mp4_packet_outside_mdat")
            if any(data[cursor:first]):
                raise MP4InspectionError("mp4_nonzero_mdat_gap")
            cursor, index = last, index + 1
        if any(data[cursor:end]):
            raise MP4InspectionError("mp4_nonzero_mdat_gap")
        previous = end
    if index != len(packets):
        raise MP4InspectionError("mp4_packet_outside_mdat")


def inspect_mp4_metadata(
    path: Path,
    packet_ranges,
    *,
    limits: MP4Limits | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Return all metadata bytes; ranges must come from an already verified decoder.

    Caller must scan every metadata chunk and decoded text view, bind this returned
    source hash to its decode, and scan decoded audio/video/cover images separately.
    No unknown metadata, padding, or numeric region is exempted by the helper.
    """
    limits = limits or MP4Limits()
    _limits(limits)
    _require_hachoir()
    started = time.monotonic()
    data = _read_source(Path(path), limits.max_bytes)
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise MP4InspectionError("mp4_source_hash_mismatch")
    seconds = limits.max_seconds - (time.monotonic() - started)
    if seconds <= 0:
        raise MP4InspectionError("mp4_time_limit")
    parsed = _parse_bounded(data, limits, seconds)
    mdats = tuple(tuple(pair) for pair in parsed["mdat_ranges"])
    _verified_packets(data, packet_ranges, mdats, limits)
    for event in parsed["diagnostics"]:
        first, last = event["offset_bits"], event["offset_bits"] + event["size_bits"]
        if (
            first < 0
            or last > len(data) * 8
            or any(first < end * 8 and start * 8 < last for start, end in mdats)
        ):
            raise MP4InspectionError("mp4_diagnostic_outside_metadata")
    metadata_ranges, cursor = [], 0
    for first, last in mdats:
        if cursor < first:
            metadata_ranges.append((cursor, first))
        cursor = last
    if cursor < len(data):
        metadata_ranges.append((cursor, len(data)))
    if sum(last - first for first, last in metadata_ranges) > limits.max_metadata_bytes:
        raise MP4InspectionError("mp4_metadata_limit")
    if hashlib.sha256(_read_source(Path(path), limits.max_bytes)).hexdigest() != digest:
        raise MP4InspectionError("mp4_source_changed")
    if time.monotonic() - started > limits.max_seconds:
        raise MP4InspectionError("mp4_time_limit")
    return {
        "metadata_chunks": tuple(data[first:last] for first, last in metadata_ranges),
        "metadata_ranges": tuple(metadata_ranges),
        "mdat_ranges": mdats,
        "text_views": tuple(parsed["text_views"]),
        "field_count": parsed["field_count"],
        "source_sha256": digest,
        "hachoir_version": "3.4.0",
        "hachoir_license": "GNU GPL v2 (external optional dependency)",
        "resource_limits": parsed["resource_limits"],
        "diagnostics": tuple(parsed["diagnostics"]),
        "diagnostic_count": len(parsed["diagnostics"]),
        "avcc_record_ranges": tuple(
            tuple(pair) for pair in parsed["avcc_record_ranges"]
        ),
        "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


# This parses FFmpeg's textual report, not H.264 binary packets. The caller must
# bind a successful full trace_headers invocation and decode to the source hash.
_TRACE_PREFIX = re.compile(r"^\[trace_headers @ (0x[0-9a-fA-F]+)\]\s*(.*)$")
_TRACE_FIELD = re.compile(
    r"^(\d{1,10})\s+([A-Za-z_][A-Za-z_0-9]*(?:\[\d+\])*)\s+([01]{1,64})\s*=\s*(-?\d{1,20})\s*$"
)
_TRACE_HEADINGS = {
    "Sequence Parameter Set": {7},
    "Picture Parameter Set": {8},
    "Supplemental Enhancement Information": {6},
    "Slice Header": {1, 5},
    "Access Unit Delimiter": {9},
}
_TRACE_NALS = {1, 5, 6, 7, 8, 9}


def _integer_columns_agree(bits: str, value: int) -> bool:
    """Cross-check fixed or Exp-Golomb integer columns already decoded by FFmpeg."""
    if int(bits, 2) == value:
        return True
    zeros = len(bits) - len(bits.lstrip("0"))
    if len(bits) != 2 * zeros + 1 or zeros == len(bits):
        return False
    unsigned = (1 << zeros) - 1 + int(bits[zeros + 1 :] or "0", 2)
    signed = (unsigned + 1) // 2 if unsigned % 2 else -(unsigned // 2)
    return value in {unsigned, signed}


def _trace_units(report: bytes):
    if not isinstance(report, bytes) or not 0 < len(report) <= 16_000_000:
        raise MP4InspectionError("mp4_trace_report_limit")
    try:
        lines = io.StringIO(report.decode("utf-8", errors="strict"))
    except UnicodeError:
        raise MP4InspectionError("mp4_trace_report_encoding") from None
    units, current, heading, reporter = [], [], None, None
    banners, packet_count, rows, last_trace = [], 0, 0, -1
    footer = {"Input": [], "Output": []}
    avcc_tail_bytes = 0
    deadline = time.monotonic() + 20
    for position, line in enumerate(lines):
        line = line.rstrip("\r\n")
        if position >= 250_000 or len(line) > 4096 or time.monotonic() > deadline:
            raise MP4InspectionError("mp4_trace_report_limit")
        summary = re.search(
            r"(Input|Output) stream #0:(\d+) \(([^)]+)\): (\d+) packets (read|muxed)",
            line,
        )
        if summary:
            direction, stream, kind, count, operation = summary.groups()
            if (
                stream != "0"
                or kind != "video"
                or operation != {"Input": "read", "Output": "muxed"}[direction]
            ):
                raise MP4InspectionError("mp4_trace_unsupported_stream")
            footer[direction].append((position, int(count)))
        match = _TRACE_PREFIX.fullmatch(line)
        if not match:
            continue
        identity, text = match.groups()
        if reporter is not None and identity != reporter:
            raise MP4InspectionError("mp4_trace_mixed_reporters")
        reporter, last_trace = identity, position
        if text in _TRACE_HEADINGS or text.startswith("Packet:") or text == "Extradata":
            if current:
                units.append((heading, current, banners))
                current, banners = [], []
            elif heading is not None:
                raise MP4InspectionError("mp4_trace_incomplete_unit")
            heading = text if text in _TRACE_HEADINGS else None
            if text.startswith("Packet:"):
                if not re.fullmatch(r"Packet: \d+ bytes(?:,.*)?\.", text):
                    raise MP4InspectionError("mp4_trace_packet_format")
                packet_count += 1
            continue
        tail = re.fullmatch(r"(\d{1,7}) bytes left at end of AVCC header\.", text)
        if tail:
            if tail.group(1) != "4" or avcc_tail_bytes:
                raise MP4InspectionError("mp4_trace_unverified_avcc_tail")
            avcc_tail_bytes = 4
            continue
        if text == "User Data Unregistered":
            if not current:
                raise MP4InspectionError("mp4_trace_sei_banner")
            banners.append(len(current))
            continue
        if text.startswith("nal_unit_type:"):
            summary = re.fullmatch(
                r"nal_unit_type: (\d+)\([A-Za-z0-9 -]+\), nal_ref_idc: [0-3]", text
            )
            if summary is None or int(summary.group(1)) not in _TRACE_NALS:
                raise MP4InspectionError("mp4_trace_unsupported_nal")
            continue
        field = _TRACE_FIELD.fullmatch(text)
        if field is None or heading is None:
            raise MP4InspectionError("mp4_trace_unknown_row")
        offset, name, bits, value = field.groups()
        if len(name) > 128 or not _integer_columns_agree(bits, int(value)):
            raise MP4InspectionError("mp4_trace_bit_value_mismatch")
        offset = int(offset)
        if offset != (current[-1][0] + len(current[-1][2]) if current else 0):
            raise MP4InspectionError("mp4_trace_offset_discontinuity")
        current.append((offset, name, bits, int(value)))
        rows += 1
        if rows > 200_000:
            raise MP4InspectionError("mp4_trace_report_limit")
    if current:
        units.append((heading, current, banners))
    elif heading is not None:
        raise MP4InspectionError("mp4_trace_incomplete_unit")
    if not units or not 0 < packet_count <= 100_000:
        raise MP4InspectionError("mp4_trace_missing_units")
    for direction in footer.values():
        if (
            len(direction) != 1
            or direction[0][0] <= last_trace
            or direction[0][1] != packet_count
        ):
            raise MP4InspectionError("mp4_trace_incomplete_report")
    return units, packet_count, avcc_tail_bytes


def _unit_type(heading, rows, banners):
    if len(rows) < 4:
        raise MP4InspectionError("mp4_trace_incomplete_unit")
    if rows[0] != (0, "forbidden_zero_bit", "0", 0):
        raise MP4InspectionError("mp4_trace_nal_header")
    for row, name, width in (
        (rows[1], "nal_ref_idc", 2),
        (rows[2], "nal_unit_type", 5),
    ):
        if row[1] != name or len(row[2]) != width or int(row[2], 2) != row[3]:
            raise MP4InspectionError("mp4_trace_nal_header")
    kind = rows[2][3]
    if kind not in _TRACE_HEADINGS[heading] or kind not in _TRACE_NALS:
        raise MP4InspectionError("mp4_trace_unsupported_nal")
    if kind != 6:
        if banners or any(
            row[1].startswith(
                (
                    "uuid_iso_iec_11578",
                    "user_data_payload_byte",
                    "last_payload_",
                    "ff_byte",
                )
            )
            for row in rows
        ):
            raise MP4InspectionError("mp4_trace_metadata_wrong_nal")
        if kind in {7, 8, 9}:
            _rbsp_end(
                rows,
                next(
                    (i for i, row in enumerate(rows) if row[1] == "rbsp_stop_one_bit"),
                    len(rows),
                ),
            )
    elif rows[1][3] != 0:
        raise MP4InspectionError("mp4_trace_nal_header")
    return kind


def _rbsp_end(rows, index):
    if index >= len(rows) or rows[index][1:] != ("rbsp_stop_one_bit", "1", 1):
        raise MP4InspectionError("mp4_trace_incomplete_rbsp")
    padding = (-(rows[index][0] + 1)) % 8
    rest = rows[index + 1 :]
    if len(rest) != padding or any(
        row[1:] != ("rbsp_alignment_zero_bit", "0", 0) for row in rest
    ):
        raise MP4InspectionError("mp4_trace_incomplete_rbsp")


def _sei_length(rows, index, final_name):
    value = 0
    while index < len(rows) and rows[index][1] == "ff_byte":
        if rows[index][2:] != ("11111111", 255):
            raise MP4InspectionError("mp4_trace_extended_length")
        value, index = value + 255, index + 1
        if value > 2_000_000:
            raise MP4InspectionError("mp4_trace_metadata_limit")
    if (
        index >= len(rows)
        or rows[index][1] != final_name
        or len(rows[index][2]) != 8
        or not 0 <= rows[index][3] < 255
        or int(rows[index][2], 2) != rows[index][3]
    ):
        raise MP4InspectionError("mp4_trace_sei_length")
    return value + rows[index][3], index + 1


def _sei_messages(rows, banners):
    index, messages, used_banners, size_total = 3, [], [], 0
    while index < len(rows) and rows[index][1] != "rbsp_stop_one_bit":
        kind, index = _sei_length(rows, index, "last_payload_type_byte")
        if kind != 5:
            raise MP4InspectionError("mp4_trace_unsupported_sei")
        size, index = _sei_length(rows, index, "last_payload_size_byte")
        if size < 16 or size_total + size > 2_000_000 or len(messages) >= 1024:
            raise MP4InspectionError("mp4_trace_metadata_limit")
        if index not in banners:
            raise MP4InspectionError("mp4_trace_sei_banner")
        used_banners.append(index)
        chunks = []
        for name, length in (
            ("uuid_iso_iec_11578", 16),
            ("user_data_payload_byte", size - 16),
        ):
            value = bytearray()
            for number in range(length):
                if (
                    index >= len(rows)
                    or rows[index][1] != f"{name}[{number}]"
                    or len(rows[index][2]) != 8
                    or not 0 <= rows[index][3] <= 255
                    or int(rows[index][2], 2) != rows[index][3]
                ):
                    raise MP4InspectionError("mp4_trace_sei_byte_or_index")
                value.append(rows[index][3])
                index += 1
            chunks.append(bytes(value))
        messages.append(tuple(chunks))
        size_total += size
    if not messages or banners != used_banners:
        raise MP4InspectionError("mp4_trace_incomplete_sei")
    _rbsp_end(rows, index)
    return messages


def extract_h264_sei_metadata(
    report: bytes, *, expected_packets: int | None = None
) -> dict:
    """Bounded trace_headers report adapter; caller authenticates process/source.

    UUID and payload chunks remain separate so existing byte normalizers can
    inspect UTF-16 payloads without losing their alignment to the UUID prefix.
    Unsupported SEI/NAL types are rejected rather than silently omitted.
    """
    units, packets, avcc_tail_bytes = _trace_units(report)
    if expected_packets is not None and (
        type(expected_packets) is not int or expected_packets != packets
    ):
        raise MP4InspectionError("mp4_trace_packet_count")
    messages, types, sei_units, total = [], set(), 0, 0
    for heading, rows, banners in units:
        kind = _unit_type(heading, rows, banners)
        types.add(kind)
        if kind == 6:
            values = _sei_messages(rows, banners)
            total += sum(len(uuid) + len(payload) for uuid, payload in values)
            messages.extend(values)
            sei_units += 1
            if total > 2_000_000 or len(messages) > 1024:
                raise MP4InspectionError("mp4_trace_metadata_limit")
    if not {7, 8} <= types or not types.intersection({1, 5}):
        raise MP4InspectionError("mp4_trace_incomplete_stream")
    return {
        "metadata_chunks": tuple(chunk for pair in messages for chunk in pair),
        "message_count": len(messages),
        "sei_nal_count": sei_units,
        "uuid_count": len(messages),
        "metadata_bytes": total,
        "packet_count": packets,
        "nal_unit_count": len(units),
        "nal_types": tuple(sorted(types)),
        "trace_sha256": hashlib.sha256(report).hexdigest(),
        "avcc_unparsed_tail_bytes": avcc_tail_bytes,
        "avcc_tail_metadata_bound": avcc_tail_bytes == 0,
        "trace_diagnostics": (
            [{"code": "ffmpeg_avcc_four_byte_tail", "bytes": 4, "count": 1}]
            if avcc_tail_bytes
            else []
        ),
    }


def _bind_avcc_tail(codec: dict, container: dict) -> None:
    """A fixed report INFO never exempts bytes: bind its exact metadata span."""
    ranges = []
    if codec["avcc_unparsed_tail_bytes"]:
        records = container["avcc_record_ranges"]
        if len(records) != 1 or records[0][1] - records[0][0] < 4:
            raise MP4InspectionError("mp4_trace_unbound_avcc_tail")
        first, last = records[0][1] - 4, records[0][1]
        if not any(
            start <= first and last <= end
            for start, end in container["metadata_ranges"]
        ):
            raise MP4InspectionError("mp4_trace_unbound_avcc_tail")
        ranges.append((first, last))
    codec["avcc_tail_metadata_ranges"] = ranges
    codec["avcc_tail_metadata_bound"] = True


def main(argv=None) -> int:
    """Isolated file CLI: private successful metadata is bounded IPC, not logs."""
    import argparse

    class Parser(argparse.ArgumentParser):
        def error(self, _message):
            raise MP4InspectionError("mp4_cli_arguments")

    try:
        parser = Parser(prog="mp4_identity")
        parser.add_argument("--source", required=True)
        parser.add_argument("--packet-ranges", required=True)
        parser.add_argument("--trace-report", required=True)
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--codec", required=True)
        args = parser.parse_args(argv)
        if (
            args.codec != "h264"
            or re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256) is None
        ):
            raise MP4InspectionError("mp4_cli_codec_or_hash")
        packets = json.loads(_read_source(Path(args.packet_ranges), 12_000_000))
        if not isinstance(packets, list) or not 0 < len(packets) <= 100_000:
            raise MP4InspectionError("mp4_invalid_packet_ranges")
        codec = extract_h264_sei_metadata(
            _read_source(Path(args.trace_report), 16_000_000),
            expected_packets=len(packets),
        )
        result = inspect_mp4_metadata(
            Path(args.source), packets, expected_sha256=args.expected_sha256
        )
        _bind_avcc_tail(codec, result)
        result["metadata_chunks_b64"] = [
            base64.b64encode(value).decode("ascii")
            for value in result.pop("metadata_chunks")
        ]
        result["codec_metadata_chunks_b64"] = [
            base64.b64encode(value).decode("ascii")
            for value in codec.pop("metadata_chunks")
        ]
        result["codec_metadata"] = codec
        result["codec_metadata_extracted"] = True
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 16_000_000:
            raise MP4InspectionError("mp4_result_limit")
        sys.stdout.buffer.write(encoded + b"\n")
        return 0
    except Exception as exc:
        code = (
            str(exc) if isinstance(exc, MP4InspectionError) else "mp4_cli_invalid_input"
        )
        sys.stdout.write(json.dumps({"status": "error", "code": code}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
