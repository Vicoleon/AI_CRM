"""Shared lexical retrieval behind every product-help corpus.

WHY THIS FILE EXISTS
--------------------
Cubie documents itself in chat (:mod:`rubik_core.help.content`): it answers
"how do I…" questions by scoring a fixed set of hand-written sections against
the question and handing the winners to the coordinator. The scorer lives here,
apart from the corpus, so a second product's Help page can reuse it instead of
copying it.

Deliberately lexical, not embeddings: a corpus is a few dozen fixed sections,
so a scored token match is instant, free, and — unlike a vector call — can't
fail mid-turn. The coordinator does the actual answering; this only decides
which sections it reads.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HelpSection:
    """One section of a product's Help page.

    ``id`` and ``title`` mirror the corresponding section in the Help page's own
    source exactly — for Cubie's own Help that parity is enforced by test.
    ``requires`` names an access gate — the section is withheld from users who
    can't reach the feature, matching how the Help page itself hides it.
    """

    id: str
    title: str
    body: str
    keywords: tuple[str, ...] = field(default=())
    requires: str | None = None


# Words that carry no signal here. The product's own name is in nearly every
# section, so "…in Cubie?" must not move the answer, and the question shell
# ("how do I…", "where is…") matches everything.
_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "cubie",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "to",
        "up",
        "use",
        "want",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-/]*")

# Plurals that lose "es", not just "s" ("processes" → "process").
_PLURAL_ES = ("ses", "xes", "zes", "ches", "shes")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _stem(token: str) -> str:
    """Fold the endings that would otherwise miss a plain-English question.

    Crude on purpose — enough that "priority" finds "Priorities" and "marking"
    finds "mark", while short words that matter here ("eos", "ids") are left
    alone. Applied to both sides, so consistency beats linguistic correctness.
    """
    for _ in range(2):
        if len(token) > 4 and token.endswith("ies"):
            token = token[:-3] + "y"
            continue
        if len(token) > 5 and token.endswith(_PLURAL_ES):
            token = token[:-2]
            continue
        if len(token) > 5 and token.endswith("ing"):
            token = token[:-3]
            continue
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
            continue
        break
    return token


def _stems(text: str) -> list[str]:
    return [_stem(t) for t in _tokens(text)]


def _query_terms(query: str) -> list[str]:
    """Meaningful, stemmed search terms from a user question."""
    seen: list[str] = []
    for raw in _tokens(query):
        if len(raw) < 2 or raw in _STOPWORDS:
            continue
        term = _stem(raw)
        if term not in seen:
            seen.append(term)
    return seen


class _SectionIndex:
    """Pre-tokenized section, built once at import (15 fixed sections)."""

    __slots__ = (
        "title_tokens",
        "keyword_tokens",
        "keyword_blob",
        "body_lower",
        "body_counts",
    )

    def __init__(self, section: HelpSection):
        self.title_tokens = frozenset(_stems(section.title))
        self.keyword_blob = " ".join(section.keywords).lower()
        self.keyword_tokens = frozenset(_stems(self.keyword_blob))
        self.body_lower = section.body.lower()
        counts: dict[str, int] = {}
        for t in _stems(self.body_lower):
            counts[t] = counts.get(t, 0) + 1
        self.body_counts = counts


def _score(
    section: HelpSection,
    index: _SectionIndex,
    terms: list[str],
    normalized_query: str,
) -> tuple[int, int, bool]:
    """Score ``section`` against the query.

    Returns ``(score, matched_terms, strong)``. ``strong`` means the match landed
    on the title, a keyword, or a whole phrase — a deliberate signal rather than
    a word that happens to appear in the prose.
    """
    title_tokens = index.title_tokens
    keyword_tokens = index.keyword_tokens
    keyword_blob = index.keyword_blob
    body_lower = index.body_lower
    body_counts = index.body_counts

    score = 0
    matched_terms = 0
    strong = False
    for term in terms:
        term_score = 0
        if term in title_tokens:
            term_score += 6
            strong = True
        if term in keyword_tokens:
            term_score += 4
            strong = True
        hits = body_counts.get(term, 0)
        if hits:
            term_score += min(hits, 3)
        if term_score:
            matched_terms += 1
            score += term_score

    # A multi-word phrase lifted straight out of the page ("knowledge base",
    # "focus block") is a much stronger signal than its words apart.
    if len(normalized_query) >= 6:
        if normalized_query in section.title.lower():
            score += 10
            strong = True
        elif normalized_query in body_lower or normalized_query in keyword_blob:
            score += 8
            strong = True

    return score, matched_terms, strong


class HelpIndex:
    """One corpus of help sections, pre-tokenized once at import.

    Each product's help module builds exactly one of these at module scope, so
    the tokenizing cost is paid at import and every chat turn is a dictionary
    lookup and a loop over a dozen sections.
    """

    __slots__ = ("sections", "_by_id", "_index")

    def __init__(self, sections: Sequence[HelpSection]):
        self.sections: tuple[HelpSection, ...] = tuple(sections)
        self._by_id = {s.id: s for s in self.sections}
        self._index = {s.id: _SectionIndex(s) for s in self.sections}

    def get(self, section_id: str) -> HelpSection | None:
        """The section with ``section_id``, or ``None``."""
        return self._by_id.get(section_id)

    def search(
        self,
        query: str,
        *,
        top_k: int = 3,
        allowed_ids: Collection[str] | None = None,
    ) -> list[HelpSection]:
        """Return the sections most likely to answer ``query``, best first.

        ``allowed_ids`` restricts the result to sections the caller may see (the
        gated features); ``None`` means no restriction. An empty list means
        nothing matched — callers should offer the section directory rather than
        guess.
        """
        pool = [s for s in self.sections if allowed_ids is None or s.id in allowed_ids]
        terms = _query_terms(query or "")
        if not terms:
            return []

        normalized_query = " ".join(_tokens(query or ""))
        hits: list[tuple[int, int, HelpSection]] = []
        for i, section in enumerate(pool):
            score, matched_terms, strong = _score(
                section, self._index[section.id], terms, normalized_query
            )
            # One incidental word of prose is not a match — "what's the weather
            # tomorrow?" hits "snooze it (tomorrow…)" and would otherwise drag
            # the briefing section back as an answer. Require a deliberate
            # signal (title / keyword / phrase) or agreement across two terms.
            if score > 0 and (strong or matched_terms >= 2):
                hits.append((score, i, section))
        hits.sort(key=lambda t: (-t[0], t[1]))
        return [s for _, _, s in hits[: max(1, top_k)]]


def render_sections(sections: Iterable[HelpSection], link: object) -> str:
    """Format sections for the agent: content plus the link to cite.

    ``link`` is the corpus's own section-to-URL function, so each product cites
    its own Help page.
    """
    blocks = [f"### {s.title}\n{s.body}\n\nHelp link: {link(s)}" for s in sections]
    return "\n\n---\n\n".join(blocks)


def render_directory(sections: Iterable[HelpSection], link: object) -> str:
    """A one-line-per-section index, for when nothing matched the query."""
    return "\n".join(f"- {s.title} — {link(s)}" for s in sections)
