"""Synthetic Type1 programs only; no original fonts are fixtures."""

import sys
from pathlib import Path
import importlib.util
import hashlib
import io

import pytest

if sys.version_info < (3, 11):
    pytest.skip(
        "Pinned fontTools inspection requires Python3.11+", allow_module_level=True
    )

from fontTools import t1Lib
from fontTools.misc import eexec, psLib
from fontTools.misc.psCharStrings import T1CharString
from fontTools.encodings.StandardEncoding import StandardEncoding

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / (name + ".py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TYPE1 = load("type1_identity")
MEDIA = load("media_identity")
ENGINE = load("identity_normalization")
DENIED = "type1-private-fixture@example.invalid"


def program(*, private_dict=False, private_glyph=False, incomplete=False):
    font = t1Lib.T1Font.__new__(t1Lib.T1Font)
    font.encoding = "latin-1"
    subrs = [T1CharString(program=["return"])]
    body = [0, 500, "hsbw"] + ([] if incomplete else ["endchar"])
    chars = {
        name: T1CharString(program=list(body), subrs=subrs)
        for name in (".notdef", DENIED if private_glyph else "A")
    }
    private = {
        "RD": t1Lib.RD_value,
        "ND": t1Lib.ND_values[0],
        "NP": t1Lib.PD_values[0],
        "lenIV": 4,
        "Subrs": subrs,
        "BlueValues": [],
    }
    if private_dict:
        private["SyntheticUnused"] = DENIED
    font.font = {
        "FontName": "SyntheticPublic",
        "FontType": 1,
        "PaintType": 0,
        "FontMatrix": [0.001, 0, 0, 0.001, 0, 0],
        "FontBBox": [0, 0, 500, 500],
        "FontInfo": {"FullName": "SyntheticPublic"},
        "Encoding": StandardEncoding,
        "Private": private,
        "CharStrings": chars,
    }
    data = font.createData()
    cipher = font.encode_eexec({"Private": private, "CharStrings": chars})
    head, marker, rest = data.partition(b"currentfile eexec ")
    assert rest.startswith(cipher)
    return data, [len(head) + len(marker), len(cipher), len(rest) - len(cipher)]


def alter_plain(data, lengths, change):
    a, b, c = lengths
    plain, _ = eexec.decrypt(data[a : a + b], 55665)
    plain = change(plain)
    cipher, _ = eexec.encrypt(plain, 55665)
    return data[:a] + cipher + data[a + b :], [a, len(cipher), c]


def inspect_font(data, lengths, **kwargs):
    out = MEDIA._Extracted()
    before = hashlib.sha256(data).hexdigest()
    evidence = TYPE1.scan_type1(
        data,
        lengths,
        scan_metadata=lambda b: MEDIA._pdf_metadata(b, out),
        emit_text=out.text,
        **kwargs,
    )
    assert hashlib.sha256(data).hexdigest() == before
    found = any(
        ENGINE.scan_private_text(value, [DENIED])["status"] == "fail"
        for value in out.texts
    )
    return found, evidence


def test_complete_type1_reader_binding_and_duplicate_codewords():
    data, lengths = program()
    found, evidence = inspect_font(data, lengths)
    assert not found and evidence["glyph_count"] == 2 and evidence["subr_count"] == 1
    assert evidence["binary_ranges"] == 3


@pytest.mark.parametrize(
    "location",
    ["header", "eexec_comment", "eexec_hex", "unused", "private_dict", "glyph"],
)
def test_raw_and_decrypted_metadata_is_not_hidden_by_type1_role(location):
    data, lengths = program(
        private_dict=location == "private_dict", private_glyph=location == "glyph"
    )
    if location == "header":
        extra = b"% " + DENIED.encode() + b"\n"
        data = extra + data
        lengths[0] += len(extra)
        # Keep the valid Type1 signature first.
        first = data.index(b"\n", len(extra))
        prefix = data[len(extra) : first + 1]
        data = prefix + extra + data[first + 1 :]
    elif location == "eexec_comment":
        data, lengths = alter_plain(
            data, lengths, lambda p: p[:4] + b"% " + DENIED.encode() + b"\n" + p[4:]
        )
    elif location == "eexec_hex":
        data, lengths = alter_plain(
            data,
            lengths,
            lambda p: p[:4]
            + b"/SyntheticUnused <"
            + DENIED.encode().hex().encode()
            + b"> def\n"
            + p[4:],
        )
    elif location == "unused":
        data, lengths = alter_plain(
            data,
            lengths,
            lambda p: p.replace(
                b"currentfile closefile",
                b"% " + DENIED.encode() + b"\ncurrentfile closefile",
            ),
        )
    assert inspect_font(data, lengths)[0]


@pytest.mark.parametrize(
    "attack",
    [
        b"1000000000 array",
        b"1000000000 string",
        b"1000000000 dict",
        b"0 0 100 {} for",
        b"/r {r} def r",
        b"(synthetic) (r) file",
        b"socket",
        b"unknown_unsupported_operator",
    ],
)
def test_untrusted_type1_execution_has_bounded_existing_operators(attack):
    data, lengths = program()
    line = data.index(b"\n") + 1
    extra = attack + b"\n"
    data = data[:line] + extra + data[line:]
    lengths[0] += len(extra)
    original = psLib.PSInterpreter
    with pytest.raises(TYPE1.Type1Error):
        inspect_font(data, lengths)
    assert psLib.PSInterpreter is original


def test_type1_ps_output_is_bounded_and_cannot_escape_to_caller(capsys):
    data, lengths = program()
    line = data.index(b"\n") + 1
    extra = b"0 1 2500 { pop (" + b"x" * 1000 + b") print } for\n"
    data = data[:line] + extra + data[line:]
    lengths[0] += len(extra)
    with pytest.raises(TYPE1.Type1Error):
        inspect_font(data, lengths)
    assert not capsys.readouterr().out


@pytest.mark.parametrize("alter", ["offset", "incomplete", "budget", "length"])
def test_type1_incomplete_or_forged_source_never_gets_exemption(alter):
    data, lengths = program(incomplete=alter == "incomplete")
    kwargs = {}
    if alter == "offset":
        lengths = [lengths[0] + 1, lengths[1] - 1, lengths[2]]
    if alter == "length":
        lengths[1] += 1
    if alter == "budget":
        kwargs["objects"] = 20
    with pytest.raises(TYPE1.Type1Error):
        inspect_font(data, lengths, **kwargs)


def test_type1_dependency_source_drift_rejects(monkeypatch):
    data, lengths = program()
    monkeypatch.setitem(TYPE1.SOURCES, "fontTools.misc.psLib", "0" * 64)
    with pytest.raises(TYPE1.Type1Error, match="dependency"):
        inspect_font(data, lengths)


def test_type1_host_io_guard_restores_real_implementations():
    import builtins

    original = builtins.open
    with TYPE1._no_host_io():
        with pytest.raises(TYPE1.Type1Error, match="external_io"):
            builtins.open("synthetic")
    assert builtins.open is original


def test_type1_large_source_is_stopped_before_existing_tokenizer_allocation():
    with pytest.raises(TYPE1.Type1Error, match="allocation"):
        TYPE1.scan_type1(
            b"a" * 2000001,
            [1, 2000000, 0],
            scan_metadata=lambda *_: pytest.fail("no parse"),
            emit_text=lambda *_: pytest.fail("no parse"),
        )


@pytest.mark.parametrize(
    "attack",
    [
        b"1 array dup -1 7 put pop",
        b"1 array 0 99 getinterval pop",
        b"0 string dup 999999999 65 put pop",
        b"[7] dup -1 get pop pop",
        b"(x) dup -1 get pop pop",
        b"(x) 1 1 getinterval pop",
        b"(x) dup 0 256 put pop",
        b"2 array dup true 7 put pop",
        b"(x) dup false 65 put pop",
        b"(x) 0 [7] putinterval",
    ],
)
def test_postscript_indices_and_intervals_are_not_python_slice_semantics(attack):
    data, lengths = program()
    line = data.index(b"\n") + 1
    extra = attack + b"\n"
    data = data[:line] + extra + data[line:]
    lengths[0] += len(extra)
    with pytest.raises(TYPE1.Type1Error):
        inspect_font(data, lengths)
