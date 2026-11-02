"""Node identity for the relationship graph — the part that must never drift.

Entity resolution is the whole value of a knowledge graph: two writers that
normalize an address differently create two nodes for one person, and every
traversal through that person then returns half the truth. So the rules live in
one module and are pinned here.

The load-bearing test is `test_kind_constants_match_entitykind`. `graph/keys.py`
spells the kind strings out instead of importing `EntityKind`, because importing
it initializes the whole `priorities` package — whose calendar agent is itself a
graph writer, which would close an import cycle. That is a deliberate trade:
this test is what makes the late binding safe, so if the enum is ever renamed
the failure lands here instead of silently splitting the graph in two.
"""

from __future__ import annotations

from rubik_core.graph import keys


def test_kind_constants_match_entitykind():
    """The graph's kind strings ARE EntityKind's values — verified, not assumed."""
    from rubik_core.priorities.models import EntityKind

    assert keys.KIND_PERSON == EntityKind.PERSON.value
    assert keys.KIND_COMPANY == EntityKind.COMPANY.value
    assert keys.KIND_DEAL == EntityKind.DEAL.value
    assert keys.KIND_EVENT == EntityKind.EVENT.value
    assert keys.KIND_TRANSCRIPT == EntityKind.TRANSCRIPT.value


def test_keys_module_does_not_import_priorities():
    """Importing graph.keys must not drag in the priorities package.

    This is the cycle guard the module docstring promises. Checked in a
    subprocess because by the time the suite runs, something else has almost
    certainly imported priorities already.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import rubik_core.graph.keys, sys; "
            "sys.exit(1 if 'rubik_core.priorities' in sys.modules else 0)",
        ],
        capture_output=True,
    )
    assert result.returncode == 0, (
        "rubik_core.graph.keys pulled in rubik_core.priorities — the import "
        f"cycle guard is broken. stderr: {result.stderr.decode()[-500:]}"
    )


def test_entity_key_accepts_enum_or_string():
    from rubik_core.priorities.models import EntityKind

    assert keys.entity_key(EntityKind.PERSON, "a@b.com") == "person:a@b.com"
    assert keys.entity_key("person", "a@b.com") == "person:a@b.com"


def test_entity_key_rejects_an_empty_id():
    """A node keyed "person:" would collide every unidentifiable person into one."""
    import pytest

    with pytest.raises(ValueError):
        keys.entity_key("person", "   ")


def test_person_key_folds_case_and_whitespace():
    """The same human typed three ways is ONE node."""
    variants = [
        "BEllison@ACME.example",
        " bellison@acme.example ",
        "bellison@ACME.EXAMPLE",
    ]
    assert {keys.person_key(v) for v in variants} == {"person:bellison@acme.example"}


def test_person_key_rejects_non_addresses():
    assert keys.person_key("not-an-email") is None
    assert keys.person_key("") is None
    assert keys.person_key(None) is None


def test_company_key_normalizes_urls_and_addresses():
    for value in (
        "Northwind.example",
        "https://www.northwind.example/pricing?x=1",
        "www.NORTHWIND.example",
        "someone@northwind.example",
    ):
        assert keys.company_key(value) == "company:northwind.example", value


def test_company_key_drops_consumer_mail_hosts():
    """A company node keyed on gmail.com would merge unrelated people into one
    fictitious organization — the classic entity-resolution over-merge."""
    for host in ("gmail.com", "GMAIL.com", "yahoo.com", "icloud.com"):
        assert keys.company_key(host) is None, host
    assert keys.company_key_for_email("freelancer@gmail.com") is None
    assert keys.is_generic_email_domain("outlook.com") is True
    assert keys.is_generic_email_domain("acme.example") is False


def test_company_key_rejects_non_domains():
    assert keys.company_key("localhost") is None
    assert keys.company_key("") is None
    assert keys.company_key(None) is None


def test_subdomains_stay_distinct_but_www_does_not():
    """`www.` is a prefix, not an organization; a real subdomain is left alone
    rather than guessed at (no public-suffix list here)."""
    assert keys.company_key("www.northwind.example") == "company:northwind.example"
    assert keys.company_key("sub.northwind.example") == "company:sub.northwind.example"


def test_deal_key_accepts_int_or_string_ids():
    assert keys.deal_key(12345) == "deal:12345"
    assert keys.deal_key("12345") == "deal:12345"
    assert keys.deal_key(None) is None
    assert keys.deal_key("") is None


def test_transcript_and_event_keys_never_collide():
    """Transcript ids and calendar event ids are different keyspaces (see
    backend/signals/correlator.py) — the same raw string must yield two nodes."""
    raw = "abc123"
    assert keys.transcript_key(raw) != keys.event_key(raw)
    assert keys.transcript_key(raw) == "transcript:abc123"
    assert keys.event_key(raw) == "event:abc123"


def test_parse_key_round_trips_ids_containing_colons():
    """Google Calendar event ids can contain colons; splitting on the last one
    would corrupt the id."""
    key = keys.event_key("abc:def_20260820")
    assert keys.parse_key(key) == ("event", "abc:def_20260820")
    assert keys.parse_key(keys.person_key("a@b.com")) == ("person", "a@b.com")
