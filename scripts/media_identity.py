"""Bounded, offline PDF/MP3/MP4 adapters; identity decisions stay with callbacks."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

DATA_LIMIT = 64_000_000
TEXT_LIMIT = 2_000_000
IMAGE_LIMIT = 32_000_000
OUTPUT_LIMIT = 48_000_000
OBJECT_LIMIT = 100_000
DECODE_TIMEOUT = 20
EXTRACT_TIMEOUT = 60


class MediaError(ValueError):
    """Only fixed diagnostic codes cross the extraction boundary."""


def _stop(process, group):
    if os.name == "posix" and group:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        if os.name == "nt" and group:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    process.wait(timeout=5)


def _run(arguments, *, timeout=DECODE_TIMEOUT, limit=OUTPUT_LIMIT, group=False):
    # No shell, credentials, user configuration, or inherited Python paths.
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
        if key in os.environ
    }
    env["LC_ALL"] = "C"
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=env,
            start_new_session=group and os.name == "posix",
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if group and os.name == "nt" else 0
            ),
        )
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise MediaError("timeout")
                if (
                    os.fstat(stdout.fileno()).st_size
                    + os.fstat(stderr.fileno()).st_size
                    > limit
                ):
                    raise MediaError("output_limit")
                time.sleep(0.02)
        except BaseException:
            _stop(process, group)
            raise
        if (
            os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
            > limit
        ):
            raise MediaError("output_limit")
        stdout.seek(0)
        stderr.seek(0)
        return process.returncode, stdout.read(limit + 1), stderr.read(limit + 1)


class _Extracted:
    def __init__(self):
        self.texts = set()
        self.pdf_byte_texts = {}
        self.images = {}
        self.text_bytes = self.image_bytes = 0
        self.versions = {}
        self.pdf_known_free_references = 0
        self.font_checksum_mismatches = 0
        self.type1_metrics = []
        self.mp4_diagnostics = []

    def text(self, value):
        if not isinstance(value, str):
            raise MediaError("unsupported_metadata")
        if not value or value in self.texts:
            return
        self.text_bytes += len(value.encode("utf-8"))
        if (
            self.text_bytes > TEXT_LIMIT
            or len(self.texts) + len(self.pdf_byte_texts) >= OBJECT_LIMIT
        ):
            raise MediaError("text_limit")
        self.texts.add(value)

    def pdf_bytes(self, value):
        if len(value) > TEXT_LIMIT:
            raise MediaError("text_limit")
        digest = hashlib.sha256(value).hexdigest()
        if not value or digest in self.pdf_byte_texts:
            return
        # Reserve the largest UTF-8 view, not their sum: the parent checks them
        # sequentially instead of retaining six documents. The parent recomputes
        # this bound and still enforces the same TEXT_LIMIT and item/time limits.
        self.text_bytes += _pdf_source_weight(value)
        if (
            self.text_bytes > TEXT_LIMIT
            or len(self.texts) + len(self.pdf_byte_texts) >= OBJECT_LIMIT
        ):
            raise MediaError("text_limit")
        self.pdf_byte_texts[digest] = base64.b64encode(value).decode("ascii")

    def image(self, value):
        digest = hashlib.sha256(value).hexdigest()
        if digest in self.images:
            return
        self.image_bytes += len(value)
        if self.image_bytes > IMAGE_LIMIT or len(self.images) >= 256:
            raise MediaError("image_limit")
        if not (
            value.startswith(b"\x89PNG\r\n\x1a\n") or value.startswith(b"\xff\xd8\xff")
        ):
            raise MediaError("unsupported_image")
        self.images[digest] = base64.b64encode(value).decode("ascii")

    def plain(self, value):
        if isinstance(value, str):
            self.text(value)
        elif isinstance(value, (bool, int, float)):
            self.text(str(value))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.plain(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                self.text(str(key))
                self.plain(item)
        elif (
            type(value).__module__.startswith("mutagen.id3")
            and type(value).__name__ == "ID3TimeStamp"
        ):
            self.text(str(value))
        else:
            raise MediaError("unsupported_metadata")

    def packet(self):
        return {
            "texts": sorted(self.texts),
            "pdf_byte_texts": [
                self.pdf_byte_texts[key] for key in sorted(self.pdf_byte_texts)
            ],
            "images": list(self.images.values()),
            "versions": self.versions,
            "pdf_known_free_references": self.pdf_known_free_references,
            "font_checksum_mismatches": self.font_checksum_mismatches,
            "type1_metrics": self.type1_metrics,
            "mp4_diagnostics": self.mp4_diagnostics,
        }


def _verify_font_reader_sources():
    import fontTools.ttLib.ttFont as tt_source
    import fontTools.ttLib.sfnt as sfnt_source

    # The official 4.67.0 Windows wheel uses CRLF in these Python sources.
    # Accept only the two verified raw artifacts, not arbitrary normalization.
    for module, expected in (
        (
            tt_source,
            {
                "01bed50be567307a84cd4f0c19e12514e746689501f6f7aacddc04edeeb59079",
                "e8a849a433548185466e5cd18b945a479553379654992af526703850dc651605",
            },
        ),
        (
            sfnt_source,
            {
                "adb2adf08781868769f14bcff10ec6cd3772f6fb80d4d316c4404bcc8653a4d9",
                "0c3dbc294906a37243ff084326b83e4af7ba3089638e5477275203f138d1b5ca",
            },
        ),
    ):
        if (
            hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            not in expected
        ):
            raise MediaError("dependency_unavailable")


def _cff_metadata(font, output):
    """CFF Name/String INDEX entries remain metadata even when unused."""
    names = font.fontNames
    strings = font.strings.getStrings() if font.strings is not None else []
    if len(names) + len(strings) > OBJECT_LIMIT:
        raise MediaError("object_limit")
    for value in names:
        output.text(value)
    for value in strings:
        output.text(value)


def _font(data, subtype, output, lengths=None):
    from fontTools import version
    from fontTools.cffLib import CFFFontSet
    from fontTools.ttLib import TTFont

    output.versions["fonttools"] = version
    if version != "4.67.0":
        raise MediaError("dependency_unavailable")
    _verify_font_reader_sources()
    if subtype == "/FontFile":
        import types

        source = Path(__file__).with_name("type1_identity.py")
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        previous = output.versions.get("type1_adapter_sha256", digest)
        if previous != digest:
            raise MediaError("adapter_changed")
        adapter = types.ModuleType("_bundled_type1_adapter")
        adapter.__file__ = str(source)
        exec(compile(raw, str(source), "exec"), adapter.__dict__)
        try:
            result = adapter.scan_type1(
                data,
                lengths,
                scan_metadata=lambda value: _pdf_metadata(value, output),
                emit_text=output.text,
                objects=OBJECT_LIMIT,
                bytes_limit=IMAGE_LIMIT,
                seconds=DECODE_TIMEOUT,
            )
        except adapter.Type1Error as error:
            reason = str(error)
            mapping = {
                "dependency": "dependency_unavailable",
                "allocation": "decoded_limit",
                "budget": "object_limit",
                "timeout": "timeout",
                "stack": "object_limit",
                "depth": "object_limit",
                "output": "text_limit",
                "text_limit": "text_limit",
                "image_limit": "image_limit",
                "decoded_limit": "decoded_limit",
                "object_limit": "object_limit",
                "unsupported_region": "unsupported_region",
            }
            raise MediaError(mapping.get(reason, "unsupported_font")) from None
        if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise MediaError("adapter_changed")
        result["source_sha256"] = hashlib.sha256(data).hexdigest()
        output.type1_metrics.append(result)
        output.versions["type1_adapter_sha256"] = digest
    elif subtype == "/FontFile2" or data[:4] in {b"OTTO", b"\x00\x01\x00\x00", b"true"}:
        font = TTFont(
            io.BytesIO(data), lazy=False, checkChecksums=1, ignoreDecompileErrors=False
        )
        try:
            for tag in font.keys():
                if tag in {"SVG ", "sbix", "CBDT", "CBLC", "EBDT", "EBLC", "DSIG"}:
                    raise MediaError("unsupported_font_table")
                table = font[tag]
                if type(table).__name__ == "DefaultTable":
                    raise MediaError("unsupported_font_table")
            for tag in ("CFF ", "CFF2"):
                if tag in font:
                    _cff_metadata(font[tag].cff, output)
            from fontTools.pens.basePen import NullPen

            glyphs = font.getGlyphSet()
            if len(glyphs) > 100_000:
                raise MediaError("object_limit")
            for name in glyphs.keys():
                output.text(name)
                glyphs[name].draw(NullPen())
            for tag in ("fpgm", "prep"):
                if tag in font:
                    font[tag].program.getAssembly()
            if "name" in font:
                for name in font["name"].names:
                    output.text(name.toUnicode(errors="strict"))
            if "meta" in font:
                for value in font["meta"].data.values():
                    output.text(
                        value.decode("utf-8") if isinstance(value, bytes) else value
                    )
        finally:
            font.close()
    elif subtype == "/Type1C" or subtype == "/CIDFontType0C":
        font = CFFFontSet()
        font.decompile(io.BytesIO(data), None, isCFF2=False)
        _cff_metadata(font, output)
        glyph_count = 0
        for name in font.fontNames:
            output.text(name)
            top = font[name]
            for value in top.rawDict.values():
                if isinstance(value, str):
                    output.text(value)
            glyph_count += len(top.CharStrings.charStrings)
            if glyph_count > OBJECT_LIMIT:
                raise MediaError("object_limit")
            for glyph_name in top.CharStrings.keys():
                output.text(glyph_name)
                top.CharStrings[glyph_name].decompile()
    else:
        raise MediaError("unsupported_font")


PDF_IMPLEMENTATIONS = {
    "pypdf._reader": "0f65aa59c8b2dc89b67a07a076de21ccab56a7a385d52220f260b5fce366b066",
    "pypdf.generic._data_structures": "68e58bac82fde745fbc3fdb4f84dd1a67b4cd819065499c5b4d9d41eef781841",
    "pypdf.generic._utils": "33c8bde72a42e65d9eef5d74f157aaf74bdb6d4b65427beefbe256fc0b32c793",
    "pdfminer.psparser": "169247c80a69cee45f7d132e60944e8488944d2633524bfd2a97f915eefc08bf",
}


def _pdf_dependencies(output):
    if sys.version_info < (3, 10):
        raise MediaError("dependency_unavailable")
    import importlib
    import pypdf
    import pdfminer

    if pypdf.__version__ != "6.21.2" or pdfminer.__version__ != "20260107":
        raise MediaError("dependency_unavailable")
    for name, expected in PDF_IMPLEMENTATIONS.items():
        module = importlib.import_module(name)
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
            raise MediaError("dependency_unavailable")
    output.versions.update(pypdf=pypdf.__version__, pdfminer=pdfminer.__version__)


def _byte_text_views(data):
    for codec in ("utf-8", "latin-1", "utf-16-le", "utf-16-be"):
        for offset in ((0, 1) if codec.startswith("utf-16") else (0,)):
            yield data[offset:].decode(codec, errors="replace")


def _pdf_source_weight(data):
    return max(len(value.encode("utf-8")) for value in _byte_text_views(data))


def _pdf_text_bytes(data, output, *, compact=False):
    if compact:
        output.pdf_bytes(data)
    else:
        for value in _byte_text_views(data):
            output.text(value)


def _pdf_xml_metadata(data, output):
    """Inspect raw XML and decoded values with bounded, DTD-free Expat events."""
    from xml.parsers import expat

    if len(data) > TEXT_LIMIT:
        raise MediaError("text_limit")
    output.text(
        data.decode(
            "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
        )
    )
    parser = expat.ParserCreate(namespace_separator="}")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    deadline = time.monotonic() + DECODE_TIMEOUT
    depth = events = decoded_bytes = 0
    text = []
    value_fragments = []
    subtree_starts = []

    def tick():
        nonlocal events
        events += 1
        if events > OBJECT_LIMIT:
            raise MediaError("object_limit")
        if time.monotonic() > deadline:
            raise MediaError("timeout")

    def refuse(*args):
        raise MediaError("unsupported_metadata")

    def flush_value():
        if value_fragments:
            output.text("".join(value_fragments))
            value_fragments.clear()

    def start(name, attributes):
        nonlocal depth
        tick()
        flush_value()
        subtree_starts.append(len(text))
        depth += 1
        if depth > 64:
            raise MediaError("object_limit")
        output.text(name)
        for key, value in attributes.items():
            tick()
            output.text(key)
            output.text(value)

    def end(name):
        nonlocal depth
        tick()
        flush_value()
        output.text("".join(text[subtree_starts.pop() :]))
        depth -= 1

    def characters(value):
        nonlocal decoded_bytes
        tick()
        decoded_bytes += len(value.encode("utf-8"))
        if decoded_bytes > TEXT_LIMIT:
            raise MediaError("text_limit")
        text.append(value)
        value_fragments.append(value)

    def namespace(prefix, uri):
        tick()
        output.text(prefix or "")
        output.text(uri or "")

    parser.StartNamespaceDeclHandler = namespace
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SkippedEntityHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    # Reconstruct each element text/tail value across entity/CDATA callbacks.
    # Start/end events delimit values; subtree/whole aggregates are extra views.
    try:
        parser.Parse(data, True)
    except expat.ExpatError:
        raise MediaError("unsupported_metadata") from None
    flush_value()
    output.text("".join(text))


PDF_CONTENT_OPERATORS = frozenset(
    b"""w J j M d ri i gs q Q cm m l c v y h re S s f F f* B B* b b* n
    W W* BT ET Tc Tw Tz TL Tf Tr Ts Td TD Tm T* Tj TJ ' " d0 d1 CS cs
    SC SCN sc scn G g RG rg K k sh Do MP DP BMC BDC EMC BX EX""".split()
)
PDF_NUMBER_TOKEN = re.compile(
    rb"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?=[\x00\t\n\f\r ()<>\[\]{}/%]|$)"
)
PDF_WHITESPACE = b"\x00\t\n\f\r "


def _pdf_metadata(
    data, output, *, inspect_raw=True, content_syntax=False, compact_bytes=False
):
    """Raw comments plus existing lexer positions and authoritative PDF primitives."""
    from pdfminer.psparser import PSBaseParser, PSEOF, PSKeyword, PSLiteral
    from pypdf.generic import NameObject
    from pypdf.generic._utils import (
        read_hex_string_from_stream,
        read_string_from_stream,
    )

    if len(data) > TEXT_LIMIT:
        raise MediaError("text_limit")
    # NUL-separated unknown bytes can be UTF-16, not drawing operators.
    # Keep the complete source in that ambiguous case.
    content_syntax = content_syntax and b"\x00" not in data
    if inspect_raw and not content_syntax:
        _pdf_text_bytes(data, output, compact=compact_bytes)
    raw_parts, raw_cursor = [], 0

    def omit_syntax(start, end):
        nonlocal raw_cursor
        while start > raw_cursor and data[start - 1] in PDF_WHITESPACE:
            start -= 1
        if start > raw_cursor:
            raw_parts.extend((data[raw_cursor:start], b" "))
        raw_cursor = end
        while raw_cursor < len(data) and data[raw_cursor] in PDF_WHITESPACE:
            raw_cursor += 1

    deadline = time.monotonic() + DECODE_TIMEOUT

    class LimitedParser(PSBaseParser):
        def fillbuf(self):
            if len(self._curtoken) > 262144:
                raise MediaError("text_limit")
            if time.monotonic() > deadline:
                raise MediaError("timeout")
            return super().fillbuf()

    lexer = LimitedParser(io.BytesIO(data))
    count = 0
    while True:
        try:
            position, token = lexer.nexttoken()
        except PSEOF:
            if lexer._parse1.__name__ not in {"_parse_main", "_parse_comment"}:
                raise MediaError("unverified")
            if inspect_raw and content_syntax:
                _pdf_text_bytes(
                    b"".join(raw_parts) + data[raw_cursor:],
                    output,
                    compact=compact_bytes,
                )
            return
        count += 1
        if count > OBJECT_LIMIT or time.monotonic() > deadline:
            raise MediaError("object_limit")
        # ISO 32000-1 7.3.3 / Table 51: coordinates and fixed operators are
        # syntax, not UTF-16 prose. Keep comments, names, strings and unknown
        # tokens verbatim; their decoded values are also inspected below.
        end = None
        if content_syntax and type(token) in (int, float):
            numeric = PDF_NUMBER_TOKEN.match(data, position)
            if numeric is not None:
                end = numeric.end()
        elif content_syntax and isinstance(token, PSKeyword):
            if (
                token.name in PDF_CONTENT_OPERATORS
                and data[position : position + len(token.name)] == token.name
            ):
                end = position + len(token.name)
        if end is not None:
            omit_syntax(position, end)
            continue
        stream = io.BytesIO(data)
        stream.seek(position)
        if isinstance(token, PSLiteral):
            value = NameObject.read_from_stream(stream, None)
        elif isinstance(token, bytes):
            # nexttoken supplies only the position. Its odd/invalid hex values
            # are not authoritative PDF values, including in free objects.
            if data[position : position + 1] == b"(":
                value = read_string_from_stream(stream)
            elif data[position : position + 1] == b"<":
                value = read_hex_string_from_stream(stream)
            else:
                raise MediaError("unverified")
        else:
            continue
        if isinstance(value, bytes):
            _pdf_text_bytes(value, output, compact=compact_bytes)
        else:
            output.text(str(value))
            original = getattr(value, "original_bytes", None)
            if original is not None:
                _pdf_text_bytes(original, output, compact=compact_bytes)
        if content_syntax and isinstance(token, bytes):
            # String source syntax (hex digits / octal escapes) is not another
            # character encoding. All views of its authoritative decoded bytes
            # were inspected above; preserve source comments and unknowns only.
            omit_syntax(position, stream.tell())


class _PDFSource:
    """Bind original stream reads to object identities in the isolated child only."""

    def __init__(self, data):
        self.data = data
        self.pending = {}
        self.bound = {}
        self.read_count = 0
        self.deadline = time.monotonic() + EXTRACT_TIMEOUT
        self.stream = None

    def __enter__(self):
        from pypdf.generic import DictionaryObject, StreamObject

        original_read = DictionaryObject._read_stream_data
        original_init = StreamObject.initialize_from_dictionary
        self.original_read, self.original_init = original_read, original_init
        owner = self

        class Observed(io.BytesIO):
            def read(self, size=-1):
                start = self.tell()
                result = super().read(size)
                owner.read_count += 1
                if owner.read_count > 2000000 or time.monotonic() > owner.deadline:
                    raise MediaError("object_limit")
                frame = sys._getframe(1)
                if (
                    frame.f_code is original_read.__code__
                    and frame.f_locals.get("pstart") == start
                    and frame.f_locals.get("length") == size
                    and size >= 0
                ):
                    data = frame.f_locals["data"]
                    owner.pending.setdefault(
                        id(data), {"dictionary": data, "reads": []}
                    )["reads"].append((start, result))
                return result

        self.stream = Observed(self.data)

        def read_wrapper(*, stream, pdf, data):
            original_read(stream=stream, pdf=pdf, data=data)
            if "__streamdata__" not in data:
                return
            if stream is not owner.stream:
                raise MediaError("unsupported_region")
            record = owner.pending.get(id(data))
            if record is None or len(record["reads"]) != 1:
                raise MediaError("unsupported_region")
            start, actual = record["reads"][0]
            raw = data["__streamdata__"]
            if actual != raw or memoryview(owner.data)[start : start + len(raw)] != raw:
                raise MediaError("unsupported_region")
            record["range"] = (start, start + len(raw))
            record["raw"] = raw

        def init_wrapper(data):
            record = owner.pending.get(id(data))
            obj = original_init(data)
            if record is None or "range" not in record or obj._data != record["raw"]:
                raise MediaError("unsupported_region")
            owner.bound[id(obj)] = {"object": obj, **record}
            return obj

        DictionaryObject._read_stream_data = staticmethod(read_wrapper)
        StreamObject.initialize_from_dictionary = staticmethod(init_wrapper)
        return self

    def __exit__(self, *args):
        from pypdf.generic import DictionaryObject, StreamObject

        DictionaryObject._read_stream_data = staticmethod(self.original_read)
        StreamObject.initialize_from_dictionary = staticmethod(self.original_init)
        self.stream.close()

    def inspect_remaining(self, streams, output, *, text_streams=()):
        all_ranges, compressed_ranges = [], []
        compression_roles = {}
        for obj in streams:
            record = self.bound.get(id(obj))
            if (
                record is None
                or record["object"] is not obj
                or obj._data != record["raw"]
            ):
                raise MediaError("unsupported_region")
            start, end = record["range"]
            if record["reads"] != [(start, record["raw"])]:
                raise MediaError("unsupported_region")
            if (
                not 0 <= start <= end <= len(self.data)
                or memoryview(self.data)[start:end] != obj._data
            ):
                raise MediaError("unsupported_region")
            all_ranges.append((start, end))
            compression_roles[(start, end)] = compression_roles.get(
                (start, end), True
            ) and (obj.get("/Filter") is not None or id(obj) in text_streams)
        all_ranges = sorted(set(all_ranges))
        compressed_ranges = [
            span for span, compressed in compression_roles.items() if compressed
        ]
        all_ranges.sort()
        cursor = 0
        for start, end in all_ranges:
            if start < cursor:
                raise MediaError("unsupported_region")
            cursor = end
        # Every unclassified noncompressed byte still receives literal/path
        # checking, even for a binary font. Fully inspected content streams
        # already supplied their textual projection; do not decode their
        # numeric drawing syntax again. PDF syntax is applicable outside
        # streams and to decoded text-content streams, not to binary glyph data.
        # Padding is synthetic, not source text. One separator preserves the
        # boundary without charging every compressed byte to each text codec.
        raw, cursor = bytearray(), 0
        for start, end in sorted(compressed_ranges):
            raw.extend(self.data[cursor:start])
            raw.extend(b" ")
            cursor = end
        raw.extend(self.data[cursor:])
        _pdf_text_bytes(bytes(raw), output, compact=True)
        syntax = bytearray(self.data)
        for start, end in all_ranges:
            syntax[start:end] = b" " * (end - start)
        _pdf_metadata(bytes(syntax), output, inspect_raw=False, compact_bytes=True)


def _pdf_xref_shape(stream):
    widths = stream.get("/W")
    size = stream.get("/Size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= OBJECT_LIMIT
        or not isinstance(widths, (list, tuple))
        or len(widths) != 3
        or any(
            not isinstance(n, int) or isinstance(n, bool) or not 0 <= n <= 8
            for n in widths
        )
        or not sum(widths)
    ):
        raise MediaError("unsupported_region")
    pairs = stream.get("/Index", [0, size])
    if (
        not isinstance(pairs, (list, tuple))
        or not pairs
        or len(pairs) % 2
        or len(pairs) > OBJECT_LIMIT
        or any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in pairs)
    ):
        raise MediaError("unsupported_region")
    cursor = count = 0
    for first, length in zip(pairs[::2], pairs[1::2]):
        if first < cursor or not length or first + length > size:
            raise MediaError("unsupported_region")
        cursor = first + length
        count += length
    if count > OBJECT_LIMIT or len(stream.get_data()) != sum(widths) * count:
        raise MediaError("unsupported_region")


def _pdf_reader(stream, selected=None, xref_budget=None):
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject, NullObject

    if xref_budget is None:
        xref_budget = {"decoded": 0, "entries": 0}

    class StrictKnownFreeReader(PdfReader):
        def __init__(self, source):
            self.known_free_references = 0
            self.xref_offsets = []
            self.xref_edges = []
            super().__init__(source, strict=True)

        def _find_startxref_pos(self, source):
            actual = super()._find_startxref_pos(source)
            value = actual if selected is None else selected
            if type(value) is not int or not 0 < value < len(source.getbuffer()):
                raise MediaError("unsupported_region")
            return value

        def _observe_xref(self, source):
            offset = source.tell() - 1
            if (
                not 0 < offset < len(source.getbuffer())
                or offset in self.xref_offsets
                or len(self.xref_offsets) >= 64
            ):
                raise MediaError("unsupported_region")
            self.xref_offsets.append(offset)
            return offset

        def _edge(self, first, last, source):
            if last is not None:
                if (
                    not isinstance(last, int)
                    or isinstance(last, bool)
                    or not 0 < last < len(source.getbuffer())
                ):
                    raise MediaError("unsupported_region")
                self.xref_edges.append((first, int(last)))

        def _read_xref(self, source):
            offset = self._observe_xref(source)
            before = len(self.xref_offsets)
            previous = super()._read_xref(source)
            for companion in self.xref_offsets[before:]:
                self._edge(offset, companion, source)
            self._edge(offset, previous, source)
            return previous

        def _read_pdf15_xref_stream(self, source):
            offset = self._observe_xref(source)
            obj = super()._read_pdf15_xref_stream(source)
            self._edge(offset, obj.get("/Prev"), source)
            return obj

        def _sanitize_pdf15_xref_stream_index_pairs(
            self, *, index_pairs, entry_sizes, xref_stream
        ):
            _pdf_xref_shape(xref_stream)
            xref_budget["decoded"] += len(xref_stream.get_data())
            xref_budget["entries"] += sum(index_pairs[1::2])
            if xref_budget["decoded"] > DATA_LIMIT:
                raise MediaError("decoded_limit")
            if xref_budget["entries"] > OBJECT_LIMIT:
                raise MediaError("object_limit")
            return super()._sanitize_pdf15_xref_stream_index_pairs(
                index_pairs=index_pairs,
                entry_sizes=entry_sizes,
                xref_stream=xref_stream,
            )

        def _rebuild_xref_table(self, source):
            raise MediaError("unsupported_region")

        def _read_xref_other_error(self, source, startxref):
            raise MediaError("unsupported_region")

        def get_object(self, reference):
            if isinstance(reference, IndirectObject) and reference.pdf is self:
                number, generation = reference.idnum, reference.generation
                if self.xref_free_entry.get(generation, {}).get(number) is True:
                    if (
                        number in self.xref.get(generation, {})
                        or number in self.xref_objStm
                    ):
                        raise MediaError("unverified")
                    self.known_free_references += 1
                    return NullObject()
            return super().get_object(reference)

    return StrictKnownFreeReader(stream)


def _pdf_destination(reader, value):
    from pypdf.generic import ArrayObject, Destination

    if value is None:
        raise MediaError("unsupported_active_or_attachment")
    value = value.get_object() if hasattr(value, "get_object") else value
    if isinstance(value, str):
        destination = reader.named_destinations.get(value)
    elif isinstance(value, ArrayObject):
        destination = reader._build_destination("opening view", value)
    else:
        raise MediaError("unsupported_active_or_attachment")
    if not isinstance(destination, Destination):
        raise MediaError("unsupported_active_or_attachment")
    number = reader.get_destination_page_number(destination)
    if number is None or not 0 <= number < len(reader.pages):
        raise MediaError("unsupported_active_or_attachment")


def _pdf(path, output):
    _pdf_dependencies(output)
    with _PDFSource(path.read_bytes()) as source:
        _pdf_contents(source, output)


def _pdf_contents(source, output):
    import logging
    import pypdf
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        ContentStream,
        DictionaryObject,
        IndirectObject,
        NameObject,
        StreamObject,
        TextStringObject,
    )

    output.versions["pypdf"] = pypdf.__version__
    pypdf.filters.ZLIB_MAX_OUTPUT_LENGTH = DATA_LIMIT
    diagnostics = []

    class Diagnostic(logging.Handler):
        def emit(self, record):
            if (
                record.name == "fontTools.ttLib.sfnt"
                and record.msg == "bad checksum for '%s' table"
                and isinstance(record.args, tuple)
                and len(record.args) == 1
                and isinstance(record.args[0], str)
                and len(record.args[0]) == 4
            ):
                output.font_checksum_mismatches += 1
            else:
                diagnostics.append(record.levelno)

    logger = logging.getLogger()
    handler = Diagnostic(logging.WARNING)
    logger.addHandler(handler)
    try:
        raw = source.data
        if not raw.startswith(b"%PDF-") or not raw.rstrip().endswith(b"%%EOF"):
            raise MediaError("unverified")
        xref_budget = {"decoded": 0, "entries": 0}
        current = _pdf_reader(source.stream, xref_budget=xref_budget)
        if "/Root" not in current.trailer:
            raise MediaError("unverified")
        readers, offsets, edges = [current], set(), set()
        cursor = 0
        while cursor < len(readers):
            reader = readers[cursor]
            cursor += 1
            offsets.update(reader.xref_offsets)
            edges.update(reader.xref_edges)
            if len(offsets) > 64:
                raise MediaError("object_limit")
            selected = {item._startxref for item in readers}
            for offset in sorted(offsets - selected):
                earlier = _pdf_reader(source.stream, offset, xref_budget=xref_budget)
                if earlier._startxref != offset or earlier.xref_offsets[0] != offset:
                    raise MediaError("unsupported_region")
                readers.append(earlier)
        adjacency = {offset: set() for offset in offsets}
        for first, last in edges:
            if first not in offsets or last not in offsets:
                raise MediaError("unsupported_region")
            adjacency[first].add(last)
        visiting, visited = set(), set()

        def visit(offset):
            if offset in visiting:
                raise MediaError("unsupported_region")
            if offset in visited:
                return
            visiting.add(offset)
            for child in adjacency[offset]:
                visit(child)
            visiting.remove(offset)
            visited.add(offset)

        visit(current._startxref)
        if visited != offsets:
            raise MediaError("unsupported_region")
        budget = {"objects": 0, "visited": 0, "decoded": 0, "pages": 0}
        streams, text_streams = [], set()
        for reader in readers:
            streams.extend(_pdf_context(reader, output, budget, text_streams))
        source.inspect_remaining(streams, output, text_streams=text_streams)
        if diagnostics:
            raise MediaError("unverified")
    finally:
        logger.removeHandler(handler)


def _pdf_validate_stream_encoding(stream, filters):
    """Bind each admitted filter to all original bytes before pypdf decodes it."""
    data = stream._data
    if filters == ["/ASCII85Decode", "/FlateDecode"]:
        # PDF ASCII85 uses a mandatory terminator and no PostScript opener.
        # Only PDF whitespace may be ignored; trailing bytes cannot disappear.
        compact = data.translate(None, b"\x00\t\n\x0c\r ")
        if not compact.endswith(b"~>") or compact.startswith(b"<~"):
            raise MediaError("unsupported_region")
        bound = group_length = 0
        for index in range(len(compact) - 2):
            value = compact[index]
            if value == 122:  # z abbreviates one complete zero-valued group.
                if group_length:
                    raise MediaError("unsupported_region")
                bound += 4
            elif 33 <= value <= 117:
                group_length += 1
                if group_length == 5:
                    bound += 4
                    group_length = 0
            else:
                raise MediaError("unsupported_region")
            if bound > DATA_LIMIT:
                raise MediaError("decoded_limit")
        if group_length == 1:
            raise MediaError("unsupported_region")
        bound += max(0, group_length - 1)
        if bound > DATA_LIMIT:
            raise MediaError("decoded_limit")
        try:
            data = base64.a85decode(compact, adobe=True, ignorechars=b"")
        except ValueError as exc:
            raise MediaError("unsupported_region") from exc
        filters = ["/FlateDecode"]
    if filters == ["/FlateDecode"]:
        import zlib

        decoder = zlib.decompressobj()
        expanded = decoder.decompress(data, DATA_LIMIT + 1)
        if len(expanded) > DATA_LIMIT or decoder.unconsumed_tail:
            raise MediaError("decoded_limit")
        if not decoder.eof or decoder.unused_data:
            raise MediaError("unsupported_region")
    elif filters not in ([None], ["/DCTDecode"]):
        raise MediaError("unsupported_stream")


def _icc_profile(data, output, components):
    """Inspect the bounded ICC v4 matrix subset, including every locale.

    ICC.1:2022 sections 7, 10.15, 10.18, 10.22 and 10.31. Unknown tags,
    overlapping payloads and nonzero unclaimed bytes never gain an exemption.
    Numeric colour values are structural data, not character encodings.
    """
    import struct

    def u32(offset):
        if offset < 0 or offset + 4 > len(data):
            raise MediaError("unsupported_stream")
        return struct.unpack_from(">I", data, offset)[0]

    if (
        len(data) < 132
        or len(data) > DATA_LIMIT
        or u32(0) != len(data)
        or data[36:40] != b"acsp"
        or data[8] != 4
        or any(data[10:12])
        or any(data[100:128])
        or {b"GRAY": 1, b"RGB ": 3, b"CMYK": 4}.get(data[16:20]) != components
    ):
        raise MediaError("unsupported_stream")
    # Fixed header signatures may carry text; profile ID and numeric header
    # fields retain their specified binary meaning.
    for start in (4, 12, 16, 20, 36, 40, 48, 52, 80):
        output.text(data[start : start + 4].decode("ascii", errors="strict"))
    count = u32(128)
    table_end = 132 + 12 * count
    if not count or count > OBJECT_LIMIT or table_end > len(data):
        raise MediaError("unsupported_stream")
    signatures, ranges, unique = set(), [], {}
    allowed = {
        b"desc": b"mluc",
        b"cprt": b"mluc",
        b"wtpt": b"XYZ ",
        b"rXYZ": b"XYZ ",
        b"gXYZ": b"XYZ ",
        b"bXYZ": b"XYZ ",
        b"chad": b"sf32",
        b"rTRC": b"para",
        b"gTRC": b"para",
        b"bTRC": b"para",
    }
    for index in range(count):
        entry = 132 + 12 * index
        signature, start, size = struct.unpack_from(">4sII", data, entry)
        end = start + size
        if (
            signature in signatures
            or signature not in allowed
            or start < table_end
            or start % 4
            or size < 8
            or end > len(data)
            or data[start : start + 4] != allowed[signature]
            or any(data[start + 4 : start + 8])
        ):
            raise MediaError("unsupported_stream")
        signatures.add(signature)
        for first, last in ranges:
            if start < last and first < end and (first, last) != (start, end):
                raise MediaError("unsupported_stream")
        ranges.append((start, end))
        unique[(start, end)] = allowed[signature]
    _covered(data, ranges, start=table_end)
    for (start, end), kind in unique.items():
        size = end - start
        if kind == b"mluc":
            if size < 16:
                raise MediaError("unsupported_stream")
            records, record_size = u32(start + 8), u32(start + 12)
            header = 16 + 12 * records
            if (
                not records
                or records > OBJECT_LIMIT
                or record_size != 12
                or header > size
            ):
                raise MediaError("unsupported_stream")
            strings = set()
            for index in range(records):
                entry = start + 16 + 12 * index
                locale = data[entry : entry + 4]
                length, offset = u32(entry + 4), u32(entry + 8)
                if (
                    not all(65 <= c <= 90 or 97 <= c <= 122 for c in locale)
                    or offset < header
                    or length % 2
                    or offset % 2
                    or offset + length > size
                ):
                    raise MediaError("unsupported_stream")
                first, last = start + offset, start + offset + length
                output.text(locale.decode("ascii"))
                strings.add((first, last))
            strings = sorted(strings)
            for previous, current in zip(strings, strings[1:]):
                if previous[1] > current[0]:
                    raise MediaError("unsupported_stream")
            for first, last in strings:
                output.text(data[first:last].decode("utf-16-be", errors="strict"))
            _covered(data, strings, start=start + header, end=end)
        elif kind == b"para":
            if size < 12:
                raise MediaError("unsupported_stream")
            function, reserved = struct.unpack_from(">HH", data, start + 8)
            parameters = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}.get(function)
            if reserved or parameters is None or size != 12 + 4 * parameters:
                raise MediaError("unsupported_stream")
        elif size != {b"XYZ ": 20, b"sf32": 44}[kind]:
            raise MediaError("unsupported_stream")


def _pdf_context(reader, output, budget, text_streams=None):
    from pypdf.generic import (
        ArrayObject,
        ByteStringObject,
        ContentStream,
        DictionaryObject,
        IndirectObject,
        NameObject,
        StreamObject,
        TextStringObject,
    )

    if text_streams is None:
        text_streams = set()

    if reader.is_encrypted:
        raise MediaError("encrypted")
    complete = "/Root" in reader.trailer
    pages = reader.pages if complete else []
    budget["pages"] += len(pages)
    if (complete and not pages) or budget["pages"] > 1000:
        raise MediaError("page_limit")
    open_action = reader.root_object.get("/OpenAction") if complete else None
    if open_action is not None:
        from pypdf.generic import Destination

        value = open_action.get_object()
        if isinstance(value, DictionaryObject):
            if value.get("/S") != "/GoTo":
                raise MediaError("unsupported_active_or_attachment")
            _pdf_destination(reader, value.get("/D"))
        else:
            destination = reader.open_destination
            if isinstance(destination, str):
                destination = reader.named_destinations.get(destination)
            if not isinstance(destination, Destination):
                raise MediaError("unsupported_active_or_attachment")
            number = reader.get_destination_page_number(destination)
            if number is None or not 0 <= number < len(reader.pages):
                raise MediaError("unsupported_active_or_attachment")
    references = {
        (number, generation)
        for generation, numbers in reader.xref.items()
        for number in numbers
        if generation != 65535 and number
    }
    references.update((number, 0) for number in reader.xref_objStm)
    budget["objects"] += len(references)
    if budget["objects"] > OBJECT_LIMIT:
        raise MediaError("object_limit")
    objects = [
        reader.get_object(IndirectObject(n, g, reader)) for n, g in sorted(references)
    ]
    if not complete:
        # A linked partial xref is not a separate complete document. The
        # existing PageObject adapter can still inspect any actual page object.
        from pypdf import PageObject

        for obj in objects:
            if isinstance(obj, DictionaryObject) and obj.get("/Type") == "/Page":
                page = PageObject(reader, obj.indirect_reference)
                page.update(obj)
                pages.append(page)
        budget["pages"] += len(pages)
        if budget["pages"] > 1000:
            raise MediaError("page_limit")
    stack = [reader.trailer, *objects, *reader.resolved_objects.values()]
    seen, streams, fonts = set(), [], {}
    icc_streams, cid_sets = set(), set()
    dictionaries, action_entries, cmap_objects = [], [], set()
    glyph_dictionaries, glyph_streams, forbidden_dictionaries = set(), set(), []
    forbidden = {
        "/JS",
        "/JavaScript",
        "/AA",
        "/EmbeddedFiles",
        "/EF",
        "/RichMediaContent",
        "/XFA",
    }
    while stack:
        obj = stack.pop()
        if isinstance(obj, IndirectObject):
            obj = obj.get_object()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        budget["visited"] += 1
        if budget["visited"] > OBJECT_LIMIT:
            raise MediaError("object_limit")
        if isinstance(obj, DictionaryObject):
            if "/OpenAction" in obj and (not complete or obj is not reader.root_object):
                raise MediaError("unsupported_active_or_attachment")
            if forbidden.intersection(obj) or obj.get("/Type") in {
                "/Filespec",
                "/EmbeddedFile",
            }:
                forbidden_dictionaries.append(obj)
            if (
                obj.get("/Type") == "/Font"
                and obj.get("/Subtype") == "/Type3"
                and "/CharProcs" in obj
            ):
                charprocs = obj["/CharProcs"].get_object()
                if not isinstance(charprocs, DictionaryObject) or isinstance(
                    charprocs, StreamObject
                ):
                    raise MediaError("unsupported_font")
                glyph_dictionaries.add(id(charprocs))
                for glyph in charprocs.values():
                    glyph = glyph.get_object()
                    if not isinstance(glyph, StreamObject):
                        raise MediaError("unsupported_font")
                    glyph_streams.add(id(glyph))
            dictionaries.append(obj)
            if obj.get("/Type") == "/FontDescriptor" and "/CIDSet" in obj:
                target = obj["/CIDSet"].get_object()
                if not isinstance(target, StreamObject):
                    raise MediaError("unsupported_font")
                cid_sets.add(id(target))
            if isinstance(obj, StreamObject):
                if "/F" in obj or "/FFilter" in obj:
                    raise MediaError("external_reference")
                streams.append(obj)
            for key, value in obj.items():
                output.text(str(key))
                if key in {"/FontFile", "/FontFile2", "/FontFile3"}:
                    target = value.get_object()
                    fonts[id(target)] = str(target.get("/Subtype", key))
                if key == "/A":
                    action_entries.append((obj, value))
                if key == "/ToUnicode":
                    cmap_objects.add(id(value.get_object()))
                stack.append(value)
        elif isinstance(obj, (ArrayObject, list, tuple)):
            if len(obj) == 2 and obj[0] == "/ICCBased":
                profile = obj[1].get_object()
                if not isinstance(profile, StreamObject):
                    raise MediaError("unsupported_stream")
                icc_streams.add(id(profile))
            stack.extend(obj)
        elif isinstance(obj, (TextStringObject, NameObject, str)):
            output.text(str(obj))
        elif isinstance(obj, (ByteStringObject, bytes)):
            for codec in ("utf-8", "latin-1", "utf-16-le", "utf-16-be"):
                for start in ((0, 1) if codec.startswith("utf-16") else (0,)):
                    output.text(bytes(obj)[start:].decode(codec, errors="replace"))
    if any(id(obj) not in glyph_dictionaries for obj in forbidden_dictionaries):
        raise MediaError("unsupported_active_or_attachment")
    action_roots = [
        value
        for owner, value in action_entries
        if id(owner) not in glyph_dictionaries and owner.get("/Type") != "/StructElem"
    ]
    if open_action is not None and isinstance(
        open_action.get_object(), DictionaryObject
    ):
        action_roots.append(open_action)
    action_roots.extend(obj for obj in dictionaries if obj.get("/Type") == "/Action")
    seen_actions = set()
    while action_roots:
        action = action_roots.pop().get_object()
        if id(action) in seen_actions:
            continue
        seen_actions.add(id(action))
        if len(seen_actions) > OBJECT_LIMIT:
            raise MediaError("object_limit")
        if isinstance(action, ArrayObject):
            action_roots.extend(action)
            continue
        if not isinstance(action, DictionaryObject) or action.get("/S") not in {
            "/URI",
            "/GoTo",
        }:
            raise MediaError("unsupported_active_or_attachment")
        if action.get("/S") == "/GoTo":
            _pdf_destination(reader, action.get("/D"))
        if action.get("/S") == "/URI":
            from urllib.parse import urlsplit, unquote

            raw_uri = str(action.get("/URI", ""))
            # urllib removes raw TAB/LF before parsing; reject them before that
            # normalization so malformed source cannot become a valid mailto.
            if any(ord(char) < 32 or ord(char) == 127 for char in raw_uri):
                raise MediaError("unsupported_active_or_attachment")
            uri = urlsplit(raw_uri)
            if uri.scheme == "mailto":
                target = unquote(uri.path)
                if (
                    uri.netloc
                    or uri.fragment
                    or re.search(r"%(?![0-9a-fA-F]{2})", raw_uri)
                    or not re.fullmatch(r"[^\s@,;]+@[^\s@,;]+", target)
                    or any(ord(char) < 32 or ord(char) == 127 for char in target)
                ):
                    raise MediaError("unsupported_active_or_attachment")
                output.text(unquote(raw_uri))
            elif uri.scheme not in {"http", "https"} or not uri.netloc:
                raise MediaError("unsupported_active_or_attachment")
        if "/Next" in action:
            action_roots.append(action["/Next"])
    decoded = {}
    for stream in streams:
        if stream.get("/Subtype") == "/Image":
            filters = stream.get("/Filter")
            filters = filters if isinstance(filters, list) else [filters]
            if any(
                value
                not in {
                    None,
                    "/FlateDecode",
                    "/DCTDecode",
                    "/ASCIIHexDecode",
                    "/ASCII85Decode",
                    "/RunLengthDecode",
                    "/LZWDecode",
                    "/CCITTFaxDecode",
                }
                for value in filters
            ):
                raise MediaError("unsupported_image")
        filters = stream.get("/Filter")
        filters = filters if isinstance(filters, list) else [filters]
        _pdf_validate_stream_encoding(stream, filters)
        data = stream.get_data()
        budget["decoded"] += len(data)
        if budget["decoded"] > DATA_LIMIT:
            raise MediaError("decoded_limit")
        decoded[id(stream)] = data
        if id(stream) in fonts:
            subtype = fonts[id(stream)]
            lengths = None
            if subtype == "/FontFile":
                values = [
                    stream.get(key, 0) for key in ("/Length1", "/Length2", "/Length3")
                ]
                if any(
                    not isinstance(value, int) or isinstance(value, bool)
                    for value in values
                ):
                    raise MediaError("unsupported_font")
                lengths = [int(value) for value in values]
            _font(data, subtype, output, lengths=lengths)
    image_objects, content_objects = set(), set(glyph_streams)
    verified_contents = set()

    from pypdf import PageObject

    class ParsedPage(PageObject):
        def get_contents(self):
            return self.parsed_contents

    def inspect_content(stream, contents):
        _pdf_metadata(
            decoded[id(stream)], output, content_syntax=True, compact_bytes=True
        )
        for operands, operator in contents.operations:
            if operator == b"INLINE IMAGE":
                raise MediaError("unsupported_image")
            stack = list(operands)
            while stack:
                value = stack.pop()
                if isinstance(value, (TextStringObject, NameObject, str)):
                    output.text(str(value))
                elif isinstance(value, (ByteStringObject, bytes)):
                    output.text(bytes(value).decode("latin-1"))
                elif isinstance(value, (ArrayObject, list, tuple)):
                    stack.extend(value)
                elif isinstance(value, DictionaryObject):
                    stack.extend(value.values())
        text_streams.add(id(stream))

    for stream in streams:
        if (
            stream.get("/Subtype") == "/Image"
            and int(stream.get("/Width", 0)) * int(stream.get("/Height", 0))
            > 16_000_000
        ):
            raise MediaError("image_limit")
    for page in pages:
        page_content = page.get("/Contents")
        if page_content is not None:
            page_content = page_content.get_object()
            if isinstance(page_content, ArrayObject):
                content_objects.update(id(part.get_object()) for part in page_content)
            else:
                content_objects.add(id(page_content))
        content = page.get_contents()
        if content is not None and len(content.get_data()) > 8_000_000:
            raise MediaError("decoded_limit")
        if content is not None:
            # Match extract_text's byte-string parsing. This isolated page view
            # lets text extraction and image discovery share the same parse;
            # original reader objects and original stream bindings stay intact.
            content.forced_encoding = "bytes"
        view = ParsedPage(reader, page.indirect_reference)
        view.update(page)
        view.parsed_contents = content
        if content is not None:
            view[NameObject("/Contents")] = content
        output.text(view.extract_text())
        for image in view.images:
            image.image.load()
            output.image(image.data)
            if image.indirect_reference is not None:
                image_objects.add(id(image.indirect_reference.get_object()))
        if (
            content is not None
            and not isinstance(page_content, ArrayObject)
            and page_content.get("/Type") is None
            and page_content.get("/Subtype") is None
            and "/FunctionType" not in page_content
            and id(page_content) not in fonts
            and id(page_content) not in cid_sets
            and id(page_content) not in icc_streams
            and id(page_content) not in cmap_objects
        ):
            inspect_content(page_content, content)
            verified_contents.add(id(page_content))
        # Retain only successful IDs across pages, not every parsed operand tree.
        del view, content
    for stream in streams:
        data = decoded[id(stream)]
        kind = stream.get("/Type")
        if id(stream) in cid_sets and (
            id(stream) in fonts
            or id(stream) in content_objects
            or id(stream) in cmap_objects
            or id(stream) in icc_streams
        ):
            raise MediaError("unsupported_font")
        if id(stream) in fonts:
            continue
        if id(stream) in cid_sets:
            # ISO 32000-1 9.8.3: CID membership bitmap, up to 65536 CIDs.
            # Still inspect all byte-text views; a binary declaration must not
            # conceal literal private data or credentials in this side stream.
            if (
                set(stream) - {"/Length", "/Filter", "/DecodeParms"}
                or not 0 < len(data) <= 8192
            ):
                raise MediaError("unsupported_font")
            _pdf_text_bytes(data, output, compact=True)
            continue
        if id(stream) in icc_streams:
            if set(stream) - {
                "/N",
                "/Range",
                "/Alternate",
                "/Length",
                "/Filter",
                "/DecodeParms",
            }:
                raise MediaError("unsupported_stream")
            components = stream.get("/N")
            if (
                not isinstance(components, int)
                or isinstance(components, bool)
                or components not in {1, 3, 4}
            ):
                raise MediaError("unsupported_stream")
            _icc_profile(data, output, int(components))
            continue
        if kind == "/ObjStm":
            _pdf_metadata(data, output, compact_bytes=True)
            continue
        if kind == "/XRef":
            _pdf_xref_shape(stream)
            continue
        if stream.get("/Subtype") == "/Image":
            filters = stream.get("/Filter")
            filters = filters if isinstance(filters, list) else [filters]
            if "/DCTDecode" in filters:
                output.image(data)
            elif id(stream) not in image_objects:
                raise MediaError("unsupported_image")
        elif kind == "/Metadata" or stream.get("/Subtype") == "/XML":
            _pdf_xml_metadata(data, output)
        elif id(stream) in cmap_objects:
            _pdf_metadata(data, output, compact_bytes=True)
        elif (kind == "/Pattern" and stream.get("/PatternType") == 1) or stream.get(
            "/FunctionType"
        ) == 4:
            # Standard graphics source is inspected as inert text, never run.
            # Declared binary/unknown roles do not acquire a text exemption.
            data.decode("ascii", errors="strict")
            _pdf_metadata(data, output, content_syntax=True, compact_bytes=True)
            text_streams.add(id(stream))
            if kind == "/Pattern":
                ContentStream(stream, reader).operations
        elif id(stream) in content_objects or stream.get("/Subtype") == "/Form":
            # PDF syntax is parsed by pypdf, never evaluated as source code here.
            if id(stream) not in verified_contents:
                inspect_content(stream, ContentStream(stream, reader))
        else:
            raise MediaError("unsupported_stream")
    output.pdf_known_free_references += reader.known_free_references
    return streams


def _id3(tags, output):
    if tags is None:
        return
    if getattr(tags, "unknown_frames", []):
        raise MediaError("unsupported_metadata")
    for frame in tags.values():
        if frame.FrameID in {"PRIV", "GEOB", "AENC", "ENCR", "GRID"}:
            raise MediaError("unsupported_attachment")
        for name, value in vars(frame).items():
            if name.startswith("_"):
                continue
            if name == "data" and frame.FrameID == "APIC":
                if frame.mime == "-->":
                    raise MediaError("external_reference")
                output.image(value)
            elif isinstance(value, bytes):
                raise MediaError("unsupported_metadata")
            else:
                output.plain(value)


def _av_diagnostics(error, streams):
    if not error:
        return False
    # MOV exposes cover art as a virtual video stream without a media timescale.
    # This single advisory is allowed only when an attached picture is declared;
    # the image itself must still be extracted and pass the caller's image check.
    if not any(item.get("disposition", {}).get("attached_pic") for item in streams):
        raise MediaError("unverified")
    advisory = re.compile(
        rb"\[(?:mov,mp4,m4a,3gp,3g2,mj2|in#[0-9]+) @ 0x[0-9a-fA-F]+\] stream [0-9]+, timescale not set"
    )
    if not all(advisory.fullmatch(line) for line in error.splitlines()):
        raise MediaError("unverified")
    return True


def _packet_ranges(probe, options, path, streams):
    code, raw, error = _run(
        [
            probe,
            "-v",
            "warning",
            *options,
            "-show_packets",
            "-show_entries",
            "packet=pos,size,stream_index",
            "-of",
            "json",
            str(path),
        ],
        limit=12_000_000,
    )
    if code:
        raise MediaError("unverified")
    _av_diagnostics(error, streams)
    packets = json.loads(raw).get("packets", [])
    if not packets or len(packets) > OBJECT_LIMIT:
        raise MediaError("object_limit")
    descriptions = {item["index"]: item for item in streams}
    ranges = []
    for packet in packets:
        stream = descriptions.get(packet.get("stream_index"))
        if stream is None:
            raise MediaError("unsupported_stream")
        if stream.get("disposition", {}).get("attached_pic"):
            continue
        start, size = int(packet["pos"]), int(packet["size"])
        if start < 0 or size <= 0:
            raise MediaError("unverified")
        ranges.append((start, start + size))
    return ranges


def _covered(data, ranges, start=0, end=None):
    end = len(data) if end is None else end
    cursor = start
    for first, last in sorted(ranges):
        if first < start or last > end or first > last:
            raise MediaError("unverified")
        if first > cursor and any(data[cursor:first]):
            raise MediaError("unsupported_region")
        cursor = max(cursor, last)
    if any(data[cursor:end]):
        raise MediaError("unsupported_region")


def _audio_video(path, kind, output):
    if kind != "mp3":
        raise MediaError("unsupported_structural_coverage")
    import mutagen
    from mutagen.mp3 import MP3

    output.versions["mutagen"] = mutagen.version_string
    probe = shutil.which("ffprobe")
    decoder = shutil.which("ffmpeg")
    if not probe or not decoder:
        raise MediaError("dependency_unavailable")
    for name, binary in (("ffprobe", probe), ("ffmpeg", decoder)):
        code, text, error = _run([binary, "-version"], limit=100_000)
        if code or error:
            raise MediaError("unverified")
        output.versions[name] = text.decode("utf-8").splitlines()[0][:200]
    options = [
        "-max_alloc",
        "33554432",
        "-protocol_whitelist",
        "file,pipe",
        "-format_whitelist",
        "mp3",
    ]
    code, raw, error = _run(
        [
            probe,
            "-v",
            "error",
            *options,
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ],
        limit=TEXT_LIMIT,
    )
    if code:
        raise MediaError("unverified")
    info = json.loads(raw)
    streams = info.get("streams", [])
    if not streams or len(streams) > 32:
        raise MediaError("stream_limit")
    cover_advisory = _av_diagnostics(error, streams)
    duration = float(info.get("format", {}).get("duration", 0))
    if not 0 < duration <= 1200:
        raise MediaError("duration_limit")
    for item in [info.get("format", {}), *streams, *info.get("chapters", [])]:
        for key, value in item.get("tags", {}).items():
            output.text(key)
            output.text(value)
        for side in item.get("side_data_list", []):
            output.plain(side)
    for stream in streams:
        if stream.get("codec_type") not in {"audio", "video", "subtitle"}:
            raise MediaError("unsupported_stream")
        if stream.get("width", 0) * stream.get("height", 0) > 16_000_000:
            raise MediaError("image_limit")
        if stream.get("codec_type") == "subtitle":
            if stream.get("codec_name") not in {
                "mov_text",
                "subrip",
                "ass",
                "webvtt",
                "text",
            }:
                raise MediaError("unsupported_subtitle")
            subtitle = path.parent / ("subtitle-" + str(stream["index"]) + ".srt")
            code, _raw, error = _run(
                [
                    decoder,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    *options,
                    "-i",
                    str(path),
                    "-map",
                    "0:" + str(stream["index"]),
                    "-f",
                    "srt",
                    str(subtitle),
                ],
                limit=TEXT_LIMIT,
            )
            if code or subtitle.stat().st_size > TEXT_LIMIT:
                raise MediaError("unverified")
            _av_diagnostics(error, streams)
            output.text(subtitle.read_text(encoding="utf-8"))
    code, _raw, error = _run(
        [
            decoder,
            "-nostdin",
            "-v",
            "warning",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            "1",
            *options,
            "-i",
            str(path),
            "-map",
            "0:V?",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ],
        limit=TEXT_LIMIT,
    )
    if code:
        raise MediaError("unverified")
    _av_diagnostics(error, streams)
    packet_ranges = _packet_ranges(probe, options, path, streams)
    source_bytes = path.read_bytes()
    from mutagen.apev2 import APEv2, APENoHeaderError
    from mutagen.id3._id3v1 import ParseID3v1

    from mutagen.mp3 import MPEGFrame

    media = MP3(path)
    known_ranges = list(packet_ranges)
    if source_bytes.startswith(b"ID3"):
        if media.tags is None or not isinstance(media.tags.size, int):
            raise MediaError("unverified")
        padding = getattr(media.tags, "_padding", None)
        if not isinstance(padding, int) or padding < 0 or padding > media.tags.size:
            raise MediaError("unsupported_metadata")
        if any(source_bytes[media.tags.size - padding : media.tags.size]):
            raise MediaError("unsupported_metadata")
        known_ranges.append((0, media.tags.size))
    first_packet = min(first for first, _ in packet_ranges)
    with path.open("rb") as handle:
        handle.seek(media.info.frame_offset)
        MPEGFrame(handle)
        first_frame_end = handle.tell()
    if first_packet not in {media.info.frame_offset, first_frame_end}:
        raise MediaError("unsupported_region")
    known_ranges.append((media.info.frame_offset, first_packet))
    _id3(media.tags, output)
    tail = path.read_bytes()[-128:]
    legacy = ParseID3v1(tail)
    if legacy:
        _id3(legacy, output)
        known_ranges.append((len(source_bytes) - 128, len(source_bytes)))
    try:
        ape = APEv2(path)
    except APENoHeaderError:
        ape = None
    if ape is not None:
        from mutagen.apev2 import _APEv2Data

        with path.open("rb") as handle:
            region = _APEv2Data(handle)
        known_ranges.append((region.start, region.end))
        for key, value in ape.items():
            output.text(key)
            if value.kind != 0:
                raise MediaError("unsupported_attachment")
            output.plain(list(value))
    _covered(source_bytes, known_ranges)

    if cover_advisory and not output.images:
        raise MediaError("unsupported_image")


def _mp4_diagnostics(values):
    if not isinstance(values, list) or len(values) > OBJECT_LIMIT:
        raise MediaError("unverified")
    allowed = {
        "hachoir_avc_reserved_ones",
        "hachoir_es_reserved_one",
        "hachoir_encoder_fourcc_ascii_fallback",
    }
    for item in values:
        if item == {"code": "ffmpeg_avcc_four_byte_tail", "bytes": 4, "count": 1}:
            continue
        if (
            not isinstance(item, dict)
            or set(item) != {"code", "offset_bits", "size_bits"}
            or item["code"] not in allowed
            or any(
                type(item[key]) is not int or not 0 <= item[key] <= DATA_LIMIT * 8
                for key in ("offset_bits", "size_bits")
            )
        ):
            raise MediaError("unverified")
    return values


def _mp4(path, output):
    # All decoders see the same immutable staged bytes and a forced local MOV
    # demuxer. External data references are disabled before opening the input.
    original = path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    helper = Path(__file__).resolve().with_name("mp4_identity.py")
    helper_sha = hashlib.sha256(helper.read_bytes()).hexdigest()
    if helper_sha != "aa8407da57102abb6172fb5308ae148523598259cc529ca9c40a30a144805d2b":
        raise MediaError("dependency_unavailable")
    probe, decoder = shutil.which("ffprobe"), shutil.which("ffmpeg")
    if not probe or not decoder:
        raise MediaError("dependency_unavailable")
    for name, binary in (("ffprobe", probe), ("ffmpeg", decoder)):
        code, raw, error = _run([binary, "-version"], limit=100_000)
        if code or error:
            raise MediaError("unverified")
        output.versions[name] = raw.decode("utf-8").splitlines()[0][:200]
    options = [
        "-max_alloc",
        "33554432",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-use_absolute_path",
        "0",
    ]
    code, raw, error = _run(
        [
            probe,
            "-v",
            "error",
            *options,
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ],
        limit=TEXT_LIMIT,
    )
    if code or error:
        raise MediaError("unverified")
    info = json.loads(raw)
    streams = info.get("streams")
    if not isinstance(streams, list) or len(streams) != 1:
        raise MediaError("unsupported_stream")
    video = streams[0]
    if (
        video.get("codec_type") != "video"
        or video.get("codec_name") != "h264"
        or video.get("disposition", {}).get("attached_pic")
    ):
        raise MediaError("unsupported_stream")
    if not 0 < float(info.get("format", {}).get("duration", 0)) <= 1200:
        raise MediaError("duration_limit")
    if not 0 < video.get("width", 0) * video.get("height", 0) <= 16_000_000:
        raise MediaError("image_limit")
    for item in [info.get("format", {}), *streams, *info.get("chapters", [])]:
        output.plain(item.get("tags", {}))
        output.plain(item.get("side_data_list", []))
    ranges = _packet_ranges(probe, options, path, streams)
    code, _raw, error = _run(
        [
            decoder,
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            "1",
            *options,
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        limit=TEXT_LIMIT,
    )
    if code or error:
        raise MediaError("unverified")
    code, _raw, trace = _run(
        [
            decoder,
            "-nostdin",
            "-v",
            "trace",
            "-xerror",
            *options,
            "-i",
            str(path),
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
        limit=16_000_000,
    )
    if code:
        raise MediaError("unverified")
    packets_path = path.parent / "packets.json"
    trace_path = path.parent / "trace.txt"
    packets_path.write_text(json.dumps(ranges), encoding="ascii")
    trace_path.write_bytes(trace)
    code, raw, error = _run(
        [
            sys.executable,
            "-I",
            str(helper),
            "--source",
            str(path),
            "--packet-ranges",
            str(packets_path),
            "--trace-report",
            str(trace_path),
            "--expected-sha256",
            expected,
            "--codec",
            "h264",
        ],
        limit=16_000_000,
    )
    if code or error:
        try:
            reason = json.loads(raw).get("code")
        except (ValueError, AttributeError):
            reason = None
        if reason in {"mp4_hachoir_unavailable", "mp4_hachoir_version_unverified"}:
            raise MediaError("dependency_unavailable")
        raise MediaError("unsupported_metadata")
    result = json.loads(raw)
    if (
        not isinstance(result, dict)
        or result.get("source_sha256") != expected
        or result.get("helper_sha256") != helper_sha
        or result.get("hachoir_version") != "3.4.0"
        or result.get("codec_metadata_extracted") is not True
    ):
        raise MediaError("unverified")
    codec = result.get("codec_metadata", {})
    if (
        type(codec.get("packet_count")) is not int
        or codec["packet_count"] != len(ranges)
        or codec.get("trace_sha256") != hashlib.sha256(trace).hexdigest()
    ):
        raise MediaError("unverified")
    chunks = result.get("metadata_chunks_b64")
    spans = result.get("metadata_ranges")
    mdats = result.get("mdat_ranges")
    if not all(isinstance(value, list) for value in (chunks, spans, mdats)):
        raise MediaError("unverified")
    if not mdats or len(chunks) != len(spans) or len(spans) > OBJECT_LIMIT:
        raise MediaError("unverified")
    cursor = 0
    complement = []
    for span in mdats:
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(n) is not int for n in span)
            or not cursor <= span[0] <= span[1] <= len(original)
        ):
            raise MediaError("unverified")
        if cursor < span[0]:
            complement.append([cursor, span[0]])
        cursor = span[1]
    if cursor < len(original):
        complement.append([cursor, len(original)])
    if spans != complement:
        raise MediaError("unverified")
    for encoded, (first, last) in zip(chunks, spans):
        value = base64.b64decode(encoded, validate=True)
        if value != original[first:last]:
            raise MediaError("unverified")
        _pdf_text_bytes(value, output)
    codec_chunks = result.get("codec_metadata_chunks_b64")
    if (
        not isinstance(codec_chunks, list)
        or len(codec_chunks) > OBJECT_LIMIT
        or len(codec_chunks) % 2
        or type(codec.get("message_count")) is not int
        or codec["message_count"] != len(codec_chunks) // 2
    ):
        raise MediaError("unverified")
    decoded = [base64.b64decode(item, validate=True) for item in codec_chunks]
    if sum(map(len, decoded)) > TEXT_LIMIT:
        raise MediaError("text_limit")
    for offset in range(0, len(decoded), 2):
        uuid, payload = decoded[offset : offset + 2]
        if len(uuid) != 16:
            raise MediaError("unverified")
        # Same-message boundary is meaningful: UUID suffix plus payload prefix.
        for value in (uuid, payload, uuid + payload):
            _pdf_text_bytes(value, output)
    if (
        hashlib.sha256(path.read_bytes()).hexdigest() != expected
        or hashlib.sha256(helper.read_bytes()).hexdigest() != helper_sha
    ):
        raise MediaError("input_changed")
    output.versions["hachoir"] = "3.4.0"
    output.versions["mp4_adapter_sha256"] = helper_sha
    advisory = codec.get("trace_diagnostics")
    if (
        not isinstance(advisory, list)
        or codec.get("avcc_tail_metadata_bound") is not True
    ):
        raise MediaError("unverified")
    output.mp4_diagnostics = _mp4_diagnostics(result.get("diagnostics") + advisory)


def _child(kind, path):
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        # Darwin exposes RLIMIT_AS but rejects setting it. Logical decoded-size
        # budgets apply everywhere; Linux additionally enforces address space.
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
        resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_LIMIT, OUTPUT_LIMIT))
    output = _Extracted()
    with (
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        if kind == "pdf":
            import warnings
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = 16_000_000
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            _pdf(path, output)
        elif kind == "mp4":
            _mp4(path, output)
        else:
            _audio_video(path, kind, output)
    return output.packet()


def scan_private_media(data, patterns, *, kind, check_text, check_image):
    """Validate original bytes offline, then use only the admitted identity callbacks."""
    if not isinstance(data, bytes) or kind not in {"pdf", "mp3", "mp4"}:
        raise ValueError("private_identity_media_unsupported")
    if not data or len(data) > DATA_LIMIT:
        raise ValueError("private_identity_media_data_limit")
    if not patterns or not all(isinstance(p, str) and p.strip() for p in patterns):
        raise ValueError("private_identity_policy_empty")
    if not callable(check_text) or not callable(check_image):
        raise ValueError("private_identity_media_callback_unavailable")
    source = Path(__file__).resolve()
    adapter_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="private-media-") as directory:
            path = Path(directory) / ("input." + kind)
            path.write_bytes(data)
            path.chmod(0o600)
            code, raw, _error = _run(
                [sys.executable, "-I", str(source), "--extract", kind, str(path)],
                timeout=EXTRACT_TIMEOUT,
                group=True,
            )
            if code == 86:
                raise MediaError("dependency_unavailable")
            if code:
                try:
                    reason = json.loads(raw).get("error", "unverified")
                except (ValueError, AttributeError):
                    reason = "unverified"
                allowed = {
                    "encrypted",
                    "unsupported_active_or_attachment",
                    "external_reference",
                    "unsupported_font",
                    "unsupported_font_table",
                    "unsupported_attachment",
                    "unsupported_metadata",
                    "unsupported_image",
                    "unsupported_stream",
                    "unsupported_region",
                    "unsupported_subtitle",
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
                    "dependency_unavailable",
                }
                raise MediaError(reason if reason in allowed else "unverified")
            if (
                hashlib.sha256(path.read_bytes()).digest()
                != hashlib.sha256(data).digest()
            ):
                raise MediaError("input_changed")
            packet = json.loads(raw)
        if hashlib.sha256(source.read_bytes()).hexdigest() != adapter_sha:
            raise MediaError("adapter_changed")
        result = {
            "status": "pass",
            "literal_match_count": 0,
            "secret_match_count": 0,
            "local_path_match": False,
            "reconstructed_path_match": False,
            "metadata_path_match": False,
        }
        if (
            not isinstance(packet, dict)
            or not isinstance(packet.get("texts"), list)
            or not isinstance(packet.get("images"), list)
        ):
            raise MediaError("unverified")
        if not all(
            isinstance(value, str) for value in packet["texts"] + packet["images"]
        ):
            raise MediaError("unverified")
        pdf_sources = packet.get("pdf_byte_texts", [])
        if (
            not isinstance(pdf_sources, list)
            or not all(isinstance(value, str) for value in pdf_sources)
            or (kind != "pdf" and pdf_sources)
        ):
            raise MediaError("unverified")
        versions = packet.get("versions")
        if not isinstance(versions, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and len(value) <= 200
            for key, value in versions.items()
        ):
            raise MediaError("unverified")
        size = sum(len(text.encode("utf-8")) for text in packet["texts"])
        if (
            size > TEXT_LIMIT
            or len(packet["images"]) > 256
            or len(packet["texts"]) + len(pdf_sources) > OBJECT_LIMIT
        ):
            raise MediaError("text_limit")
        deadline = time.monotonic() + EXTRACT_TIMEOUT
        decoded_sources = []
        for value in pdf_sources:
            if time.monotonic() > deadline:
                raise MediaError("timeout")
            if len(value) > 4 * ((TEXT_LIMIT + 2) // 3):
                raise MediaError("text_limit")
            source_bytes = base64.b64decode(value, validate=True)
            size += _pdf_source_weight(source_bytes)
            if not source_bytes or size > TEXT_LIMIT:
                raise MediaError("text_limit")
            decoded_sources.append(source_bytes)

        def text_values():
            yield from packet["texts"]
            for source_bytes in decoded_sources:
                yield from _byte_text_views(source_bytes)

        image_size, seen_texts = 0, set()
        for kind_, values in (("text", text_values()), ("image", packet["images"])):
            for value in values:
                if time.monotonic() > deadline:
                    raise MediaError("timeout")
                if kind_ == "text":
                    digest = hashlib.sha256(value.encode("utf-8")).digest()
                    if not value or digest in seen_texts:
                        continue
                    if len(seen_texts) >= OBJECT_LIMIT:
                        raise MediaError("text_limit")
                    seen_texts.add(digest)
                if kind_ == "image":
                    value = base64.b64decode(value, validate=True)
                    image_size += len(value)
                    if image_size > IMAGE_LIMIT:
                        raise MediaError("image_limit")
                checked = (check_text if kind_ == "text" else check_image)(
                    value, patterns
                )
                if (
                    not isinstance(checked, dict)
                    or checked.get("status") not in {"pass", "fail"}
                    or type(checked.get("literal_match_count")) is not int
                    or checked["literal_match_count"] < 0
                    or type(checked.get("local_path_match")) is not bool
                ):
                    raise MediaError("callback_unverified")
                if (checked["status"] == "pass") != (
                    checked["literal_match_count"] == 0
                    and not checked["local_path_match"]
                ):
                    raise MediaError("callback_unverified")
                reconstructed = checked.get("reconstructed_path_match", False)
                metadata_path = checked.get("metadata_path_match", False)
                if (
                    type(reconstructed) is not bool
                    or type(metadata_path) is not bool
                    or (reconstructed or metadata_path)
                    and not checked["local_path_match"]
                ):
                    raise MediaError("callback_unverified")
                secret_count = checked.get("secret_match_count", 0)
                if type(secret_count) is not int or secret_count < 0:
                    raise MediaError("callback_unverified")
                result["secret_match_count"] += secret_count
                result["literal_match_count"] += checked["literal_match_count"]
                result["local_path_match"] |= checked["local_path_match"]
                result["reconstructed_path_match"] |= bool(
                    checked.get("reconstructed_path_match", False)
                )
        if time.monotonic() > deadline:
            raise MediaError("timeout")
        result["metadata_path_match"] = result["local_path_match"]
        result["status"] = (
            "fail"
            if result["literal_match_count"] or result["local_path_match"]
            else "pass"
        )
        free_count = packet.get("pdf_known_free_references", 0)
        if type(free_count) is not int or not 0 <= free_count <= OBJECT_LIMIT:
            raise MediaError("unverified")
        result["pdf_known_free_references"] = free_count
        checksum_count = packet.get("font_checksum_mismatches", 0)
        if type(checksum_count) is not int or not 0 <= checksum_count <= OBJECT_LIMIT:
            raise MediaError("unverified")
        result["font_checksum_mismatches"] = checksum_count
        type1_metrics = packet.get("type1_metrics", [])
        if not isinstance(type1_metrics, list) or len(type1_metrics) > OBJECT_LIMIT:
            raise MediaError("unverified")
        required = {
            "operations",
            "charged_bytes",
            "glyph_count",
            "subr_count",
            "binary_ranges",
            "output_bytes",
            "fonttools_version",
            "source_sha256",
        }
        for item in type1_metrics:
            if (
                not isinstance(item, dict)
                or set(item) != required
                or item["fonttools_version"] != "4.67.0"
                or not isinstance(item["source_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"]) is None
            ):
                raise MediaError("unverified")
            if any(
                type(item[key]) is not int or not 0 <= item[key] <= DATA_LIMIT
                for key in required - {"fonttools_version", "source_sha256"}
            ):
                raise MediaError("unverified")
        result["type1_metrics"] = type1_metrics
        result["mp4_diagnostics"] = _mp4_diagnostics(packet.get("mp4_diagnostics", []))
        result["adapter_sha256"] = adapter_sha
        result["decoder_versions"] = packet.get("versions", {})
        return result
    except (MediaError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, MediaError) else "unverified"
        raise ValueError("private_identity_media_" + reason) from None


if __name__ == "__main__":
    if (
        len(sys.argv) != 4
        or sys.argv[1] != "--extract"
        or sys.argv[2] not in {"pdf", "mp3", "mp4"}
    ):
        sys.exit(2)
    try:
        print(json.dumps(_child(sys.argv[2], Path(sys.argv[3]))))
    except ImportError:
        sys.exit(86)
    except BaseException as exc:
        print(
            json.dumps(
                {"error": str(exc) if isinstance(exc, MediaError) else "unverified"}
            )
        )
        sys.exit(87)
