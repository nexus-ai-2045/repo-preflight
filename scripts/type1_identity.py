"""Guard the existing fontTools Type1 reader; never interpret fonts in the caller."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import math
import os
from pathlib import Path
import socket
import subprocess
import time
import builtins

SOURCES = {
    "fontTools.t1Lib": "4c77be140243089eecdc4eb27f17a1db538731ae596858783092e06097305da5",
    "fontTools.misc.psLib": "e00b206c318bc1ef00017d1ba97c265ca4d0ef4f10d1a88ab8049712d049ff3b",
    "fontTools.misc.psOperators": "f522e5e4f3c1ba52e8d17c60fddaa524c8ad34805d88629d91784e5ac3526191",
    "fontTools.misc.psCharStrings": "959126b00d7dc3e09c57bcb312eaa9659b6fa57197561416381b5218b15ff879",
    "fontTools.misc.eexec": "18d9f638246f3b51db6c87833f193d8b48253bb72ec7f0106a854c5e1051f32f",
}

# Exact raw sources in the SHA-verified official fontTools 4.67.0 Windows
# wheel. Its CRLF sources otherwise equal the existing LF source pins.
WINDOWS_SOURCES = {
    "fontTools.t1Lib": "cd5566943584bfd5182a95006025f9ae26f33477744e020673f84163e40e11c4",
    "fontTools.misc.psLib": "d47834e0910fe2b457cecabd77c7aafad753015c4c42e6eb7002db4445ec7e4a",
    "fontTools.misc.psOperators": "f67678ca66e288202963d57c38046962abceef7384709806c93b42b86cc3fabc",
    "fontTools.misc.psCharStrings": "1be66367972ea80a79d66ae1a3e4489a66def6e527c50fce512e230b8a54e118",
    "fontTools.misc.eexec": "78df51d7febbb5669e5a7de2904b3457007537bcabe2f05bcecf9a31b01444ec",
}


class Type1Error(ValueError):
    """Fixed diagnostic vocabulary, no source values."""


def _verify_source_bytes(name: str, source: bytes) -> None:
    """Accept only reviewed raw LF/Windows CRLF artifacts of the same source."""
    expected = SOURCES[name]
    actual = hashlib.sha256(source).hexdigest()
    if actual == expected:
        return
    if (
        actual == WINDOWS_SOURCES[name]
        and hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest() == expected
    ):
        return
    raise Type1Error("dependency")


class _Budget:
    def __init__(self, objects, bytes_limit, seconds):
        self.objects = objects
        self.bytes_limit = bytes_limit
        self.deadline = time.monotonic() + seconds
        self.operations = self.allocated = self.output_bytes = 0
        self.values = {}

    def tick(self, amount=1):
        self.operations += amount
        if self.operations > self.objects:
            raise Type1Error("budget")
        if time.monotonic() > self.deadline:
            raise Type1Error("timeout")

    def allocation(self, amount):
        if amount < 0 or self.allocated + amount > self.bytes_limit:
            raise Type1Error("allocation")
        self.allocated += amount

    def value(self, value):
        if (
            isinstance(value, (bytes, str, list, tuple, dict))
            and id(value) not in self.values
        ):
            self.allocation(
                len(value) * (8 if isinstance(value, (list, tuple, dict)) else 4)
            )
            self.values[id(value)] = value


@contextlib.contextmanager
def _no_host_io():
    def deny(*args, **kwargs):
        raise Type1Error("external_io")

    targets = [
        (builtins, "open"),
        (io, "open"),
        (os, "open"),
        (os, "system"),
        (socket, "socket"),
        (subprocess, "Popen"),
    ]
    previous = [(owner, key, getattr(owner, key)) for owner, key in targets]
    for owner, key in targets:
        setattr(owner, key, deny)
    try:
        yield
    finally:
        for owner, key, value in previous:
            setattr(owner, key, value)


def _scan_type1(
    data,
    lengths,
    *,
    scan_metadata,
    emit_text,
    objects=100000,
    bytes_limit=32000000,
    seconds=20,
):
    """Inspect original/decrypted metadata and reader-bound complete glyph codewords."""
    import fontTools
    from fontTools import t1Lib
    from fontTools.misc import psLib, psCharStrings, eexec
    from fontTools.pens.basePen import NullPen

    if fontTools.version != "4.67.0":
        raise Type1Error("dependency")
    for name in SOURCES:
        _verify_source_bytes(
            name, Path(importlib.import_module(name).__file__).read_bytes()
        )
    # This reviewed subset is small Type1 programs. Bound the existing lexer's
    # largest possible token before it materializes a source literal/comment.
    if isinstance(data, bytes) and len(data) > 2000000:
        raise Type1Error("allocation")
    if (
        not isinstance(data, bytes)
        or len(data) > 64000000
        or len(lengths) != 3
        or any(type(x) is not int or x < 0 for x in lengths)
        or sum(lengths) != len(data)
        or lengths[0] == 0
        or lengths[1] < 4
    ):
        raise Type1Error("source_binding")
    t1Lib.assertType1(data)
    clear, cipher, tail = lengths
    plain, _ = eexec.decrypt(data[clear : clear + cipher], 55665)
    budget = _Budget(objects, bytes_limit, seconds)
    reads, directories, eexec_events = {}, [], []
    original_interpreter, original_tokenizer = psLib.PSInterpreter, psLib.PSTokenizer
    original_charstring, original_outline = (
        psCharStrings.T1CharString,
        psCharStrings.T1OutlineExtractor,
    )
    current_outline = [None]

    class Sink:
        def write(self, value):
            size = len(value.encode("utf-8"))
            if budget.output_bytes + size > 2000000:
                raise Type1Error("output")
            budget.output_bytes += size
            emit_text(value)
            return len(value)

        def flush(self):
            pass

    class Tokenizer(original_tokenizer):
        def getnexttoken(self, *args, **kwargs):
            budget.tick()
            kind, token = super().getnexttoken(*args, **kwargs)
            if token is not None and len(token) > 2000000:
                raise Type1Error("allocation")
            return kind, token

        def skipwhite(self, *args, **kwargs):
            if eexec_events and self.buf is eexec_events[0]["buffer"]:
                if not 0 <= self.pos <= cipher:
                    raise Type1Error("source_binding")
                # Reuse the existing lexer, but never let decrypted trailer
                # bytes outside declared Length2 alter the cipher boundary.
                return super().skipwhite(
                    whitematch=lambda value, position: psLib.skipwhiteRE.match(
                        value, position, cipher
                    )
                )
            return super().skipwhite(*args, **kwargs)

        def starteexec(self):
            if self.buf is not data or self.pos + 1 != clear or eexec_events:
                raise Type1Error("source_binding")
            super().starteexec()
            if self.buf[:cipher] != plain:
                raise Type1Error("source_binding")
            eexec_events.append(
                {"tokenizer": self, "buffer": self.buf, "closed": False}
            )

        def stopeexec(self):
            if (
                not eexec_events
                or eexec_events[0]["tokenizer"] is not self
                or self.buf is not eexec_events[0]["buffer"]
                or not 4 <= self.pos <= cipher
            ):
                raise Type1Error("source_binding")
            eexec_events[0]["closed"] = True
            super().stopeexec()

    class Interpreter(original_interpreter):
        def __init__(self, *args, **kwargs):
            self.depth = 0
            self.binding_depth = 0
            super().__init__(*args, **kwargs)

        def push(self, obj):
            budget.tick()
            if len(self.stack) >= 4096:
                raise Type1Error("stack")
            budget.value(obj.value)
            if isinstance(obj.value, str):
                emit_text(obj.value)
            return super().push(obj)

        def pop(self, *types):
            budget.tick()
            return super().pop(*types)

        def handle_object(self, obj):
            budget.tick()
            if len(self.dictstack) > 64 or self.proclevel > 64:
                raise Type1Error("depth")
            return super().handle_object(obj)

        def call_procedure(self, proc):
            budget.tick()
            self.depth += 1
            if self.depth > 64:
                raise Type1Error("depth")
            try:
                return super().call_procedure(proc)
            finally:
                self.depth -= 1

        def proc_bind(self, proc):
            budget.tick(2 * len(proc.value))
            self.binding_depth += 1
            self.depth += 1
            if self.depth > 64:
                raise Type1Error("depth")
            try:
                return super().proc_bind(proc)
            finally:
                self.depth -= 1
                self.binding_depth -= 1

        def resolve_name(self, name):
            if not self.binding_depth:
                budget.tick()
            if name in {
                "file",
                "run",
                "deletefile",
                "renamefile",
                "status",
                "socket",
                "system",
                "execfile",
                "filenameforall",
                "setfileposition",
            }:
                raise Type1Error("external_io")
            return super().resolve_name(name)

        def do_makearray(self):
            count = 0
            for item in reversed(self.stack):
                if item is self.mark:
                    break
                count += 1
            else:
                raise Type1Error("stack")
            budget.allocation(count * 8)
            return super().do_makearray()

        def do_special(self, token):
            if token == "}":
                count = 0
                for item in reversed(self.stack):
                    if item is self.procmark:
                        break
                    count += 1
                else:
                    raise Type1Error("stack")
                budget.allocation(count * 8)
            result = super().do_special(token)
            if self.proclevel > 64:
                raise Type1Error("depth")
            return result

        def ps_array(self):
            number = self.stack[-1].value
            if type(number) is not int or not 0 <= number <= objects:
                raise Type1Error("allocation")
            budget.allocation(number * 8)
            return super().ps_array()

        def ps_string(self):
            number = self.stack[-1].value
            if type(number) is not int or not 0 <= number <= 2000000:
                raise Type1Error("allocation")
            budget.allocation(number * 4)
            return super().ps_string()

        def ps_dict(self):
            number = self.stack[-1].value
            if type(number) is not int or not 0 <= number <= objects:
                raise Type1Error("allocation")
            budget.allocation(number * 8)
            return super().ps_dict()

        def ps_for(self):
            start, step, last = [x.value for x in self.stack[-4:-1]]
            if (
                not all(
                    isinstance(x, (int, float))
                    and math.isfinite(x)
                    and abs(x) <= objects
                    for x in (start, step, last)
                )
                or step == 0
            ):
                raise Type1Error("loop")
            budget.tick(max(0, math.floor((last - start) / step) + 1))
            return super().ps_for()

        def ps_forall(self):
            budget.tick(len(self.stack[-2].value))
            return super().ps_forall()

        def ps_def(self):
            if len(self.dictstack[-1]) >= objects:
                raise Type1Error("allocation")
            return super().ps_def()

        def ps_store(self):
            if len(self.dictstack[-1]) >= objects:
                raise Type1Error("allocation")
            return super().ps_store()

        def ps_put(self):
            target, index, value = self.stack[-3], self.stack[-2], self.stack[-1]
            if target.type in {"arraytype", "proceduretype", "stringtype"}:
                if (
                    index.type != "integertype"
                    or type(index.value) is not int
                    or not 0 <= index.value < len(target.value)
                ):
                    raise Type1Error("source_binding")
                if target.type == "stringtype" and (
                    value.type != "integertype"
                    or type(value.value) is not int
                    or not 0 <= value.value <= 255
                ):
                    raise Type1Error("source_binding")
            if (
                isinstance(target.value, (dict, list, tuple))
                and len(target.value) >= objects
            ):
                raise Type1Error("allocation")
            return super().ps_put()

        def ps_get(self):
            target, index = self.stack[-2], self.stack[-1]
            if target.type in {"arraytype", "proceduretype", "stringtype"} and (
                index.type != "integertype"
                or type(index.value) is not int
                or not 0 <= index.value < len(target.value)
            ):
                raise Type1Error("source_binding")
            return super().ps_get()

        def ps_getinterval(self):
            target, start, length = self.stack[-3], self.stack[-2], self.stack[-1]
            if (
                target.type not in {"arraytype", "stringtype"}
                or start.type != "integertype"
                or length.type != "integertype"
                or type(start.value) is not int
                or type(length.value) is not int
                or start.value < 0
                or length.value < 0
                or start.value + length.value > len(target.value)
            ):
                raise Type1Error("source_binding")
            budget.allocation(length.value * 8)
            return super().ps_getinterval()

        def ps_putinterval(self):
            target, index, value = self.stack[-3], self.stack[-2].value, self.stack[-1]
            if (
                target.type not in {"arraytype", "stringtype"}
                or target.type != value.type
                or self.stack[-2].type != "integertype"
                or type(index) is not int
                or index < 0
                or index + len(value.value) > len(target.value)
            ):
                raise Type1Error("allocation")
            budget.allocation(len(target.value) * 4)
            return super().ps_putinterval()

        def ps_readstring(self):
            string, file = self.stack[-1], self.stack[-2]
            tokenizer = file.value
            start = tokenizer.pos + 1
            size = len(string.value)
            if (
                not eexec_events
                or tokenizer is not eexec_events[0]["tokenizer"]
                or tokenizer.buf is not eexec_events[0]["buffer"]
                or not 4 <= start <= start + size <= cipher
                or id(string) in reads
            ):
                raise Type1Error("source_binding")
            super().ps_readstring()
            if (
                tokenizer.pos != start + size
                or len(string.value) != size
                or string.value != plain[start : start + size]
            ):
                raise Type1Error("source_binding")
            reads[id(string)] = {
                "object": string,
                "raw": string.value,
                "range": (start, start + size),
                "used": False,
            }

        def close(self):
            directories.append(self.dictstack[0]["FontDirectory"])
            super().close()

    class CharString(original_charstring):
        def getToken(self, index, *args, **kwargs):
            budget.tick()
            if (
                current_outline[0] is not None
                and len(current_outline[0].operandStack) > 4096
            ):
                raise Type1Error("stack")
            return super().getToken(index, *args, **kwargs)

    class Outline(original_outline):
        def execute(self, charstring):
            budget.tick()
            if self.subrLevel >= 64:
                raise Type1Error("depth")
            previous = current_outline[0]
            current_outline[0] = self
            try:
                return super().execute(charstring)
            finally:
                current_outline[0] = previous

        def op_callsubr(self, index):
            number = self.operandStack[-1]
            if type(number) is not int or not 0 <= number < len(self.subrs):
                raise Type1Error("codewords")
            return super().op_callsubr(index)

    psLib.PSInterpreter, psLib.PSTokenizer = Interpreter, Tokenizer
    psCharStrings.T1CharString, psCharStrings.T1OutlineExtractor = CharString, Outline
    try:
        font = t1Lib.T1Font.__new__(t1Lib.T1Font)
        font.data = data
        font.encoding = "latin-1"
        with (
            _no_host_io(),
            contextlib.redirect_stdout(Sink()),
            contextlib.redirect_stderr(Sink()),
        ):
            font.parse()
            for glyph in font.getGlyphSet().values():
                glyph.decompile()
                glyph.draw(NullPen())
        if len(eexec_events) != 1 or not eexec_events[0]["closed"]:
            raise Type1Error("source_binding")
        all_fonts = []
        for directory in directories:
            for value in directory.value.values():
                if "CharStrings" in value.value:
                    all_fonts.append(value)
        glyph_count = subr_count = 0
        binary_objects = set()
        for raw_font in all_fonts:
            private = raw_font.value["Private"].value
            length_iv = private.get("lenIV")
            length_iv = length_iv.value if length_iv is not None else 4
            if type(length_iv) is not int or not 0 <= length_iv <= 32:
                raise Type1Error("codewords")
            subrs = private["Subrs"].value
            glyphs = raw_font.value["CharStrings"].value
            decoded_subrs = []

            def decode(obj, *, subroutine=False):
                budget.tick()
                record = reads.get(id(obj))
                if (
                    record is None
                    or record["object"] is not obj
                    or obj.value != record["raw"]
                    or len(obj.value) < length_iv
                ):
                    raise Type1Error("source_binding")
                record["used"] = True
                binary_objects.add(id(obj))
                decoded, _ = eexec.decrypt(obj.value, 4330)
                char = CharString(decoded[length_iv:], subrs=decoded_subrs)
                char.decompile()
                if (
                    not char.program
                    or len(char.program) > objects
                    or any(
                        isinstance(x, str) and x not in original_charstring.opcodes
                        for x in char.program
                    )
                ):
                    raise Type1Error("codewords")
                terminals = {"return"} if subroutine else {"endchar", "seac"}
                if char.program[-1] not in terminals or any(
                    token in {"return", "endchar", "seac"}
                    for token in char.program[:-1]
                    if isinstance(token, str)
                ):
                    raise Type1Error("codewords")
                return char

            for subr in subrs:
                decoded_subrs.append(decode(subr, subroutine=True))
                subr_count += 1
            for name, glyph in glyphs.items():
                emit_text(str(name))
                char = decode(glyph)
                with (
                    _no_host_io(),
                    contextlib.redirect_stdout(Sink()),
                    contextlib.redirect_stderr(Sink()),
                ):
                    char.draw(NullPen())
                glyph_count += 1
        if not reads or not all(record["used"] for record in reads.values()):
            raise Type1Error("source_binding")
        records = sorted(reads.values(), key=lambda record: record["range"])
        ranges = [record["range"] for record in records]
        cursor = 4
        masked = bytearray(plain)
        for record in records:
            start, end = record["range"]
            if start < cursor or plain[start:end] != record["raw"]:
                raise Type1Error("source_binding")
            masked[start:end] = b" " * (end - start)
            cursor = end
        # No searching for byte occurrences: exact reader-recorded offsets only.
        scan_metadata(data[:clear] + b" " * cipher + data[clear + cipher :])
        scan_metadata(bytes(masked[4:]))
        visited = set()

        def metadata(obj, depth=0):
            budget.tick()
            if depth > 64:
                raise Type1Error("depth")
            if obj is None:
                return
            if id(obj) in visited:
                return
            visited.add(id(obj))
            if id(obj) in binary_objects:
                return
            value = getattr(obj, "value", obj)
            if isinstance(value, dict):
                for key, item in value.items():
                    emit_text(str(key))
                    metadata(item, depth + 1)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    metadata(item, depth + 1)
            elif isinstance(value, str):
                emit_text(value)
            elif isinstance(value, bytes):
                for codec in ("utf-8", "latin-1", "utf-16-le", "utf-16-be"):
                    emit_text(value.decode(codec, errors="replace"))

        for directory in directories:
            metadata(directory)
        return {
            "operations": budget.operations,
            "charged_bytes": budget.allocated,
            "glyph_count": glyph_count,
            "subr_count": subr_count,
            "binary_ranges": len(ranges),
            "output_bytes": budget.output_bytes,
            "fonttools_version": fontTools.version,
        }
    finally:
        psLib.PSInterpreter, psLib.PSTokenizer = (
            original_interpreter,
            original_tokenizer,
        )
        psCharStrings.T1CharString, psCharStrings.T1OutlineExtractor = (
            original_charstring,
            original_outline,
        )


def scan_type1(*args, **kwargs):
    """Return only fixed failures, including when an underlying PS name is invalid."""
    try:
        return _scan_type1(*args, **kwargs)
    except Type1Error:
        raise
    except Exception as error:
        reason = str(error)
        allowed = {
            "text_limit",
            "image_limit",
            "object_limit",
            "decoded_limit",
            "timeout",
            "unsupported_region",
        }
        raise Type1Error(reason if reason in allowed else "unverified") from None
