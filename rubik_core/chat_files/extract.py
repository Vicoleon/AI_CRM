"""Check an attached file and read what can be read without a model.

Pure functions over bytes: no database, no network. :func:`validate` refuses a
file before anything is stored; :func:`extract` turns an accepted one into
text (and, for a picture, a copy small enough to send to the chat model).

What needs a model is flagged rather than guessed at: a picture, and a PDF
whose pages carry no text layer (a scan). ``reader.read_file`` reads those.

Office files are zip archives. Their declared uncompressed size is checked
before any library opens them (:func:`_check_zip`), so a small upload cannot
expand into gigabytes in memory.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from rubik_core.brand import PRODUCT_NAME

logger = logging.getLogger(__name__)


class ChatFileError(ValueError):
    """A refusal the person can act on. Its message is shown to them verbatim."""


# ── What Cubie reads ────────────────────────────────────────────────────────

KIND_PDF = "pdf"
KIND_WORD = "word"
KIND_SPREADSHEET = "spreadsheet"
KIND_PRESENTATION = "presentation"
KIND_TEXT = "text"
KIND_IMAGE = "image"

#: suffix → (kind, MIME type Cubie records and serves it back as)
SUFFIXES: dict[str, tuple[str, str]] = {
    ".pdf": (KIND_PDF, "application/pdf"),
    ".docx": (
        KIND_WORD,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".xlsx": (
        KIND_SPREADSHEET,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".xlsm": (KIND_SPREADSHEET, "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ".csv": (KIND_SPREADSHEET, "text/csv"),
    ".tsv": (KIND_SPREADSHEET, "text/tab-separated-values"),
    ".pptx": (
        KIND_PRESENTATION,
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".txt": (KIND_TEXT, "text/plain"),
    ".md": (KIND_TEXT, "text/markdown"),
    ".json": (KIND_TEXT, "application/json"),
    ".png": (KIND_IMAGE, "image/png"),
    ".jpg": (KIND_IMAGE, "image/jpeg"),
    ".jpeg": (KIND_IMAGE, "image/jpeg"),
    ".webp": (KIND_IMAGE, "image/webp"),
    ".gif": (KIND_IMAGE, "image/gif"),
    ".heic": (KIND_IMAGE, "image/heic"),
    ".heif": (KIND_IMAGE, "image/heif"),
}

#: The pre-2007 binary Office formats. No reader for them is installed, and
#: re-saving is one click in the program that made them.
_LEGACY = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}

KIND_LABELS: dict[str, str] = {
    KIND_PDF: "PDF",
    KIND_WORD: "Word document",
    KIND_SPREADSHEET: "spreadsheet",
    KIND_PRESENTATION: "presentation",
    KIND_TEXT: "text file",
    KIND_IMAGE: "image",
}

MAX_FILENAME_CHARS = 255

# A picture is sent to the chat model at this size at most: Gemini tiles larger
# images anyway, and the copy is kept in the conversation's history.
IMAGE_MAX_EDGE = 1600
IMAGE_MAX_PIXELS = 40_000_000
# A HEIC photo cannot be decoded here (no plugin in the image), so it goes to
# the model as it is: allowed only while it stays a modest inline payload.
HEIC_PASSTHROUGH_MAX_BYTES = 7_000_000

# A PDF averaging fewer characters than this per page has no real text layer:
# it is a scan, and the model reads it instead.
SCANNED_PDF_CHARS_PER_PAGE = 40
MAX_PDF_PAGES = 500
# A scan goes to the model whole, as inline bytes: base64 makes it a third
# larger, under Vertex's ~20 MB request ceiling only up to about 14 MB. Its full
# reading must also fit one answer (MAX_OUTPUT_TOKENS in reader.py).
SCANNED_PDF_MAX_BYTES = 14_000_000
SCANNED_PDF_MAX_PAGES = 40

# Zip safety for .docx/.xlsx/.pptx: refuse an archive that claims to expand
# past this, or that compresses suspiciously well.
ZIP_MAX_UNCOMPRESSED = 100 * 1024 * 1024
ZIP_MAX_RATIO = 200

# A spreadsheet can DECLARE a huge grid in a few KB (one cell at XFD1048576),
# and openpyxl pads every row out to it. These bound the work, not the reading
# of any real workbook.
SPREADSHEET_MAX_ROWS_PER_SHEET = 20_000
SPREADSHEET_MAX_COLS = 200
SPREADSHEET_MAX_CELLS = 1_000_000
SPREADSHEET_MAX_SHEETS = 50
# This many blank rows in a row is the padding above, not a gap in real data:
# the sheet stops there and the next one is read.
SPREADSHEET_MAX_BLANK_RUN = 1_000


def suffix_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def accepted_suffixes() -> list[str]:
    return sorted(SUFFIXES)


@dataclass
class Extracted:
    """What was read locally. ``text`` may be empty when ``needs_model``."""

    kind: str
    mime_type: str
    text: str = ""
    #: "12 pages", "3 sheets", "8 slides", "1280×720" — shown to the model.
    detail: str = ""
    #: A picture, or a scanned PDF: the reader model must read the media.
    needs_model: bool = False
    #: The bytes the model (and, for a picture, the chat) should see.
    media: bytes | None = None
    media_mime: str | None = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)


# ── Validation ──────────────────────────────────────────────────────────────


def validate(filename: str, data: bytes, *, max_bytes: int) -> tuple[str, str]:
    """Refuse a file before it is stored. Returns ``(kind, mime_type)``."""
    name = (filename or "").strip()
    if not name:
        raise ChatFileError("That file had no name.")
    if len(name) > MAX_FILENAME_CHARS:
        raise ChatFileError("That file's name is too long. Rename it and try again.")
    suffix = suffix_of(name)
    if suffix in _LEGACY:
        raise ChatFileError(
            f"{PRODUCT_NAME} can't read the older {suffix} format. Open the file and save it "
            f"as {_LEGACY[suffix]}, then attach it again."
        )
    if suffix not in SUFFIXES:
        raise ChatFileError(
            f"{PRODUCT_NAME} can read Word (.docx), PDF, Excel (.xlsx, .csv), PowerPoint "
            "(.pptx), text files and pictures (PNG, JPG, WEBP, GIF, HEIC)."
        )
    if not data:
        raise ChatFileError("That file is empty.")
    if len(data) > max_bytes:
        mb = max_bytes // 1_000_000
        raise ChatFileError(f"That file is larger than {mb} MB. Attach a smaller one.")
    kind, mime = SUFFIXES[suffix]
    _check_signature(kind, suffix, data)
    return kind, mime


def _check_signature(kind: str, suffix: str, data: bytes) -> None:
    """The bytes must be what the name says, so a renamed file is refused here
    rather than half-read later."""
    wrong = ChatFileError(
        "That file doesn't look like the kind its name says. Check it opens on "
        "your computer, then attach it again."
    )
    if kind == KIND_PDF and not data[:1024].lstrip().startswith(b"%PDF"):
        raise wrong
    if suffix in {".docx", ".xlsx", ".xlsm", ".pptx"} and not data.startswith(b"PK"):
        raise wrong
    if suffix in {".heic", ".heif"} and data[4:8] != b"ftyp":
        raise wrong
    if kind == KIND_IMAGE and suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        heads = {
            ".png": (b"\x89PNG",),
            ".jpg": (b"\xff\xd8",),
            ".jpeg": (b"\xff\xd8",),
            ".gif": (b"GIF8",),
            ".webp": (b"RIFF",),
        }[suffix]
        if not data.startswith(heads):
            raise wrong


def _check_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile:
        raise ChatFileError(
            "That file is damaged or isn't really an Office file. Check it opens "
            "on your computer, then attach it again."
        ) from None
    total = sum(i.file_size for i in infos)
    if total > ZIP_MAX_UNCOMPRESSED or total > ZIP_MAX_RATIO * max(len(data), 1):
        raise ChatFileError("That file is too large to read once it's unpacked.")
    return zf


# ── Reading ─────────────────────────────────────────────────────────────────


def extract(filename: str, data: bytes, *, max_chars: int) -> Extracted:
    """Read an accepted file. Raises :class:`ChatFileError` when it can't be read."""
    suffix = suffix_of(filename)
    kind, mime = SUFFIXES[suffix]
    try:
        if kind == KIND_PDF:
            out = _pdf(data, max_chars)
        elif kind == KIND_WORD:
            out = _docx(data, max_chars)
        elif kind == KIND_SPREADSHEET:
            out = (
                _delimited(data, max_chars, "\t" if suffix == ".tsv" else ",")
                if suffix in {".csv", ".tsv"}
                else _xlsx(data, max_chars)
            )
        elif kind == KIND_PRESENTATION:
            out = _pptx(data, max_chars)
        elif kind == KIND_TEXT:
            out = _plain(data, max_chars)
        else:
            out = _image(data, suffix, mime)
    except ChatFileError:
        raise
    except Exception:  # noqa: BLE001 — any parser failure is one plain refusal
        logger.warning("[chat-files] could not read a %s upload", suffix, exc_info=True)
        raise ChatFileError(
            f"{PRODUCT_NAME} couldn't open that file. Check it opens on your computer, then "
            "attach it again."
        ) from None
    out.kind, out.mime_type = kind, mime
    out.text = clean_text(out.text)
    return out


def clean_text(text: str | None) -> str:
    """Text Postgres will store: no NUL characters (a UTF-16 file read wrongly,
    some PDFs, a model's answer can all carry them)."""
    return (text or "").replace("\x00", "")


def _cap(text: str, max_chars: int) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def _decode(data: bytes) -> str:
    # Windows Notepad and Excel's "Unicode text" save UTF-16 with a byte-order mark.
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _pdf(data: bytes, max_chars: int) -> Extracted:
    import fitz  # PyMuPDF

    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ChatFileError(
                "That PDF is password-protected. Save a copy without the password "
                "and attach that."
            )
        pages = doc.page_count
        parts: list[str] = []
        total = 0
        for i, page in enumerate(doc):
            if i >= MAX_PDF_PAGES or total > max_chars:
                break
            body = page.get_text().strip()
            if body:
                parts.append(f"--- Page {i + 1} ---\n{body}")
                total += len(body)
    text, truncated = _cap("\n\n".join(parts), max_chars)
    out = Extracted(
        kind=KIND_PDF,
        mime_type="application/pdf",
        text=text,
        detail=f"{pages} page{'s' if pages != 1 else ''}",
        truncated=truncated or pages > MAX_PDF_PAGES,
    )
    if total < SCANNED_PDF_CHARS_PER_PAGE * max(min(pages, MAX_PDF_PAGES), 1):
        # A scan (or mostly pictures): the model reads the pages themselves,
        # when the scan is small enough to hand it whole.
        if len(data) <= SCANNED_PDF_MAX_BYTES and pages <= SCANNED_PDF_MAX_PAGES:
            out.needs_model = True
            out.media, out.media_mime = data, "application/pdf"
        elif not text:
            raise ChatFileError(
                f"That PDF is a scan of {pages} pages. {PRODUCT_NAME} reads scans of up to "
                f"{SCANNED_PDF_MAX_PAGES} pages and "
                f"{SCANNED_PDF_MAX_BYTES // 1_000_000} MB; split it and attach the "
                "part you need."
            )
        else:
            out.notes.append("mostly scanned pages; only the text layer was read")
    return out


def _docx(data: bytes, max_chars: int) -> Extracted:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    _check_zip(data)
    doc = Document(io.BytesIO(data))
    parts: list[str] = []
    total = 0
    tables = 0
    # Body order, so a table sits under the paragraph that introduces it.
    for child in doc.element.body.iterchildren():
        if total > max_chars:
            break
        if child.tag == qn("w:p"):
            line = Paragraph(child, doc).text.strip()
        elif child.tag == qn("w:tbl"):
            tables += 1
            line = _rows_to_text(_docx_rows(Table(child, doc)))
        else:
            continue
        if line:
            parts.append(line)
            total += len(line)
    text, truncated = _cap("\n\n".join(parts), max_chars)
    detail = f"{tables} table{'s' if tables != 1 else ''}" if tables else ""
    return Extracted(
        kind=KIND_WORD, mime_type="", text=text, detail=detail, truncated=truncated
    )


def _docx_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            value = " ".join(cell.text.split())
            # A merged cell repeats once per grid column it spans.
            if cells and cells[-1] == value and value:
                continue
            cells.append(value)
        rows.append(cells)
    return rows


def _rows_to_text(rows: list[list[str]]) -> str:
    lines = [" | ".join(r) for r in rows if any(c.strip() for c in r)]
    return "\n".join(lines)


def _xlsx(data: bytes, max_chars: int) -> Extracted:
    import openpyxl

    _check_zip(data)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        parts: list[str] = []
        total = 0
        sheets = len(wb.worksheets)
        cells_seen = 0
        truncated = sheets > SPREADSHEET_MAX_SHEETS
        for ws in wb.worksheets[:SPREADSHEET_MAX_SHEETS]:
            if total > max_chars or cells_seen > SPREADSHEET_MAX_CELLS:
                truncated = True
                break
            # Ignore the grid size the file declares (see SPREADSHEET_MAX_*).
            ws.reset_dimensions()
            lines: list[str] = []
            blank_run = 0
            rows = ws.iter_rows(values_only=True, max_col=SPREADSHEET_MAX_COLS)
            for n, row in enumerate(rows):
                cells_seen += len(row)
                if (
                    n >= SPREADSHEET_MAX_ROWS_PER_SHEET
                    or total > max_chars
                    or cells_seen > SPREADSHEET_MAX_CELLS
                ):
                    truncated = True
                    break
                cells = ["" if v is None else str(v).strip() for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not any(cells):
                    blank_run += 1
                    if blank_run > SPREADSHEET_MAX_BLANK_RUN:
                        truncated = True
                        break
                    continue
                blank_run = 0
                line = " | ".join(cells)
                lines.append(line)
                total += len(line)
            if lines:
                parts.append(f"## Sheet: {ws.title}\n" + "\n".join(lines))
    finally:
        wb.close()
    text, capped = _cap("\n\n".join(parts), max_chars)
    return Extracted(
        kind=KIND_SPREADSHEET,
        mime_type="",
        text=text,
        detail=f"{sheets} sheet{'s' if sheets != 1 else ''}",
        truncated=truncated or capped,
    )


def _delimited(data: bytes, max_chars: int, delimiter: str) -> Extracted:
    raw = _decode(data)
    lines: list[str] = []
    total = 0
    truncated = False
    for row in csv.reader(io.StringIO(raw), delimiter=delimiter):
        if total > max_chars:
            truncated = True
            break
        if not any(c.strip() for c in row):
            continue
        line = " | ".join(c.strip() for c in row)
        lines.append(line)
        total += len(line)
    text, capped = _cap("\n".join(lines), max_chars)
    return Extracted(
        kind=KIND_SPREADSHEET,
        mime_type="",
        text=text,
        detail=f"{len(lines)} rows",
        truncated=truncated or capped,
    )


_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx(data: bytes, max_chars: int) -> Extracted:
    from defusedxml import ElementTree as SafeET

    zf = _check_zip(data)
    slides = sorted(
        (int(m.group(1)), name)
        for name in zf.namelist()
        if (m := _SLIDE_RE.match(name))
    )
    parts: list[str] = []
    total = 0
    for number, name in slides:
        if total > max_chars:
            break
        root = SafeET.fromstring(zf.read(name))
        paras = []
        for p in root.iter(f"{_A_NS}p"):
            words = "".join(t.text or "" for t in p.iter(f"{_A_NS}t")).strip()
            if words:
                paras.append(words)
        if paras:
            body = "\n".join(paras)
            parts.append(f"--- Slide {number} ---\n{body}")
            total += len(body)
    text, truncated = _cap("\n\n".join(parts), max_chars)
    return Extracted(
        kind=KIND_PRESENTATION,
        mime_type="",
        text=text,
        detail=f"{len(slides)} slide{'s' if len(slides) != 1 else ''}",
        truncated=truncated,
    )


def pptx_text(data: bytes, max_chars: int = 2_000_000) -> tuple[str, int]:
    """A deck's slide text and slide count, for the Assets loader
    (``rubik_core/ingestion/loader.py``). Raises :class:`ChatFileError` on a
    damaged or oversized file."""
    out = _pptx(data, max_chars)
    slides = int((out.detail or "0").split()[0])
    return clean_text(out.text), slides


def _plain(data: bytes, max_chars: int) -> Extracted:
    text, truncated = _cap(_decode(data), max_chars)
    return Extracted(kind=KIND_TEXT, mime_type="", text=text, truncated=truncated)


def _image(data: bytes, suffix: str, mime: str) -> Extracted:
    """A copy of the picture sized for vision; the model reads it."""
    if suffix in {".heic", ".heif"}:
        if len(data) > HEIC_PASSTHROUGH_MAX_BYTES:
            raise ChatFileError(
                "That photo is too large to read. Export it as a JPG and attach that."
            )
        return Extracted(
            kind=KIND_IMAGE,
            mime_type=mime,
            needs_model=True,
            media=data,
            media_mime=mime,
        )
    media, media_mime, size = normalize_image(data)
    return Extracted(
        kind=KIND_IMAGE,
        mime_type=mime,
        detail=f"{size[0]}×{size[1]}",
        needs_model=True,
        media=media,
        media_mime=media_mime,
    )


def normalize_image(data: bytes) -> tuple[bytes, str, tuple[int, int]]:
    """Upright, at most ``IMAGE_MAX_EDGE`` on its long side, as JPEG (or PNG when
    it has transparency). Returns ``(bytes, mime, original_size)``."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as probe:
        width, height = probe.size
    if width * height > IMAGE_MAX_PIXELS:
        raise ChatFileError("That picture is too large to read. Attach a smaller one.")
    with Image.open(io.BytesIO(data)) as img:
        img.seek(0)  # an animated GIF: its first frame
        img = ImageOps.exif_transpose(img)
        img.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
        has_alpha = img.mode in {"RGBA", "LA"} or (
            img.mode == "P" and "transparency" in img.info
        )
        buf = io.BytesIO()
        if has_alpha:
            img.convert("RGBA").save(buf, format="PNG", optimize=True)
            return buf.getvalue(), "image/png", (width, height)
        img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg", (width, height)
