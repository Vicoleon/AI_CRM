"""Proposal templates — the workspace's own service write-ups.

A template is a named write-up of one service (``body_md``) plus, optionally,
the names of catalog rows that price it (spec §4.2.5). Admins write them in
Admin → Company setup; the table ships empty, so a new workspace's proposals
are built from the AI-written sections alone until somebody adds one.

How a template meets a proposal:

* The brief carries ``template = {"key": ...}``. The brief prompt lists the
  active templates (:func:`prompt_menu`) and the model suggests one from the
  call; the AE can change it on the brief form.
* The TAILORED part of the document (prepared for, executive summary, scope,
  desired outcomes, pricing, notes) is unchanged: the model writes it from the
  confirmed brief and the fence in ``brief.py`` still holds.
* The template's body prints after the tailored scope and outcomes, before
  pricing, in the admin's words. The model never sees or edits it; the draft
  prompt gets its headings only, so it does not repeat them.
* PRICES NEVER COME FROM PROSE. A template's catalog rows become pricing lines
  on the brief (:func:`pricing_lines`), and the renderer prints the table from
  the brief. A figure an admin typed into ``body_md`` prints as written, the
  way any of their own copy does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from rubik_core.company.catalog import price_label, rows_named
from rubik_core.database.models import ProposalTemplate as TemplateRow

MAX_NAME_CHARS = 200
MAX_BODY_CHARS = 20_000
MAX_CATALOG_ITEMS = 20
MAX_KEY_CHARS = 64

_KEY_SHAPE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


class TemplateError(ValueError):
    """A template the admin submitted cannot be saved; the message says why."""


@dataclass(frozen=True)
class Template:
    id: int
    #: Stable slug the brief stores. Derived from the name on create.
    key: str
    name: str
    body_md: str
    catalog_item_names: tuple[str, ...]
    active: bool


def _from_row(row: TemplateRow) -> Template:
    names = row.catalog_item_names if isinstance(row.catalog_item_names, list) else []
    return Template(
        id=row.id,
        key=row.key,
        name=row.name,
        body_md=row.body_md or "",
        catalog_item_names=tuple(str(n) for n in names if str(n).strip()),
        active=bool(row.active),
    )


def template_to_dict(t: Template) -> dict[str, Any]:
    """The admin API shape."""
    return {
        "id": t.id,
        "key": t.key,
        "name": t.name,
        "body_md": t.body_md,
        "catalog_item_names": list(t.catalog_item_names),
        "active": t.active,
    }


def is_key(value: Any) -> bool:
    """Whether ``value`` has the shape of a template key (not whether one exists)."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_KEY_CHARS
        and bool(_KEY_SHAPE.match(value))
    )


# ── Reads ────────────────────────────────────────────────────────────────────


def list_templates(db: Session, *, active_only: bool = False) -> list[Template]:
    query = db.query(TemplateRow)
    if active_only:
        query = query.filter(TemplateRow.active.is_(True))
    return [_from_row(r) for r in query.order_by(TemplateRow.name, TemplateRow.id)]


def _find(db: Session, key: str | None, *, active_only: bool) -> Template | None:
    if not is_key(key):
        return None
    query = db.query(TemplateRow).filter(TemplateRow.key == key)
    if active_only:
        query = query.filter(TemplateRow.active.is_(True))
    row = query.first()
    return _from_row(row) if row is not None else None


def get(db: Session, key: str | None) -> Template | None:
    """The ACTIVE template with ``key``, or ``None``.

    For choosing a template: the brief prompt's menu and the check that clears
    a key the model invented or an admin has retired (``service._apply_template``).
    """
    return _find(db, key, active_only=True)


def get_by_key(db: Session, key: str | None) -> Template | None:
    """The template with ``key``, active or retired, or ``None`` once deleted.

    For a proposal that already names it: drafting a version and rendering the
    download. Unticking "Offer it on new proposals" only stops new proposals
    from picking the template; one that already carries it keeps printing its
    write-up. Deleting the template is what removes it from them.
    """
    return _find(db, key, active_only=False)


def get_by_id(db: Session, template_id: int) -> Template | None:
    row = db.get(TemplateRow, template_id)
    return _from_row(row) if row is not None else None


# ── Writes (Admin → Company setup) ───────────────────────────────────────────


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[: MAX_KEY_CHARS - 4]
    return slug or "template"


def _unique_key(db: Session, name: str) -> str:
    base = _slug(name)
    taken = {k for (k,) in db.query(TemplateRow.key).all()}
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _clean(payload: Mapping[str, Any], *, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "name" in payload or not partial:
        name = " ".join(str(payload.get("name") or "").split())
        if not name:
            raise TemplateError("A template needs a name.")
        if len(name) > MAX_NAME_CHARS:
            raise TemplateError(f"Keep the name under {MAX_NAME_CHARS} characters.")
        out["name"] = name
    if "body_md" in payload or not partial:
        body = str(payload.get("body_md") or "").strip()
        if len(body) > MAX_BODY_CHARS:
            raise TemplateError(f"Keep the text under {MAX_BODY_CHARS} characters.")
        out["body_md"] = body
    if "catalog_item_names" in payload or not partial:
        raw = payload.get("catalog_item_names") or []
        if not isinstance(raw, (list, tuple)):
            raise TemplateError("catalog_item_names must be a list of names.")
        names = [" ".join(str(n).split()) for n in raw]
        names = list(dict.fromkeys(n for n in names if n))
        if len(names) > MAX_CATALOG_ITEMS:
            raise TemplateError(
                f"A template can price at most {MAX_CATALOG_ITEMS} catalog items."
            )
        out["catalog_item_names"] = names
    if "active" in payload or not partial:
        out["active"] = bool(payload.get("active", True))
    return out


def create_template(db: Session, payload: Mapping[str, Any]) -> Template:
    values = _clean(payload, partial=False)
    row = TemplateRow(key=_unique_key(db, values["name"]), **values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _from_row(row)


def update_template(
    db: Session, template_id: int, payload: Mapping[str, Any]
) -> Template | None:
    """Apply the fields present in ``payload``. The key never changes, so a
    brief that names this template keeps finding it after a rename."""
    row = db.get(TemplateRow, template_id)
    if row is None:
        return None
    for field_name, value in _clean(payload, partial=True).items():
        setattr(row, field_name, value)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _from_row(row)


def delete_template(db: Session, template_id: int) -> bool:
    row = db.get(TemplateRow, template_id)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# ── Prompts, pricing and the brief form ──────────────────────────────────────


def prompt_menu(templates: list[Template]) -> str:
    """The template list the brief prompt chooses from, one per line."""
    return "\n".join(f'- "{t.key}": {t.name}' for t in templates)


def price_options(db: Session, t: Template) -> list[dict[str, str]]:
    """The template's catalog rows that carry a price, as brief-form chips."""
    return [
        {"description": row.name, "standard_rate": price_label(row)}
        for row in rows_named(db, list(t.catalog_item_names))
        if row.price is not None
    ]


def pricing_lines(db: Session, t: Template) -> list[dict[str, Any]]:
    """Pricing lines for the brief, one per priced catalog row of ``t``.

    The list price goes in both columns; the AE lowers the proposal price to
    offer a discount, and the renderer prints a standard-rate column only when
    the two differ. ``confirmed`` stays false until the AE confirms the brief.
    """
    return [
        {
            "description": option["description"],
            "standard_rate": option["standard_rate"],
            "proposal_price": option["standard_rate"],
            "confirmed": False,
        }
        for option in price_options(db, t)
    ]


def as_api(db: Session, t: Template) -> dict[str, Any]:
    """The listing the brief form reads (``GET /api/proposals/templates``)."""
    return {
        "key": t.key,
        "name": t.name,
        "headings": [b.text for b in body_blocks(t.body_md) if b.kind == "heading"],
        "price_options": price_options(db, t),
    }


# ── The body ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Block:
    kind: Literal["heading", "paragraph", "bullet"]
    text: str


def body_blocks(body_md: str) -> list[Block]:
    """``body_md`` as headings, paragraphs and bullets, for both renderers.

    A small subset of Markdown, which is all a service write-up needs: a line
    starting ``#`` is a heading, ``-``/``*``/``1.`` a bullet, and a blank line
    ends a paragraph. Emphasis markers are left in; each renderer strips them.
    """
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Block("paragraph", " ".join(paragraph)))
            paragraph.clear()

    for raw in (body_md or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        bullet = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)$", line)
        if heading:
            flush()
            if heading.group(1).strip():
                blocks.append(Block("heading", heading.group(1).strip()))
        elif bullet:
            flush()
            if bullet.group(1).strip():
                blocks.append(Block("bullet", bullet.group(1).strip()))
        else:
            paragraph.append(line)
    flush()
    return blocks


def printed_blocks(t: Template) -> list[Block]:
    """What the renderers print for ``t``: its body, under its name when the
    body does not open with a heading of its own."""
    blocks = body_blocks(t.body_md)
    if not blocks:
        return []
    if blocks[0].kind != "heading":
        blocks.insert(0, Block("heading", t.name))
    return blocks
