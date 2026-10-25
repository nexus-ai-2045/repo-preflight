"""Synthetic parser/coverage cases; private actual source is opt-in, never copied."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from types import SimpleNamespace

import pytest

from scripts import mp4_identity as helper

DENIED = b"media-fixture-private@example.invalid"


def atom(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, tag) + payload


def synthetic(
    tmp_path, payload=b"synthetic-media", extra=b"", *, minor=b"\x00\x00\x02\x00"
):
    prefix = atom(b"ftyp", b"isom" + minor + b"isomiso2")
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(prefix + atom(b"mdat", payload) + extra)
    first = len(prefix) + 8
    return path, (first, first + len(payload))


@pytest.fixture
def hachoir():
    dependency = pytest.importorskip("hachoir")
    if dependency.__version__ != "3.4.0":
        pytest.skip("strict parser dependency must be pinned to Hachoir 3.4.0")
    return dependency


def inspect(path, ranges, **kwargs):
    return helper.inspect_mp4_metadata(path, ranges, **kwargs)


def test_normal_structure_returns_exact_metadata_and_media_partition(tmp_path, hachoir):
    path, media = synthetic(tmp_path)
    before = path.read_bytes()
    result = inspect(path, [media], expected_sha256=hashlib.sha256(before).hexdigest())
    assert result["mdat_ranges"] == (media,)
    covered = [False] * len(before)
    for first, last in (*result["metadata_ranges"], *result["mdat_ranges"]):
        assert not any(covered[first:last])
        covered[first:last] = [True] * (last - first)
    assert all(covered)
    assert result["metadata_chunks"] == tuple(
        before[a:b] for a, b in result["metadata_ranges"]
    )
    assert b"synthetic-media" not in b"".join(result["metadata_chunks"])
    assert result["hachoir_version"] == "3.4.0"
    assert (
        result["helper_sha256"]
        == hashlib.sha256(Path(helper.__file__).read_bytes()).hexdigest()
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("tag", [b"free", b"skip", b"zzzz"])
def test_unknown_padding_and_opaque_metadata_remain_visible(tmp_path, hachoir, tag):
    path, media = synthetic(tmp_path, extra=atom(tag, b"\x00\x00" + DENIED))
    result = inspect(path, [media])
    assert DENIED in b"".join(result["metadata_chunks"])


def test_known_numeric_region_is_not_raw_exempted(tmp_path, hachoir):
    path, media = synthetic(tmp_path, minor=b"LEAK")
    assert b"LEAK" in b"".join(inspect(path, [media])["metadata_chunks"])


def test_hidden_nonzero_mdat_gap_is_rejected(tmp_path, hachoir):
    path, media = synthetic(tmp_path, payload=b"ABC" + DENIED)
    with pytest.raises(helper.MP4InspectionError, match="nonzero_mdat_gap"):
        inspect(path, [(media[0], media[0] + 3)])


def test_only_zero_mdat_gaps_are_allowed(tmp_path, hachoir):
    path, media = synthetic(tmp_path, payload=b"\x00ABC\x00")
    assert inspect(path, [(media[0] + 1, media[1] - 1)])["mdat_ranges"] == (media,)


@pytest.mark.parametrize(
    "case", ["missing", "negative", "bool", "reverse", "overlap", "outside", "past_eof"]
)
def test_invalid_or_forged_packet_intervals_are_rejected(tmp_path, hachoir, case):
    path, (first, last) = synthetic(tmp_path)
    ranges = {
        "missing": [],
        "negative": [(-1, last)],
        "bool": [(True, last)],
        "reverse": [(last, first)],
        "overlap": [(first, last), (first, last)],
        "outside": [(0, first)],
        "past_eof": [(first, last + 1)],
    }[case]
    with pytest.raises(helper.MP4InspectionError):
        inspect(path, ranges)


def test_truncation_rejected_by_strict_parser(tmp_path, hachoir):
    path, media = synthetic(tmp_path)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(helper.MP4InspectionError):
        inspect(path, [media])


def test_missing_mdat_rejected(tmp_path, hachoir):
    path, media = synthetic(tmp_path)
    path.write_bytes(path.read_bytes()[: media[0] - 8])
    with pytest.raises(helper.MP4InspectionError, match="missing_mdat"):
        inspect(path, [media])


@pytest.mark.parametrize(
    "limits",
    [
        helper.MP4Limits(max_bytes=8),
        helper.MP4Limits(max_fields=2),
        helper.MP4Limits(max_depth=1),
        helper.MP4Limits(max_metadata_bytes=8),
        helper.MP4Limits(max_seconds=0.001),
    ],
)
def test_resource_limits_fail_closed(tmp_path, hachoir, limits):
    path, media = synthetic(tmp_path)
    with pytest.raises(helper.MP4InspectionError):
        inspect(path, [media], limits=limits)


def test_absent_dependency_is_fixed_error_without_source_details(tmp_path, monkeypatch):
    original = helper.importlib.import_module

    def unavailable(name):
        if name == "hachoir":
            raise ImportError("untrusted source detail")
        return original(name)

    monkeypatch.setattr(helper.importlib, "import_module", unavailable)
    path, media = synthetic(tmp_path)
    with pytest.raises(helper.MP4InspectionError, match="^mp4_hachoir_unavailable$"):
        inspect(path, [media])


def test_wrong_dependency_version_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        helper.importlib,
        "import_module",
        lambda _name: SimpleNamespace(__version__="unknown"),
    )
    path, media = synthetic(tmp_path)
    with pytest.raises(
        helper.MP4InspectionError, match="^mp4_hachoir_version_unverified$"
    ):
        inspect(path, [media])


def test_source_digest_mismatch_stops_before_parser(tmp_path, hachoir):
    path, media = synthetic(tmp_path)
    with pytest.raises(helper.MP4InspectionError, match="source_hash_mismatch"):
        inspect(path, [media], expected_sha256="0" * 64)


def ffprobe_ranges(path):
    probe = shutil.which("ffprobe")
    decoder = shutil.which("ffmpeg")
    if not probe or not decoder:
        pytest.skip("optional ffprobe/ffmpeg needed for actual decoder-bound fixture")
    decode = subprocess.run(
        [
            decoder,
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=20,
    )
    assert decode.returncode == 0, "full fixture decode failed"
    result = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_packets",
            "-show_entries",
            "packet=pos,size",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, "fixture packet probe failed"
    return [
        (int(x["pos"]), int(x["pos"]) + int(x["size"]))
        for x in json.loads(result.stdout)["packets"]
    ]


@pytest.fixture
def real_video(tmp_path, hachoir):
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("optional ffmpeg needed for video fixture")
    path = tmp_path / "normal.mp4"
    run = subprocess.run(
        [
            binary,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=16x16:d=0.2",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 0, "video fixture creation failed"
    return path


def test_real_decoded_media_ranges_account_for_nonzero_payload(real_video):
    assert inspect(real_video, ffprobe_ranges(real_video))["field_count"] > 10


def test_mvhd_extra_trailer_is_not_repaired_or_ignored(real_video):
    from hachoir.parser.container.mp4 import Atom, MP4File
    from hachoir.field.generic_field_set import GenericFieldSet
    from hachoir.stream import StringInputStream

    original = real_video.read_bytes()
    parser = MP4File(StringInputStream(original), validate=True)

    def atoms(field):
        if isinstance(field, GenericFieldSet):
            for child in field:
                if isinstance(child, Atom):
                    yield child
                yield from atoms(child)

    values = list(atoms(parser))
    moov = next(x for x in values if x.parent is parser and x["tag"].value == "moov")
    mvhd = next(x for x in values if x["tag"].value == "mvhd")
    offset = (mvhd.absolute_address + mvhd.size) // 8
    changed = bytearray(original[:offset] + DENIED + original[offset:])
    for field in (moov, mvhd):
        start = field.absolute_address // 8
        changed[start : start + 4] = (field.size // 8 + len(DENIED)).to_bytes(4, "big")
    parser.close()
    real_video.write_bytes(changed)
    with pytest.raises(
        helper.MP4InspectionError, match="strict_parse|parser_diagnostic|structure"
    ):
        inspect(real_video, [(1, 2)])


def test_actual_source_unchanged_readback(hachoir):
    receipt = os.environ.get("PREFLIGHT_MP4_ACTUAL_RECEIPT")
    if not receipt:
        pytest.skip(
            "private actual-source receipt is opt-in and never checked into tests"
        )
    rows = json.loads(Path(receipt).read_text())
    assert isinstance(rows, list) and len(rows) == 1
    entry = rows[0]
    path = Path(entry["path"])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert before == entry["sha256"]
    result = inspect(path, ffprobe_ranges(path), expected_sha256=before)
    assert result["source_sha256"] == before
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    # Actual colr opaque bytes stay in the caller's metadata inspection, not exempted.
    assert b"colr" in b"".join(result["metadata_chunks"])


@pytest.mark.skipif(os.name != "posix", reason="POSIX kernel resource caps")
def test_parser_child_reports_kernel_caps(tmp_path, hachoir):
    path, media = synthetic(tmp_path)
    limits = inspect(path, [media])["resource_limits"]
    assert limits["kernel_caps"] is True
    assert limits["cpu_seconds"] <= 45
    assert limits["file_bytes"] == 16_000_000
    if __import__("sys").platform.startswith("linux"):
        assert limits["address_space_bytes"] == 2_000_000_000
    else:
        assert limits["address_space_bytes"] is None


def set_bits(data: bytes, first: int, width: int, value: int) -> bytes:
    changed = bytearray(data)
    for shift in range(width):
        byte, bit = divmod(first + shift, 8)
        mask = 1 << (7 - bit)
        changed[byte] = (
            (changed[byte] | mask)
            if value & (1 << (width - shift - 1))
            else (changed[byte] & ~mask)
        )
    return bytes(changed)


def test_es_reserved_one_diagnostic_is_retained_and_its_metadata_scanned(real_video):
    result = inspect(real_video, ffprobe_ranges(real_video))
    kinds = {x["code"] for x in result["diagnostics"]}
    assert kinds == {"hachoir_es_reserved_one", "hachoir_encoder_fourcc_ascii_fallback"}
    assert result["diagnostic_count"] == 2
    for event in result["diagnostics"]:
        first, last = (
            event["offset_bits"] // 8,
            (event["offset_bits"] + event["size_bits"] + 7) // 8,
        )
        assert any(
            start <= first and last <= end for start, end in result["metadata_ranges"]
        )


def test_zero_es_reserved_bit_is_not_certified_by_library_zero_expectation(real_video):
    ranges = ffprobe_ranges(real_video)
    result = inspect(real_video, ranges)
    event = next(
        x for x in result["diagnostics"] if x["code"] == "hachoir_es_reserved_one"
    )
    real_video.write_bytes(
        set_bits(real_video.read_bytes(), event["offset_bits"], event["size_bits"], 0)
    )
    with pytest.raises(helper.MP4InspectionError, match="reserved_pattern_mismatch"):
        inspect(real_video, ranges)


def test_unadopted_fourcc_fallback_stays_rejected(tmp_path, hachoir):
    path, media = synthetic(tmp_path, extra=atom(b"\xa9nam", b"opaque"))
    with pytest.raises(helper.MP4InspectionError, match="parser_diagnostic"):
        inspect(path, [media])


def test_wrong_ancestor_reserved_warning_stays_rejected(real_video):
    from hachoir.parser.container.mp4 import MP4File, MovieHeader
    from hachoir.field.generic_field_set import GenericFieldSet
    from hachoir.stream import StringInputStream

    parser = MP4File(StringInputStream(real_video.read_bytes()), validate=True)

    def find(field):
        if isinstance(field, MovieHeader):
            return field["flags"].absolute_address, field["flags"].size
        if isinstance(field, GenericFieldSet):
            for child in field:
                result = find(child)
                if result is not None:
                    return result
        return None

    offset, size = find(parser)
    parser.close()
    ranges = ffprobe_ranges(real_video)
    real_video.write_bytes(set_bits(real_video.read_bytes(), offset, size, 1))
    with pytest.raises(helper.MP4InspectionError, match="parser_diagnostic"):
        inspect(real_video, ranges)


@pytest.fixture
def avc_video(tmp_path, hachoir):
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("optional ffmpeg needed for AVC fixture")
    path = tmp_path / "avc.mp4"
    run = subprocess.run(
        [
            binary,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=16x16:d=0.2",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        timeout=20,
    )
    if b"Unknown encoder" in run.stderr:
        pytest.skip("optional AVC encoder unavailable")
    assert run.returncode == 0, "AVC fixture creation failed"
    return path


def test_proven_avc_ones_diagnostic_keeps_exact_metadata_coverage(avc_video):
    result = inspect(avc_video, ffprobe_ranges(avc_video))
    values = [
        x for x in result["diagnostics"] if x["code"] == "hachoir_avc_reserved_ones"
    ]
    assert [x["size_bits"] for x in values] == [6, 3, 6, 5, 5]
    for event in values:
        first, last = (
            event["offset_bits"] // 8,
            (event["offset_bits"] + event["size_bits"] + 7) // 8,
        )
        assert any(
            start <= first and last <= end for start, end in result["metadata_ranges"]
        )


@pytest.mark.parametrize("replacement", [0, 62])
def test_partial_or_zero_avc_reserved_bits_stay_rejected(avc_video, replacement):
    ranges = ffprobe_ranges(avc_video)
    result = inspect(avc_video, ranges)
    event = next(
        x for x in result["diagnostics"] if x["code"] == "hachoir_avc_reserved_ones"
    )
    avc_video.write_bytes(
        set_bits(
            avc_video.read_bytes(),
            event["offset_bits"],
            event["size_bits"],
            replacement,
        )
    )
    with pytest.raises(
        helper.MP4InspectionError, match="parser_diagnostic|reserved_pattern_mismatch"
    ):
        inspect(avc_video, ranges)


def synthetic_trace(messages=None):
    """Text report fixtures only; these are never claimed as actual decoder evidence."""
    prefix = "[trace_headers @ 0x1234] "
    lines = [prefix + "Extradata"]

    def unit(heading, kind, body):
        lines.append(prefix + heading)
        offset = 0

        def emit(name, bits, value):
            nonlocal offset
            lines.append(prefix + f"{offset} {name} {bits} = {value}")
            offset += len(bits)

        emit("forbidden_zero_bit", "0", 0)
        emit(
            "nal_ref_idc", "00" if kind in {6, 9} else "11", 0 if kind in {6, 9} else 3
        )
        emit("nal_unit_type", f"{kind:05b}", kind)
        body(emit, lambda: offset)

    def trailing(emit, position):
        emit("rbsp_stop_one_bit", "1", 1)
        while position() % 8:
            emit("rbsp_alignment_zero_bit", "0", 0)

    unit("Sequence Parameter Set", 7, trailing)
    unit("Picture Parameter Set", 8, trailing)
    lines.append(prefix + "Packet: 10 bytes, key frame.")
    if messages:

        def sei(emit, position):
            def length(number, name):
                while number >= 255:
                    emit("ff_byte", "11111111", 255)
                    number -= 255
                emit(name, f"{number:08b}", number)

            for uuid, payload in messages:
                length(5, "last_payload_type_byte")
                length(len(uuid) + len(payload), "last_payload_size_byte")
                lines.append(prefix + "User Data Unregistered")
                for name, data in (
                    ("uuid_iso_iec_11578", uuid),
                    ("user_data_payload_byte", payload),
                ):
                    for index, byte in enumerate(data):
                        emit(f"{name}[{index}]", f"{byte:08b}", byte)
            trailing(emit, position)

        unit("Supplemental Enhancement Information", 6, sei)
    unit("Slice Header", 5, lambda emit, _pos: emit("first_mb_in_slice", "1", 0))
    lines += [
        "Output stream #0:0 (video): 1 packets muxed (10 bytes);",
        "Input stream #0:0 (video): 1 packets read (10 bytes);",
    ]
    return ("\n".join(lines) + "\n").encode()


def test_trace_complete_uuid_payload_multiple_extended_and_utf16():
    uuid = b"UUID-PRIVATE-001"
    long_data = b"prefix-" + DENIED + b"A" * 600 + b"-suffix"
    utf16 = "fixture-name-文字".encode("utf-16-le")
    report = synthetic_trace([(uuid, long_data), (bytes(range(16)), utf16)])
    result = helper.extract_h264_sei_metadata(report, expected_packets=1)
    assert result["metadata_chunks"] == (uuid, long_data, bytes(range(16)), utf16)
    assert result["message_count"] == 2
    assert result["metadata_bytes"] == 32 + len(long_data) + len(utf16)


def test_trace_no_sei_normal_stream_is_supported():
    result = helper.extract_h264_sei_metadata(synthetic_trace(), expected_packets=1)
    assert result["metadata_chunks"] == ()
    assert result["message_count"] == 0


@pytest.mark.parametrize(
    "case",
    [
        "bad_bits",
        "index_gap",
        "offset_gap",
        "truncated",
        "length",
        "unknown_sei",
        "unknown_nal",
        "ff_byte",
        "missing_footer",
        "packet_count",
        "tail_size",
        "unknown_info",
    ],
)
def test_trace_malformed_or_unknown_reports_fail_closed(case):
    report = synthetic_trace([(bytes(range(16)), b"A" * 300)])
    text = report.decode()
    if case == "bad_bits":
        text = text.replace(
            "user_data_payload_byte[0] 01000001 = 65",
            "user_data_payload_byte[0] 01000001 = 66",
        )
    elif case == "index_gap":
        text = text.replace("user_data_payload_byte[1]", "user_data_payload_byte[9]")
    elif case == "offset_gap":
        lines = text.splitlines()
        index = next(i for i, x in enumerate(lines) if "user_data_payload_byte[0]" in x)
        parts = lines[index].split("] ", 1)
        offset, rest = parts[1].split(" ", 1)
        lines[index] = parts[0] + "] " + str(int(offset) + 1) + " " + rest
        text = "\n".join(lines)
    elif case == "truncated":
        text = text[: text.index("user_data_payload_byte[20]")]
    elif case == "length":
        text = text.replace(
            "last_payload_size_byte 00111101 = 61",
            "last_payload_size_byte 00111110 = 62",
        )
    elif case == "unknown_sei":
        text = text.replace(
            "last_payload_type_byte 00000101 = 5", "last_payload_type_byte 00000100 = 4"
        )
    elif case == "unknown_nal":
        text = text.replace("nal_unit_type 00110 = 6", "nal_unit_type 01100 = 12")
    elif case == "ff_byte":
        text = text.replace("ff_byte 11111111 = 255", "ff_byte 11111110 = 254", 1)
    elif case == "missing_footer":
        text = text[: text.index("Input stream #")]
    elif case == "packet_count":
        text = text.replace("1 packets read", "2 packets read")
    elif case == "tail_size":
        text = text.replace(
            "Extradata\n",
            "Extradata\n[trace_headers @ 0x1234] 5 bytes left at end of AVCC header.\n",
        )
    elif case == "unknown_info":
        text = text.replace(
            "Extradata\n",
            "Extradata\n[trace_headers @ 0x1234] unknown repair applied\n",
        )
    assert text.encode() != report
    with pytest.raises(helper.MP4InspectionError):
        helper.extract_h264_sei_metadata(text.encode(), expected_packets=1)


def test_trace_limit_and_packet_count_requirements():
    with pytest.raises(helper.MP4InspectionError, match="report_limit"):
        helper.extract_h264_sei_metadata(b"x" * 16_000_001)
    with pytest.raises(helper.MP4InspectionError, match="packet_count"):
        helper.extract_h264_sei_metadata(synthetic_trace(), expected_packets=True)


def traced_video(path, tmp_path, *, injection=None, remove_sei=False):
    binary = shutil.which("ffmpeg")
    if not binary:
        pytest.skip("optional ffmpeg needed for trace fixture")
    source = path
    if injection is not None or remove_sei:
        source = tmp_path / "codec-metadata.mp4"
        bsf = (
            "filter_units=remove_types=6"
            if remove_sei
            else "h264_metadata=sei_user_data=" + injection
        )
        remux = subprocess.run(
            [
                binary,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-bsf:v",
                bsf,
                str(source),
            ],
            capture_output=True,
            timeout=20,
        )
        assert remux.returncode == 0, "codec metadata fixture construction failed"
    report = subprocess.run(
        [
            binary,
            "-nostdin",
            "-v",
            "trace",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=20,
    )
    assert report.returncode == 0, "trace_headers fixture failed"
    return source, report.stderr


def test_full_command_independent_sei_prefix_suffix_and_uuid_recovered(
    avc_video, tmp_path
):
    uuid = "01234567-89ab-cdef-0123-456789abcdef"
    marker = "prefix-" + DENIED.decode() + "A" * 300 + "-suffix"
    path, report = traced_video(avc_video, tmp_path, injection=uuid + "+" + marker)
    packets = ffprobe_ranges(path)
    result = helper.extract_h264_sei_metadata(report, expected_packets=len(packets))
    assert bytes.fromhex(uuid.replace("-", "")) in result["metadata_chunks"]
    assert any(
        chunk.startswith(marker.encode()) and chunk.endswith(b"-suffix\x00")
        for chunk in result["metadata_chunks"]
    )
    assert (
        result["message_count"] >= 2
    )  # Independent injected message plus encoder message.


def test_full_command_video_without_sei_is_reported_as_empty(avc_video, tmp_path):
    path, report = traced_video(avc_video, tmp_path, remove_sei=True)
    result = helper.extract_h264_sei_metadata(
        report, expected_packets=len(ffprobe_ranges(path))
    )
    assert result["message_count"] == 0


def test_isolated_cli_from_unrelated_directory_recovers_all_codec_metadata(
    avc_video, tmp_path
):
    import base64
    import sys

    executable = os.environ.get("MP4_HELPER_CLI_PYTHON", sys.executable)
    installed = subprocess.run(
        [
            executable,
            "-I",
            "-c",
            "import hachoir; assert hachoir.__version__ == '3.4.0'",
        ],
        capture_output=True,
        timeout=10,
    )
    if installed.returncode:
        pytest.skip(
            "isolated CLI interpreter needs optional Hachoir in its site-packages"
        )
    path, report = traced_video(
        avc_video,
        tmp_path,
        injection="01234567-89ab-cdef-0123-456789abcdef+prefix-cli-marker-suffix",
    )
    packets = ffprobe_ranges(path)
    packet_file, trace_file = tmp_path / "packets.json", tmp_path / "trace.txt"
    packet_file.write_text(json.dumps(packets))
    trace_file.write_bytes(report)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    args = [
        executable,
        "-I",
        str(Path(helper.__file__).resolve()),
        "--source",
        str(path),
        "--packet-ranges",
        str(packet_file),
        "--trace-report",
        str(trace_file),
        "--expected-sha256",
        before,
        "--codec",
        "h264",
    ]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, timeout=30)
    assert result.returncode == 0, "isolated helper CLI failed"
    body = json.loads(result.stdout)
    assert body["source_sha256"] == before
    assert body["codec_metadata_extracted"] is True
    chunks = [
        base64.b64decode(x, validate=True) for x in body["codec_metadata_chunks_b64"]
    ]
    assert any(b"prefix-cli-marker-suffix" in x for x in chunks)
    assert body["codec_metadata"]["avcc_tail_metadata_bound"] is True
    for first, last in body["codec_metadata"]["avcc_tail_metadata_ranges"]:
        assert last - first == 4
        assert any(
            start <= first < last <= end for start, end in body["metadata_ranges"]
        )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    invalid = subprocess.run(
        args[:-1] + ["hevc"], cwd=tmp_path, capture_output=True, timeout=10
    )
    assert invalid.returncode != 0
    assert json.loads(invalid.stdout) == {
        "status": "error",
        "code": "mp4_cli_codec_or_hash",
    }
    assert invalid.stderr == b""
