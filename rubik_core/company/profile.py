"""The company profile: who this workspace is, in its admin's words.

One record per workspace, stored in ``workspace_settings`` under
:data:`SETTINGS_KEY` (a key-value row, so no table of its own). It feeds two
kinds of reader:

* **Prompts.** Every prompt that needs to know what the company does includes
  :func:`company_context_block`. With no profile saved the block is empty and
  the prompt says "the company" instead, so an empty profile never reaches a
  model as "None", a blank name or a raw ``{company}`` token.
* **Documents.** The proposal letterhead reads the name, legal name, address
  and website (``rubik_core/proposals/brand.py``). The Deep Prep PDF and Word
  exports, the L10 recap email and the Scorecard and VTO upload templates carry
  the company's name (:func:`company_name_for_documents`), the product's name
  until an admin saves one.

Missing rows, malformed values and database errors give the empty profile.
An implicit read without a workspace raises instead of swallowing the missing
tenant context and returning a misleading profile.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, fields
from typing import Any

from sqlalchemy.orm import Session

from rubik_core.brand import PRODUCT_NAME
from rubik_core.tenancy.context import current_workspace

logger = logging.getLogger(__name__)

SETTINGS_KEY = "company_profile"
_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 256
_cache: dict[str, tuple[float, CompanyProfile]] = {}
_cache_lock = threading.Lock()
_cache_epoch: dict[str, int] = {}
_global_epoch = 0

#: Longest value kept per field. The profile is read into many prompts, so a
#: pasted page of marketing copy is cut rather than crowding out the call.
MAX_SHORT_CHARS = 200
MAX_LONG_CHARS = 1_000
MAX_PRODUCTS = 20

_LONG_FIELDS = frozenset({"address", "blurb", "offering", "customers", "sales_process"})


@dataclass(frozen=True)
class CompanyProfile:
    """What the company is called, where it is, and what it does.

    Every field is optional and defaults to empty. ``products`` holds the main
    products or services, one short line each.
    """

    name: str = ""
    legal_name: str = ""
    website: str = ""
    address: str = ""
    blurb: str = ""
    industry: str = ""
    offering: str = ""
    customers: str = ""
    sales_process: str = ""
    products: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any(getattr(self, f.name) for f in fields(self))


def _text(value: Any, limit: int) -> str:
    """A stripped string, or ``""`` for anything that is not text."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit].strip() if limit else value.strip()


def _multiline(value: Any, limit: int) -> str:
    """Like :func:`_text` but keeps line breaks (an address, a process)."""
    if not isinstance(value, str):
        return ""
    lines = [" ".join(line.split()) for line in value.strip().splitlines()]
    return "\n".join(line for line in lines if line)[:limit].strip()


def profile_from_dict(raw: Any) -> CompanyProfile:
    """Coerce a stored or submitted mapping into a clean :class:`CompanyProfile`.

    Unknown keys are ignored; a non-string value becomes empty. Never raises.
    """
    src = raw if isinstance(raw, dict) else {}
    values: dict[str, Any] = {}
    for f in fields(CompanyProfile):
        if f.name == "products":
            continue
        if f.name in _LONG_FIELDS:
            values[f.name] = _multiline(src.get(f.name), MAX_LONG_CHARS)
        else:
            values[f.name] = _text(src.get(f.name), MAX_SHORT_CHARS)
    given = src.get("products")
    items = given if isinstance(given, (list, tuple)) else []
    products = [_text(p, MAX_SHORT_CHARS) for p in items]
    values["products"] = tuple(dict.fromkeys(p for p in products if p))[:MAX_PRODUCTS]
    return CompanyProfile(**values)


def profile_to_dict(profile: CompanyProfile) -> dict[str, Any]:
    """The JSON shape stored and served: every field, products as a list."""
    out: dict[str, Any] = {f.name: getattr(profile, f.name) for f in fields(profile)}
    out["products"] = list(profile.products)
    return out


def _read(db: Session) -> CompanyProfile:
    from rubik_core.database.models import WorkspaceSetting

    row = (
        db.query(WorkspaceSetting).filter(WorkspaceSetting.key == SETTINGS_KEY).first()
    )
    return profile_from_dict(row.value if row is not None else None)


def clear_cache(slug: str | None = None) -> None:
    """Clear one workspace's profile, or all entries for test/process reset."""
    global _global_epoch
    with _cache_lock:
        if slug is None:
            _global_epoch += 1
            _cache.clear()
            _cache_epoch.clear()
        else:
            _cache.pop(slug, None)
            _cache_epoch[slug] = _cache_epoch.get(slug, 0) + 1


def get_company_profile(db: Session | None = None) -> CompanyProfile:
    """The saved profile, or the empty one on read failure.

    With no ``db`` it opens (and closes) its own session, so a prompt builder
    deep in a call stack can read the profile without threading a session
    through.
    """
    if db is not None:
        try:
            return _read(db)
        except Exception:  # noqa: BLE001 - an explicit DB may fail too
            logger.warning("could not read the company profile; using the empty one")
            return CompanyProfile()
    slug = current_workspace()  # a missing tenant scope must never be swallowed
    with _cache_lock:
        hit = _cache.get(slug)
        if hit is not None and hit[0] > time.monotonic():
            return hit[1]
        epoch = (_global_epoch, _cache_epoch.get(slug, 0))
    try:
        from rubik_core.database.db import SessionLocal

        with SessionLocal() as own:
            profile = _read(own)
    except Exception:  # noqa: BLE001 — a prompt must run without its profile
        logger.warning("could not read the company profile; using the empty one")
        return CompanyProfile()
    with _cache_lock:
        if (_global_epoch, _cache_epoch.get(slug, 0)) == epoch:
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                oldest = min(_cache, key=lambda key: _cache[key][0])
                _cache.pop(oldest, None)
            _cache[slug] = (time.monotonic() + _CACHE_TTL_SECONDS, profile)
    return profile


def save_company_profile(db: Session, profile: CompanyProfile) -> CompanyProfile:
    """Store ``profile`` (cleaned the same way a read cleans it) and return it."""
    from rubik_core.database.models import WorkspaceSetting

    slug = current_workspace()
    clean = profile_from_dict(profile_to_dict(profile))
    value = profile_to_dict(clean)
    row = (
        db.query(WorkspaceSetting).filter(WorkspaceSetting.key == SETTINGS_KEY).first()
    )
    if row is None:
        db.add(WorkspaceSetting(key=SETTINGS_KEY, value=value))
    else:
        row.value = value
    db.commit()
    clear_cache(slug)
    return clean


# ── Wording helpers for prompts and copy ─────────────────────────────────────


def company_name_or_default(
    profile: CompanyProfile, default: str = "the company"
) -> str:
    """The company's name, its legal name, or ``default`` when neither is set."""
    return profile.name or profile.legal_name or default


#: What UI copy addressed to the company's own people says when no profile is
#: saved: "outside your company", "your company's Workspace accounts".
YOUR_COMPANY = "your company"


def company_name_for_copy(db: Session | None = None) -> str:
    """The company's name for copy shown to its own people, or "your company".

    "outside Acme Corp" once an admin has saved the profile, "outside your
    company" before that. Never raises (see :func:`get_company_profile`).
    """
    return company_name_or_default(get_company_profile(db), YOUR_COMPANY)


def company_name_for_documents(profile: CompanyProfile | None = None) -> str:
    """The name a generated document carries in its header and footer.

    The company's name or legal name, else the product's name, so a document
    made before an admin fills in the profile still reads "Cubie — Confidential"
    and never "None — Confidential" or " — Confidential". With no ``profile``
    it reads the saved one (never raises; see :func:`get_company_profile`).
    """
    if profile is None:
        profile = get_company_profile()
    return company_name_or_default(profile, PRODUCT_NAME)


def company_label(profile: CompanyProfile, default: str = "the company") -> str:
    """The name with the industry when one is set, for a one-line introduction.

    ``"Acme Corp (Industrial cleaning)"``, ``"Acme Corp"``,
    ``"the company (Industrial cleaning)"`` or ``"the company"``.
    """
    name = company_name_or_default(profile, default)
    return f"{name} ({profile.industry})" if profile.industry else name


def company_possessive(profile: CompanyProfile, default: str = "the company") -> str:
    """``"Acme Corp's"`` or ``"the company's"``, for "X's AI assistant"."""
    name = company_name_or_default(profile, default)
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def company_context_block(profile: CompanyProfile) -> str:
    """A short plain-text description of the company for a prompt.

    ``""`` when the profile says nothing a prompt can use, so a caller can
    always concatenate it. Only the fields that are set appear, so the text
    never carries "None" or an empty label.
    """
    name = company_name_or_default(profile, "")
    lines = [
        ("Name", name),
        ("Industry", profile.industry),
        ("What it sells", profile.offering),
        ("Who it sells to", profile.customers),
        ("Main products or services", "; ".join(profile.products)),
        ("How it sells", profile.sales_process),
        ("In short", profile.blurb),
        ("Website", profile.website),
    ]
    body = [
        f"- {label}: {value.replace(chr(10), '; ')}" for label, value in lines if value
    ]
    if not body:
        return ""
    return (
        "ABOUT THE COMPANY (the company you work for, as its admin describes it)\n"
        + "\n".join(body)
        + "\n"
    )
