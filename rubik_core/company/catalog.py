"""The product and service catalog: parse an upload, replace, list.

An admin uploads a CSV with one simple, generic shape (spec §4.2.5)::

    name,description,category,price,unit,url

Only ``name`` is required; ``price`` and ``unit`` are optional, so a service
priced per engagement leaves them blank. Column order does not matter and
unknown columns are ignored. An upload REPLACES the whole catalog, and only
when the file has no errors: a file with one bad row changes nothing, so a
half-applied catalog never reaches a proposal.

Errors name the row the admin sees in a spreadsheet: the header is row 1, the
first item row 2 (``"row 2: name is required"``).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from rubik_core.database.models import CatalogItem

HEADER: tuple[str, ...] = ("name", "description", "category", "price", "unit", "url")

MAX_ROWS = 2_000
MAX_NAME_CHARS = 200
MAX_DESCRIPTION_CHARS = 2_000
MAX_CATEGORY_CHARS = 120
MAX_UNIT_CHARS = 40
MAX_URL_CHARS = 500
#: A price above this is a typo (an extra zero, a pasted id), not a price. It
#: also has to fit ``catalog_items.price_cents``, a 32-bit integer (at most
#: 2,147,483,647 cents): $20,000,000 is 2,000,000,000 cents.
MAX_PRICE = Decimal("20000000")

# Currency marks and thousands separators a spreadsheet adds to a number.
_PRICE_NOISE = re.compile(r"[\s,$€£]|^(?:USD|EUR|GBP)|(?:USD|EUR|GBP)$", re.I)


@dataclass(frozen=True)
class CatalogRow:
    """One product or service, as the CSV and the API carry it."""

    name: str
    description: str = ""
    category: str = ""
    price: Decimal | None = None
    unit: str = ""
    url: str = ""


def _cell(row: dict[str, str | None], column: str) -> str:
    return " ".join((row.get(column) or "").split())


class _PriceOverCap(ValueError):
    """A real number, just above ``MAX_PRICE``: told apart from "not a number"."""


def _parse_price(text: str) -> Decimal | None:
    """``"$1,200.50"`` → ``Decimal("1200.50")``; blank → ``None``; else raises.

    Raises ``_PriceOverCap`` for a number above ``MAX_PRICE`` and
    ``InvalidOperation`` for anything that is not a non-negative number.
    """
    cleaned = _PRICE_NOISE.sub("", text)
    if not cleaned:
        return None
    value = Decimal(cleaned)  # InvalidOperation on anything else
    if not value.is_finite() or value < 0:
        raise InvalidOperation(text)
    if value > MAX_PRICE:
        raise _PriceOverCap(text)
    return value.quantize(Decimal("0.01"))


def _parse_row(row: dict[str, str | None], line: int) -> tuple[CatalogRow | None, str]:
    """One data row → ``(CatalogRow, "")`` or ``(None, "row N: …")``."""
    name = _cell(row, "name")
    if not name:
        return None, f"row {line}: name is required"
    if len(name) > MAX_NAME_CHARS:
        return None, f"row {line}: name is longer than {MAX_NAME_CHARS} characters"
    raw_price = _cell(row, "price")
    try:
        price = _parse_price(raw_price)
    except _PriceOverCap:
        return None, f'row {line}: price "{raw_price}" is over the {MAX_PRICE:,} limit'
    except (InvalidOperation, ValueError):
        return None, f'row {line}: price "{raw_price}" is not a number'
    url = _cell(row, "url")
    if url and not re.match(r"^https?://", url, re.I):
        return None, f"row {line}: url must start with http:// or https://"
    if len(url) > MAX_URL_CHARS:
        return None, f"row {line}: url is longer than {MAX_URL_CHARS} characters"
    return (
        CatalogRow(
            name=name,
            description=_cell(row, "description")[:MAX_DESCRIPTION_CHARS],
            category=_cell(row, "category")[:MAX_CATEGORY_CHARS],
            price=price,
            unit=_cell(row, "unit")[:MAX_UNIT_CHARS],
            url=url,
        ),
        "",
    )


def parse_catalog_csv(text: str) -> tuple[list[CatalogRow], list[str]]:
    """Parse an uploaded catalog. Returns ``(rows, errors)``.

    ``errors`` is empty for a clean file. Rows with an error are left out of
    ``rows``, but callers replace the catalog only when ``errors`` is empty.
    """
    reader = csv.DictReader(io.StringIO((text or "").lstrip("﻿")))
    if reader.fieldnames is None:
        return [], ["row 1: the file is empty; the first row must be the header"]
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
    if "name" not in reader.fieldnames:
        return [], [
            "row 1: the header must include a name column "
            f"(expected {','.join(HEADER)})"
        ]

    rows: list[CatalogRow] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    for record in reader:
        line = reader.line_num
        if all(not (v or "").strip() for v in record.values() if isinstance(v, str)):
            continue  # a blank line a spreadsheet left at the end
        if len(rows) + len(errors) >= MAX_ROWS:
            errors.append(f"the file has more than {MAX_ROWS} rows")
            break
        parsed, error = _parse_row(record, line)
        if parsed is None:
            errors.append(error)
            continue
        folded = parsed.name.casefold()
        if folded in seen:
            errors.append(
                f'row {line}: "{parsed.name}" is already on row {seen[folded]}; '
                "names must be unique"
            )
            continue
        seen[folded] = line
        rows.append(parsed)
    return rows, errors


def _to_row(item: CatalogItem) -> CatalogRow:
    price = (
        (Decimal(item.price_cents) / 100).quantize(Decimal("0.01"))
        if item.price_cents is not None
        else None
    )
    return CatalogRow(
        name=item.name,
        description=item.description or "",
        category=item.category or "",
        price=price,
        unit=item.unit or "",
        url=item.url or "",
    )


def replace_catalog(db: Session, rows: list[CatalogRow]) -> int:
    """Swap the whole catalog for ``rows`` in one transaction. Returns the count."""
    db.query(CatalogItem).delete(synchronize_session=False)
    for position, row in enumerate(rows):
        db.add(
            CatalogItem(
                position=position,
                name=row.name,
                description=row.description,
                category=row.category,
                price_cents=(
                    int((row.price * 100).to_integral_value())
                    if row.price is not None
                    else None
                ),
                unit=row.unit,
                url=row.url,
            )
        )
    db.commit()
    return len(rows)


def list_catalog(db: Session) -> list[CatalogRow]:
    """The catalog in upload order. Empty until an admin uploads one."""
    items = db.query(CatalogItem).order_by(CatalogItem.position, CatalogItem.id).all()
    return [_to_row(item) for item in items]


def rows_named(db: Session, names: list[str]) -> list[CatalogRow]:
    """The catalog rows called ``names`` (case-insensitive), in ``names`` order.

    A name with no row is skipped: a template may outlive a catalog upload
    that dropped one of its rows.
    """
    by_name = {row.name.casefold(): row for row in list_catalog(db)}
    found = (by_name.get((n or "").strip().casefold()) for n in names)
    return [row for row in found if row is not None]


def price_label(row: CatalogRow) -> str:
    """``"$1,200"``, ``"$90.50/hour"``, or ``""`` when the row has no price."""
    if row.price is None:
        return ""
    amount = (
        f"${int(row.price):,}"
        if row.price == row.price.to_integral_value()
        else f"${row.price:,.2f}"
    )
    return f"{amount}/{row.unit}" if row.unit else amount


def row_to_dict(row: CatalogRow) -> dict[str, str | None]:
    """The API shape: the price as a plain decimal string, or ``None``."""
    return {
        "name": row.name,
        "description": row.description,
        "category": row.category,
        "price": None if row.price is None else f"{row.price:.2f}",
        "unit": row.unit,
        "url": row.url,
        "price_label": price_label(row),
    }
