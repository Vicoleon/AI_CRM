"""The shared PDF reader (``backend/tests/_pdf.py``) keeps every stream whole.

The end-of-line before ``endstream`` is not a stream's own last byte, so a
stream that happens to end in ``\\r`` must be inflated whole, not cut short and
silently dropped. Pinned for both readers: the whole-file text the brand checks
search, and the ``Tj`` strings the proposal tests read.
"""

from __future__ import annotations

import itertools
import zlib

import pytest

from backend.tests._pdf import pdf_streams, pdf_text, pdf_tj_strings

EOLS = pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf", "crlf"])


def _deflated_ending_in(last: int, render) -> tuple[str, bytes]:
    """The first ``"Acme Corp xyz"`` whose Flate encoding of ``render(text)``
    ends with byte ``last``.

    A zlib stream ends with its Adler-32, whose last byte follows the sum of
    the bytes; three printable characters span every value of it.
    """
    printable = range(33, 127)
    for x, y, z in itertools.product(printable, repeat=3):
        text = f"Acme Corp {chr(x)}{chr(y)}{chr(z)}"
        if "(" in text or ")" in text or "\\" in text:
            continue  # keep the Tj literal free of escapes
        packed = zlib.compress(render(text).encode("latin-1"))
        if packed[-1] == last:
            return text, packed
    raise AssertionError(f"no text deflates to end in {last:#x}")


def _plain(text: str) -> str:
    return text


def _tj(text: str) -> str:
    return f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"


def _one_stream_pdf(packed: bytes, eol: bytes) -> bytes:
    """A minimal PDF holding ``packed`` as its one Flate stream, then ``eol``."""
    length = str(len(packed)).encode()
    return b"".join(
        (
            b"%PDF-1.3\n1 0 obj\n<</Filter /FlateDecode /Length ",
            length,
            b">>\nstream\n",
            packed,
            eol,
            b"endstream\nendobj\n%%EOF\n",
        )
    )


@EOLS
def test_pdf_text_keeps_a_stream_whose_last_byte_is_a_carriage_return(eol):
    text, packed = _deflated_ending_in(ord("\r"), _plain)

    assert text in pdf_text(_one_stream_pdf(packed, eol))


@EOLS
def test_pdf_text_inflates_an_ordinary_stream(eol):
    text, packed = _deflated_ending_in(ord("A"), _plain)

    assert text in pdf_text(_one_stream_pdf(packed, eol))


@EOLS
@pytest.mark.parametrize("last", [ord("\r"), ord("A")], ids=["cr", "ordinary"])
def test_tj_strings_keep_a_stream_whatever_its_last_byte(eol, last):
    text, packed = _deflated_ending_in(last, _tj)

    assert pdf_tj_strings(_one_stream_pdf(packed, eol)) == [text]


def test_an_uncompressed_stream_is_read_as_written():
    data = b"%PDF-1.3\nstream\nBT (Plain words) Tj ET\r\nendstream\n%%EOF\n"

    assert pdf_streams(data) == [(b"BT (Plain words) Tj ET", False)]
    assert pdf_tj_strings(data) == ["Plain words"]
    assert "Plain words" in pdf_text(data)


def test_tj_strings_are_unescaped():
    packed = zlib.compress(rb"BT (Acme \(Pty\) Ltd) Tj ET")

    assert pdf_tj_strings(_one_stream_pdf(packed, b"\n")) == ["Acme (Pty) Ltd"]
