"""Read the text out of a PDF the app generated, with no PDF library.

Shared by every test that checks what a generated PDF says (the brand checks in
``test_generated_filenames.py`` and the proposal tests), so all of them read
streams the same way.

A stream is captured up to ``endstream`` WITH the end-of-line before it: that
line end cannot be told apart from a stream whose own last byte is ``\\r``, so
it is left for zlib, which stops at the end of its own stream and leaves the
line end unused. Cutting the line end off first would sometimes cut the
stream's last byte instead, and zlib would refuse the stream, silently losing
its text.
"""

from __future__ import annotations

import re
import zlib

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
#: fpdf2 writes each run of text as one ``(text) Tj`` operator.
_TJ = re.compile(rb"\((.*?)\)\s*Tj", re.DOTALL)


def _without_line_end(raw: bytes) -> bytes:
    """An uncompressed stream's bytes, minus the end-of-line before ``endstream``."""
    for eol in (b"\r\n", b"\n", b"\r"):
        if raw.endswith(eol):
            return raw[: -len(eol)]
    return raw


def pdf_streams(data: bytes) -> list[tuple[bytes, bool]]:
    """Every stream's content, and whether it was Flate-compressed.

    A complete Flate stream comes back inflated; anything else (an uncompressed
    stream) comes back as written, minus its line end.
    """
    streams: list[tuple[bytes, bool]] = []
    for raw in _STREAM.findall(data):
        inflater = zlib.decompressobj()
        try:
            inflated = inflater.decompress(raw)
        except zlib.error:
            streams.append((_without_line_end(raw), False))
            continue
        if inflater.eof:  # a complete stream; anything after it is the line end
            streams.append((inflated, True))
        else:
            streams.append((_without_line_end(raw), False))
    return streams


def pdf_text(data: bytes) -> str:
    """A PDF's raw bytes plus every Flate stream, inflated, for a whole-file
    search (a brand name, a banned term) wherever the text sits."""
    parts = [data.decode("latin-1")]
    parts.extend(
        content.decode("latin-1")
        for content, inflated in pdf_streams(data)
        if inflated  # an uncompressed stream is already in the raw bytes
    )
    return "\n".join(parts)


def pdf_tj_strings(data: bytes) -> list[str]:
    """The literal strings fpdf2 wrote with ``Tj``, in order, unescaped: the
    words a reader of the page sees."""
    return [
        text.decode("latin-1").replace("\\(", "(").replace("\\)", ")")
        for content, _inflated in pdf_streams(data)
        for text in _TJ.findall(content)
    ]
