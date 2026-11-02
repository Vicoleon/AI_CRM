"""Help ↔ Chat linking: section parity, retrieval, gating, and tool wiring.

Two directions are under test:
  Help → Chat  — every example question in Help is a `/chat?q=` link.
  Chat → Help  — `search_cubie_help` answers "how do I…" from the same content
                 and hands back a `/help/<topic>` link to that topic's page.

The parity test is the guard against the failure mode that would quietly break
the second direction: someone adds or renames a Help topic in
``frontend/src/pages/help/helpTopics.ts`` for a new feature and the backend
mirror in ``rubik_core/help/content.py`` never learns about it, so Chat keeps
answering from a stale page.

The product is Cubie. The backend mirror, the retrieval package behind it and
the renamed topic ids and anchors carry no old product or company name
(``_legacy_brand.LEGACY_PATTERN``), and naming Cubie in a question does not
change which sections answer it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.tests._legacy_brand import LEGACY_PATTERN, scan_repo
from rubik_core.adk_agent.tools import AgentTools
from rubik_core.agent_runtime import coordinator
from rubik_core.help import (
    HELP_PAGE_PATH,
    HELP_SECTIONS,
    help_link,
    render_help_report,
    search_help,
    section_directory,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELP_DIR = _REPO_ROOT / "frontend" / "src" / "pages" / "help"
_HELP_TOPICS_TS = _HELP_DIR / "helpTopics.ts"
_TOPIC_PAGES_TSX = _HELP_DIR / "topicPages.tsx"
_HELP_HOME_TSX = _REPO_ROOT / "frontend" / "src" / "pages" / "Help.tsx"
_KIT_TSX = _HELP_DIR / "kit.tsx"

# Each topic in HELP_TOPICS is one object literal opening on `id: '<id>',`;
# the fields we compare are single-quoted strings (helpTopics.ts says so).
_TOPIC_START_RE = re.compile(r"^\s*id: '([^']+)',$", re.M)


def _field(block: str, name: str) -> str | None:
    m = re.search(rf"^\s*{name}: '((?:[^'\\]|\\.)*)',$", block, re.M)
    if m:
        return m.group(1).replace("\\'", "'")
    # A field written in another shape must fail loudly, not read as absent —
    # an unread `requires` would pass the gate check as "ungated".
    assert not re.search(rf"\b{name}\s*:", block), (
        f"unparseable `{name}:` in helpTopics.ts block: {block[:80]!r}; "
        f"write it as `{name}: '<value>',` on its own line"
    )
    return None


def _help_topics() -> dict[str, dict[str, str | None]]:
    """id → {title, requires} for every topic in helpTopics.ts, in order."""
    source = _HELP_TOPICS_TS.read_text(encoding="utf-8")
    body = source.split("export const HELP_TOPICS", 1)[1].split("\n];", 1)[0]
    starts = list(_TOPIC_START_RE.finditer(body))
    assert len(starts) == len(re.findall(r"\bid\s*:", body)), (
        "helpTopics.ts has a topic whose `id:` line isn't `id: '<id>',` on its "
        "own line; the parity parser can't see it"
    )
    topics: dict[str, dict[str, str | None]] = {}
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(body)
        block = body[m.start() : end]
        topics[m.group(1)] = {
            "title": _field(block, "title"),
            "requires": _field(block, "requires"),
        }
    return topics


def _help_page_sections() -> dict[str, str | None]:
    return {tid: t["title"] for tid, t in _help_topics().items()}


requires_frontend = pytest.mark.skipif(
    not _HELP_TOPICS_TS.exists(),
    reason="frontend not present (backend-only container); parity runs in CI.",
)


# ── Chat → Help: the two copies stay in step ─────────────────────────────────


@requires_frontend
def test_every_help_page_section_has_backend_content():
    """A new Help section must be mirrored for the agent, or Chat can't answer
    about the feature it documents."""
    page = _help_page_sections()
    assert len(page) >= 20, (
        f"parsed only {len(page)} topics from helpTopics.ts — the parser needs updating"
    )

    mirrored = {s.id for s in HELP_SECTIONS}
    missing = sorted(set(page) - mirrored)
    assert not missing, (
        f"Help topics with no entry in rubik_core/help/content.py: {missing}. "
        "Add them so Chat can answer about these features."
    )


@requires_frontend
def test_backend_content_has_no_sections_the_page_dropped():
    """The mirror must not keep describing a section the page has removed."""
    page = _help_page_sections()
    stale = sorted({s.id for s in HELP_SECTIONS} - set(page))
    assert not stale, (
        f"content.py describes topics no longer in helpTopics.ts: {stale}. "
        "Remove them so Chat stops citing a page that isn't there."
    )


@requires_frontend
def test_section_titles_match_the_page():
    """Titles are what Chat shows the user, so a rename must travel."""
    page = _help_page_sections()
    mismatched = {
        s.id: (page[s.id], s.title)
        for s in HELP_SECTIONS
        if s.id in page and page[s.id] != s.title
    }
    assert not mismatched, f"title drift (page, backend): {mismatched}"


@requires_frontend
def test_section_gates_match_the_page():
    """A topic the Help panel hides from a reader must be one Chat won't cite
    to them, and the reverse — so both copies name the same gate."""
    page = _help_topics()
    drift = {
        s.id: (page[s.id]["requires"], s.requires)
        for s in HELP_SECTIONS
        if s.id in page and page[s.id]["requires"] != s.requires
    }
    assert not drift, f"gate drift (page, backend): {drift}"


@requires_frontend
def test_every_topic_has_a_page():
    """A topic in the registry with no page body would list in the panel and
    then show "couldn't find that help page"."""
    source = _TOPIC_PAGES_TSX.read_text(encoding="utf-8")
    pages = set(re.findall(r"^\s*'?([a-z-]+)'?: \{ Page: ", source, re.M))
    missing = sorted(set(_help_topics()) - pages)
    assert not missing, f"topics with no entry in topicPages.tsx: {missing}"


def test_every_section_has_real_content():
    for section in HELP_SECTIONS:
        assert len(section.body) > 200, f"{section.id} body looks like a stub"
        assert section.keywords, f"{section.id} has no keywords to match on"


def test_help_links_point_at_the_topic_page():
    for section in HELP_SECTIONS:
        assert help_link(section) == f"{HELP_PAGE_PATH}/{section.id}"


# ── The product is Cubie, in both homes ──────────────────────────────────────


def _legacy_hits(text: str) -> list[str]:
    return [m.group() for m in LEGACY_PATTERN.finditer(text)]


def test_help_mirror_names_no_old_product_or_company():
    """What Chat reads back to people says Cubie and "your company". That rules
    out the old "you may still see the old name" sentence too: there is no old
    name left to explain."""
    found = {}
    for section in HELP_SECTIONS:
        text = "\n".join([section.id, section.title, section.body, *section.keywords])
        if section_hits := _legacy_hits(text):
            found[section.id] = section_hits
    assert not found, f"old product or company names in content.py: {found}"


def test_help_package_names_no_old_product_or_company():
    """The retrieval package too: no old-name stopwords, docstrings or labels."""
    hits = scan_repo(_REPO_ROOT / "rubik_core" / "help")
    assert hits == [], hits


def test_the_chrome_topic_is_cubie_for_chrome():
    by_id = {s.id: s for s in HELP_SECTIONS}
    assert "cubie-for-chrome" in by_id, sorted(by_id)
    assert by_id["cubie-for-chrome"].title == "Cubie for Chrome"
    assert by_id["cubie-for-chrome"].requires == "chrome_extension"


@requires_frontend
@pytest.mark.parametrize(
    ("relative_path", "slug_source"),
    [
        ("pages/help/topicPages.tsx", "'cubie-for-chrome': { Page: CubieForChrome,"),
        ("pages/help/topics/CubieForChrome.tsx", "export const CubieForChrome"),
        ("pages/help/topics/LooseEnds.tsx", 'id="what-cubie-reads"'),
        ("pages/help/topics/MyTodos.tsx", 'id="cubie-heard-you-promise"'),
        # Chat's starter prompts for "how do I…" questions, answered from Help.
        ("pages/chatSuggestions.ts", "id: 'cubie-how-to',"),
    ],
)
def test_renamed_help_slugs(relative_path: str, slug_source: str):
    """Topic ids, page anchors and the how-to prompt group carry the new name."""
    path = _REPO_ROOT / "frontend" / "src" / relative_path
    assert path.exists(), f"{relative_path} is missing"
    assert slug_source in path.read_text(encoding="utf-8"), (
        f"{relative_path} does not contain {slug_source!r}"
    )


# ── Retrieval picks the section a real question is about ─────────────────────


@pytest.mark.parametrize(
    ("question", "expected_id"),
    [
        ("How do I add a document to the knowledge base?", "assets"),
        ("Where do I connect Slack?", "settings"),
        ("Can Cubie send emails for me?", "chat"),
        ("What does marking a priority done do?", "daily-briefing"),
        ("How do I import scorecard numbers?", "eos"),
        ("Who can see an L10 meeting?", "admins"),
        ("How do I generate meeting prep?", "meeting-prep"),
        ("Why can't it tell me our churn rate?", "crm"),
        ("How do I replay the page tour?", "getting-started"),
        ("How do I turn off the page tours?", "getting-started"),
        ("What's new in Cubie?", "whats-new"),
        ("How do I hide the spotlight bar?", "whats-new"),
        ("Where is the changelog?", "whats-new"),
    ],
)
def test_search_help_finds_the_right_section(question: str, expected_id: str):
    hits = search_help(question, top_k=3)
    assert hits, f"no Help section matched {question!r}"
    assert expected_id in {s.id for s in hits[:3]}, (
        f"{question!r} → {[s.id for s in hits]}, expected {expected_id}"
    )


@pytest.mark.parametrize(
    "question",
    [
        "What loose ends do I have?",
        "Why is my email not logged in HubSpot?",
        "open loops over 7 days",
    ],
)
def test_loose_ends_questions_go_to_the_loose_ends_section_first(question: str):
    hits = search_help(question, top_k=3)
    assert hits and hits[0].id == "loose-ends", [s.id for s in hits]


def test_what_am_i_waiting_on_stays_with_chat_after_loose_ends():
    """The live inbox sweep is a chat feature; Loose Ends' keywords must not
    pull its question away (they deliberately leave out "waiting")."""
    hits = search_help("what am I waiting on", top_k=3)
    assert hits and hits[0].id == "chat", [s.id for s in hits]


@pytest.mark.parametrize(
    ("plain", "named"),
    [
        ("Can it send emails for me?", "Can Cubie send emails for me?"),
        (
            "What does marking a priority done do?",
            "What does marking a priority done do in Cubie?",
        ),
        (
            "How do I import scorecard numbers?",
            "How do I import scorecard numbers into Cubie?",
        ),
        ("Where do I connect Slack?", "Where do I connect Slack to Cubie?"),
        ("Who can see an L10 meeting?", "Who can see an L10 meeting in Cubie?"),
    ],
)
def test_naming_the_product_does_not_move_the_answer(plain: str, named: str):
    """Nearly every section names the product, so its name carries no signal —
    the reason the old names were stopwords. Asking "…in Cubie?" must pick the
    same sections, in the same order, as asking without it."""
    assert [s.id for s in search_help(named)] == [s.id for s in search_help(plain)]


def test_search_help_returns_nothing_for_an_unrelated_question():
    """No match must stay empty so the tool offers the directory instead of
    handing the model an arbitrary section to answer from."""
    assert search_help("what is the weather in Dallas tomorrow") == []


def test_search_help_respects_allowed_ids():
    """Gated sections are withheld exactly as the Help page hides them."""
    hits = search_help("customer x-ray churn score", top_k=5)
    assert "customer-xray" in {s.id for s in hits}

    gated = search_help(
        "customer x-ray churn score",
        top_k=5,
        allowed_ids={s.id for s in HELP_SECTIONS if not s.requires},
    )
    assert "customer-xray" not in {s.id for s in gated}


def test_gated_sections_are_marked():
    by_id = {s.id: s for s in HELP_SECTIONS}
    assert by_id["customer-xray"].requires == "xray"
    assert by_id["crm-sync"].requires == "crm_sync"


def test_every_gate_name_resolves_to_a_gate():
    """A ``requires`` value with no entry in the tool's ``gates`` dict is
    withheld from everyone, silently — the section exists and nobody can be
    told about it. Each name a section uses must be one the tool resolves."""
    src = inspect.getsource(AgentTools._help_allowed_section_ids)
    gates_src = src.split("gates = {", 1)[1].split("}", 1)[0]
    resolvable = set(re.findall(r'^\s*"([a-z_]+)":', gates_src, re.M))
    used = {s.requires for s in HELP_SECTIONS if s.requires}
    assert used <= resolvable, f"gates with no resolver: {sorted(used - resolvable)}"


# ── "persona" is the sales-rhythm checklist's word ───────────────────────────


def test_bare_persona_reaches_the_sales_rhythm_note():
    """ "persona" is a sales-rhythm keyword (the daily checklist reads Buying
    role in place of Persona)."""
    ungated = {s.id for s in HELP_SECTIONS if not s.requires}
    hits = [s.id for s in search_help("persona", top_k=3, allowed_ids=ungated)]
    assert "sales-rhythm" in hits


def test_the_checklist_persona_question_still_goes_to_sales_rhythm():
    hits = search_help("why does the checklist use buying role instead of persona")
    assert hits and hits[0].id == "sales-rhythm", [s.id for s in hits]


def test_report_carries_the_link_for_the_model_to_cite():
    report = render_help_report(list(search_help("upload a document", top_k=1)))
    assert "Help link: /help/" in report


def test_section_directory_lists_links():
    directory = section_directory()
    assert "/help/chat" in directory
    assert "/help/eos" in directory


# ── Tool + coordinator wiring ────────────────────────────────────────────────


def test_help_tool_is_registered_on_the_coordinator():
    src = inspect.getsource(coordinator.build_coordinator)
    assert "tools_instance.search_cubie_help" in src


def test_help_tool_docstring_routes_product_questions_to_it():
    doc = AgentTools.search_cubie_help.__doc__ or ""
    assert "how to USE Cubie itself" in doc
    # Must steer away from the user's own data, which has its own tools.
    assert "Do NOT use it for the user's actual work" in doc


def test_coordinator_forbids_answering_product_questions_from_memory():
    src = inspect.getsource(coordinator.build_coordinator)
    assert "search_cubie_help" in src
    assert "Never describe how a feature works from memory" in src
    # And it must tell the model to hand back the Help link.
    assert "/help/chat" in src


def test_help_tool_falls_back_to_the_directory(monkeypatch):
    """An unmatched question returns the section index, not an empty report."""
    tools = AgentTools(user_email="nobody@acme.example")
    monkeypatch.setattr(
        tools,
        "_help_allowed_section_ids",
        lambda: {s.id for s in HELP_SECTIONS},
    )
    result = tools.search_cubie_help("what is the weather in Dallas tomorrow")
    assert result["status"] == "success"
    assert "/help/chat" in result["report"]
    assert "do not invent product behavior" in result["report"]


def test_help_tool_returns_matching_sections(monkeypatch):
    tools = AgentTools(user_email="nobody@acme.example")
    monkeypatch.setattr(
        tools,
        "_help_allowed_section_ids",
        lambda: {s.id for s in HELP_SECTIONS},
    )
    result = tools.search_cubie_help("how do I upload a document?")
    assert result["status"] == "success"
    assert "Help link: /help/assets" in result["report"]


def test_help_tool_gating_fails_closed_when_the_profile_is_unknown():
    """An unresolvable user gets the ungated sections only — never X-Ray or
    CRM Sync, which the Help page hides from them."""
    tools = AgentTools(user_email="not-a-real-user@example.com")
    allowed = tools._help_allowed_section_ids()
    assert "customer-xray" not in allowed
    assert "crm-sync" not in allowed
    assert "chat" in allowed


def _allowed_ids_with_gates(
    *, feature_verdict, whats_new_on: bool = True, chrome_extension_on: bool = True
) -> set[str]:
    """Resolve the Help gates for a known user, with every gate's verdict faked.

    ``feature_verdict(key)`` answers ``feature_allows``; the two older
    ``backend.auth`` predicates answer True. ``whats_new_on`` and
    ``chrome_extension_on`` are the What's New and Cubie for Chrome kill
    switches, which ship off. Patched by string target because other suites
    ``importlib.reload()`` these modules mid-run.
    """
    registry = MagicMock()
    registry.return_value.get_user.return_value = object()
    with (
        patch("rubik_core.auth.registry.UserRegistry", registry),
        patch(
            "rubik_core.features.feature_allows",
            side_effect=lambda key, _profile, *a, **k: feature_verdict(key),
        ),
        patch("backend.auth.user_can_access_xray", return_value=True),
        patch("backend.auth.user_can_access_crm_sync", return_value=True),
        patch("rubik_core.config.settings.WHATS_NEW_ENABLED", whats_new_on),
        patch(
            "rubik_core.config.settings.CHROME_EXTENSION_ENABLED", chrome_extension_on
        ),
    ):
        return AgentTools(user_email="someone@acme.example")._help_allowed_section_ids()


def test_every_requires_value_has_a_gate():
    """A ``requires`` name missing from the gate dict hides that section from
    everyone, silently, because the lookup fails closed. With every gate open,
    every section must come back."""
    allowed = _allowed_ids_with_gates(feature_verdict=lambda _key: True)
    missing = sorted({s.id for s in HELP_SECTIONS} - allowed)
    assert not missing, f"sections whose `requires` has no gate in tools.py: {missing}"


def test_the_chrome_extension_help_follows_its_roster_and_switch():
    """Chat must not point anyone at a Connect page that 403s or 404s them."""
    assert "cubie-for-chrome" in _allowed_ids_with_gates(
        feature_verdict=lambda _k: True
    )
    assert "cubie-for-chrome" not in _allowed_ids_with_gates(
        feature_verdict=lambda key: key != "chrome_extension"
    )
    assert "cubie-for-chrome" not in _allowed_ids_with_gates(
        feature_verdict=lambda _k: True, chrome_extension_on=False
    )


# ── What's new: its own gate, and it doesn't take other questions ───────────


def test_whats_new_help_needs_the_switch_and_the_roster():
    """Chat cites the What's new page only when the drawer would open: the kill
    switch on AND the reader on the `whats_new` roster."""
    assert {s.id: s for s in HELP_SECTIONS}["whats-new"].requires == "whats_new"

    def everyone(_key):
        return True

    assert "whats-new" in _allowed_ids_with_gates(feature_verdict=everyone)
    assert "whats-new" not in _allowed_ids_with_gates(
        feature_verdict=everyone, whats_new_on=False
    )
    assert "whats-new" not in _allowed_ids_with_gates(
        feature_verdict=lambda key: key != "whats_new"
    )


@pytest.mark.parametrize(
    ("question", "expected_first"),
    [
        ("What's the scorecard?", "eos"),
    ],
)
def test_whats_new_leaves_other_questions_alone(question, expected_first):
    """A question that starts with "What's" (half of them do) is not pulled to
    the drawer's page."""
    hits = [s.id for s in search_help(question, top_k=3)]
    assert hits and hits[0] == expected_first, hits
    assert "whats-new" not in hits, hits


# ── Help → Chat: the page's example questions are links ──────────────────────


@requires_frontend
def test_help_page_links_example_questions_into_chat():
    source = _KIT_TSX.read_text(encoding="utf-8")
    # The one place the deep link is built, so it can't drift per call site.
    assert "/chat?q=${encodeURIComponent(q)}" in source
    # CRM example questions render as ask links rather than plain text.
    assert "<AskLink q={q} />" in source


@requires_frontend
def test_old_help_anchor_links_still_land_on_the_topic():
    """Chat cited /help#<id> before topics became pages, and those answers are
    still in chat history — the Help home must redirect them to /help/<id>."""
    home = _HELP_HOME_TSX.read_text(encoding="utf-8")
    assert "legacyHelpHashTarget(hash)" in home
    assert "<Navigate to={legacy} replace />" in home
    registry = _HELP_TOPICS_TS.read_text(encoding="utf-8")
    assert "export const legacyHelpHashTarget" in registry


# ── What Help says matches the code ──────────────────────────────────────────
#
# Each test pins one sentence a review found out of step with the shipped
# behaviour, in BOTH Help homes where it applies (the topic page and its
# content.py mirror), so a later rewrite can't bring the old claim back.

_TOPICS_DIR = _HELP_DIR / "topics"


def _page_text(topic_file: str) -> str:
    """A topic page's prose, roughly as a reader sees it: tags and JSX
    whitespace dropped, HTML entities decoded, whitespace collapsed."""
    src = (_TOPICS_DIR / topic_file).read_text(encoding="utf-8")
    body = src.split("=> (", 1)[1]
    body = body.replace("{' '}", " ")
    body = re.sub(r"<AskLink q=\"([^\"]*)\" />", r"'\1'", body)
    body = re.sub(r"<[^>]+>", "", body)
    for entity, char in (
        ("&apos;", "'"),
        ("&amp;", "&"),
        ("&ldquo;", "'"),
        ("&rdquo;", "'"),
    ):
        body = body.replace(entity, char)
    return re.sub(r" ([,.;:)])", r"\1", " ".join(body.split()))


def _section_text(section_id: str) -> str:
    body = next(s for s in HELP_SECTIONS if s.id == section_id).body
    return " ".join(body.split())


@requires_frontend
@pytest.mark.parametrize(
    ("topic_file", "section_id", "stale", "promises"),
    [
        (
            "MyTodos.tsx",
            "my-todos",
            (
                # Managers' Chat reads every non-private promise
                # (backend/services/promise_report.py).
                "nobody else sees it",
                # The footer is kept · dropped · open (store.week_ledger).
                "kept, moved and dropped",
                # Only overdue, due-soon or undated items reach the briefing,
                # capped (PromisesChannelAgent).
                "until you mark them kept",
            ),
            (
                "A manager can ask Chat about the promises of the people who report "
                "to them and the teams they lead",
                "choose Make private",
                "Open items that are overdue, due within the next 7 days, or have no "
                "date are also sent to your Daily Briefing",
                "at most 12 at a time",
                "how many you kept and dropped since Monday, and how many are open now",
                "counts how many To-Dos each person kept or dropped",
            ),
        ),
        (
            "Productivity.tsx",
            "productivity",
            # The Tasks tab (Help Desk work) was removed with the Help Desk.
            ("File a ticket", "Help Desk"),
            ("Team Folders",),
        ),
    ],
)
def test_help_matches_the_shipped_behaviour(topic_file, section_id, stale, promises):
    """Found while writing page tours: Help described behaviour the
    app no longer has. Same sentences in both homes, old claims in neither."""
    page, mirror = _page_text(topic_file), _section_text(section_id)
    for text, where in ((page, topic_file), (mirror, section_id)):
        for claim in stale:
            assert claim not in text, (where, claim)
        for promise in promises:
            assert promise in text, (where, promise)
