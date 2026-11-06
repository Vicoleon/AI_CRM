"""CCS calendar scan: email domain → HubSpot company, and what must NOT happen.

Two units, one decision: which company is this calendar meeting with? Everything
here is about refusing to answer confidently when we cannot, because the answer
feeds a HubSpot write the AE approves later without seeing this screen again.

The three failure modes pinned below are the ones that would ship silently:

* ``candidates[0]`` — two companies share a domain and the first one wins. Both
  existing domain lookups in this repo do exactly that; ``DomainMatch.matched``
  makes it unrepresentable, and ``test_two_companies_one_domain_is_ambiguous``
  is the guard.
* a mismatched pairing — HubSpot answers about one domain and the row is filed
  under the domain that happened to be next in the request.
* an error remembered as an answer — a 429 cached as "no company for acmecorp.alt"
  for the next 24 hours.

The company search and ownership association are faked, so the batching,
the ``IN`` filter and explicit ``limit`` are real while no HubSpot request runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rubik_core.call_capture import domain_match
from rubik_core.graph import keys as graph_keys
from rubik_core.retrieval import hubspot_api
from rubik_core.tenancy.context import NoWorkspaceContext, use_workspace
from rubik_core.tenancy.hubspot_config import HubSpotConnectorConfig

AE = "ae@acme.example"


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    monkeypatch.setattr(domain_match, "workspace_secret", lambda *_: "test-token")
    monkeypatch.setattr(
        hubspot_api,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=("default",)),
    )
    domain_match.clear_match_cache()
    yield
    domain_match.clear_match_cache()


def _company(company_id: str, name: str, domain: str, stage: str = "customer") -> dict:
    return {
        "id": company_id,
        "properties": {
            "name": name,
            "domain": domain,
            "lifecyclestage": stage,
            "hs_object_id": company_id,
        },
    }


def _client(
    rows_by_call, calls: list, *, token: str = "test-token"
) -> hubspot_api.HubSpotAPIClient:
    """A real client with company search and owned-deal proof faked."""
    client = hubspot_api.HubSpotAPIClient(token=token)

    def fake_search(object_type: str, body: dict) -> dict:
        calls.append((object_type, body))
        result = rows_by_call(body) if callable(rows_by_call) else rows_by_call
        rows = result if isinstance(result, list) else result.get("results", [])
        return {"total": len(rows), "results": rows}

    def no_deals(*_a, **_k):  # pragma: no cover - failing on purpose
        raise AssertionError("_deals_for_company must never be called on this path")

    client._search = fake_search  # type: ignore[method-assign]
    client._deals_for_company = no_deals  # type: ignore[method-assign]
    client.fully_owned_deal_associations = (  # type: ignore[method-assign]
        lambda from_type, ids: {str(i): ["owned-deal"] for i in ids}
    )
    return client


# ── hubspot_api.companies_for_domains ────────────────────────────────────────


def test_one_company_one_domain_is_matched():
    calls: list = []
    client = _client(
        [_company("123", "Acme Industries LLC", "acmeindustries.alt")], calls
    )

    result = client.companies_for_domains([" AcmeIndustries.alt "])

    assert list(result) == ["acmeindustries.alt"], "keyed by the folded input"
    match = result["acmeindustries.alt"]
    assert match.matched is True
    assert (match.ambiguous, match.unmatched, match.error) == (False, False, None)
    assert match.company is not None
    assert match.company.hubspot_company_id == "123"
    assert match.company.name == "Acme Industries LLC"
    assert match.company.lifecyclestage == "customer"
    # One search, the IN filter, and an explicit page size (the default truncates).
    assert len(calls) == 1
    body = calls[0][1]
    assert body["filterGroups"][0]["filters"] == [
        {
            "propertyName": "domain",
            "operator": "IN",
            "values": ["acmeindustries.alt"],
        }
    ]
    assert body["limit"] == hubspot_api.HubSpotAPIClient.PAGE
    assert body["properties"] == hubspot_api._COMPANY_MATCH_PROPS


def test_absent_domain_is_unmatched_and_echoes_itself():
    """A domain HubSpot has never heard of is a key, not an absence."""
    client = _client([], [])

    result = client.companies_for_domains(["ghost-client.com"])

    match = result["ghost-client.com"]
    assert match.domain == "ghost-client.com"
    assert (match.unmatched, match.matched, match.error) == (True, False, None)
    assert match.candidates == ()
    assert match.company is None


def test_unreturned_domain_is_a_miss_not_a_mismatched_pairing():
    """Two asked, one answered: the silent one must not inherit the answer."""
    client = _client([_company("1", "Acme Industries LLC", "acmeindustries.alt")], [])

    result = client.companies_for_domains(["acmeindustries.alt", "beta-client.com"])

    assert set(result) == {"acmeindustries.alt", "beta-client.com"}
    assert result["acmeindustries.alt"].matched is True
    assert result["beta-client.com"].unmatched is True
    assert result["beta-client.com"].candidates == ()


def test_row_for_an_unrequested_domain_is_dropped_not_reassigned():
    """HubSpot answering about someone else must not bind our meeting to them."""
    client = _client([_company("9", "Someone Else Inc", "unrelated.com")], [])

    result = client.companies_for_domains(["acmeindustries.alt"])

    assert set(result) == {"acmeindustries.alt"}
    assert result["acmeindustries.alt"].unmatched is True


def test_hubspot_www_domain_still_pairs_to_the_bare_request():
    client = _client([_company("5", "Acme", "www.AcmeIndustries.alt")], [])

    match = client.companies_for_domains(["acmeindustries.alt"])["acmeindustries.alt"]

    assert match.matched is True
    # The requested domain, not the stored property, so the UI shows one string.
    assert match.company is not None
    assert match.company.domain == "acmeindustries.alt"


def test_two_companies_one_domain_is_ambiguous_and_prefills_nothing():
    client = _client(
        [
            _company("200", "Zed Holdings", "acmeindustries.alt"),
            _company("100", "Acme Industries LLC", "acmeindustries.alt"),
        ],
        [],
    )

    match = client.companies_for_domains(["acmeindustries.alt"])["acmeindustries.alt"]

    assert match.ambiguous is True
    assert match.matched is False, "two candidates may never prefill a company"
    assert match.company is None
    assert [c.hubspot_company_id for c in match.candidates] == ["100", "200"]
    assert [c.name for c in match.candidates] == ["Acme Industries LLC", "Zed Holdings"]


def test_truncated_page_errors_the_batch_rather_than_looking_unique():
    """A hidden second candidate would render as a confident wrong prefill."""
    client = hubspot_api.HubSpotAPIClient(token="test-token")
    client._search = lambda obj, body: {  # type: ignore[method-assign]
        "total": 900,
        "results": [_company("1", "Acme Industries LLC", "acmeindustries.alt")],
        "paging": {"next": {"after": "200"}},
    }

    match = client.companies_for_domains(["acmeindustries.alt"])["acmeindustries.alt"]

    assert match.error is not None
    assert (match.matched, match.ambiguous, match.unmatched) == (False, False, False)


def test_batching_splits_at_the_documented_limit():
    calls: list = []
    client = _client([], calls)
    domains = [f"client{i}.com" for i in range(hubspot_api._DOMAIN_MATCH_BATCH + 5)]

    result = client.companies_for_domains(domains)

    assert len(result) == len(domains)
    assert [len(c[1]["filterGroups"][0]["filters"][0]["values"]) for c in calls] == [
        hubspot_api._DOMAIN_MATCH_BATCH,
        5,
    ]


def test_a_raising_search_errors_every_domain_in_the_batch():
    def boom(_body):
        raise hubspot_api.HubSpotAPIError("HubSpot exploded")

    client = _client(boom, [])

    result = client.companies_for_domains(["acmeindustries.alt", "beta-client.com"])

    assert set(result) == {"acmeindustries.alt", "beta-client.com"}
    for match in result.values():
        assert match.error == "HubSpot company lookup failed"
        # "could not check" is NOT "no company" — none of the other states hold.
        assert (match.matched, match.ambiguous, match.unmatched) == (
            False,
            False,
            False,
        )


# ── domain_match: blocklists, caching, composition ───────────────────────────


def test_a_blocked_domain_never_reaches_hubspot():
    calls: list = []
    client = _client([], calls)

    result = domain_match.resolve_domain_matches(
        [
            "gmail.com",  # consumer mail
            "att.net",  # ISP tail
            "example.com",  # reserved
            "group.calendar.google.com",  # calendar pseudo-domain
            "googlegroups.com",
            "acme.example",  # ourselves
        ],
        ae_email=AE,
        client=client,
    )

    assert result == {}
    assert calls == [], "no HubSpot search may be issued for a blocked domain"


def test_a_role_mailbox_domain_still_matches_its_company():
    """``sales@acmecorp.alt`` is still Acme — the role list is a person filter only."""
    own = domain_match.own_domains(AE)

    assert domain_match.is_company_matchable("acmecorp.alt", own=own) is True


def test_a_positive_verdict_and_an_error_are_both_rechecked():
    calls: list = []
    state = {"fail": True}

    def flaky(_body):
        if state["fail"]:
            raise hubspot_api.HubSpotAPIError("HubSpot exploded")
        return [_company("1", "Acme Industries LLC", "acmeindustries.alt")]

    client = _client(flaky, calls)

    first = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert first["acmeindustries.alt"].error == "HubSpot company lookup failed"
    assert domain_match._MATCH_CACHE == {}, "a failure is never an answer"

    # Nothing cached, so the next scan retries rather than repeating the failure.
    state["fail"] = False
    second = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert second["acmeindustries.alt"].matched is True
    assert len(calls) == 2

    # A hit is searched again because domain ownership can change too.
    third = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert third["acmeindustries.alt"].matched is True
    assert len(calls) == 3


def test_cached_positive_company_is_rechecked_after_pipeline_reassignment():
    calls: list = []
    client = _client([_company("1", "Old customer", "acmeindustries.alt")], calls)
    first = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert first["acmeindustries.alt"].company.name == "Old customer"

    # The deal moves to another workspace in the shared portal while the
    # positive domain verdict is still warm.
    client.fully_owned_deal_associations = lambda _kind, _ids: {}  # type: ignore[method-assign]
    second = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )

    assert second["acmeindustries.alt"].unmatched
    assert second["acmeindustries.alt"].company is None
    assert len(calls) == 2


def test_positive_domain_match_changes_when_domain_moves_between_owned_companies():
    calls: list = []
    current = ["1"]
    client = _client(
        lambda _body: [
            _company(current[0], f"Company {current[0]}", "acmeindustries.alt")
        ],
        calls,
    )
    first = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert first["acmeindustries.alt"].company.hubspot_company_id == "1"

    current[0] = "2"
    second = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert second["acmeindustries.alt"].company.hubspot_company_id == "2"
    assert len(calls) == 2


def test_cached_miss_is_not_used_when_pipeline_policy_changes_or_fails(monkeypatch):
    calls: list = []
    pipeline_ids = ["default"]
    monkeypatch.setattr(
        hubspot_api,
        "get_hubspot_config",
        lambda: HubSpotConnectorConfig(pipeline_ids=tuple(pipeline_ids)),
    )
    client = _client([], calls)
    first = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert first["acmeindustries.alt"].unmatched

    pipeline_ids[:] = []
    unavailable = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert unavailable["acmeindustries.alt"].error
    assert len(calls) == 1

    pipeline_ids[:] = ["new-pipeline"]
    again = domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert again["acmeindustries.alt"].unmatched
    assert len(calls) == 2


def test_a_miss_expires_in_minutes_and_a_hit_is_checked_live():
    """The AE's usual response to a miss is to go create the company in HubSpot.

    A 24h negative entry would hide their own work from them for the rest of the
    day, so misses get minutes; a real match is checked on every request.
    """
    calls: list = []
    client = _client(
        lambda body: (
            [_company("1", "Acme Industries LLC", "acmeindustries.alt")]
            if "acmeindustries.alt" in body["filterGroups"][0]["filters"][0]["values"]
            else []
        ),
        calls,
    )

    domain_match.resolve_domain_matches(
        ["acmeindustries.alt", "ghost-client.com"], ae_email=AE, client=client
    )
    assert set(domain_match._MATCH_CACHE) == {("test", "ghost-client.com")}

    # Age the negative entry by 16 minutes.
    aged = datetime.now(timezone.utc) - timedelta(seconds=16 * 60)
    for key, (match, _, token_hash) in list(domain_match._MATCH_CACHE.items()):
        domain_match._MATCH_CACHE[key] = (match, aged, token_hash)

    domain_match.resolve_domain_matches(
        ["acmeindustries.alt"], ae_email=AE, client=client
    )
    assert len(calls) == 2, "a positive match is always searched live"

    domain_match.resolve_domain_matches(
        ["ghost-client.com"], ae_email=AE, client=client
    )
    assert len(calls) == 3, "the miss expired and was re-checked"


def test_same_domain_cache_is_separate_for_each_workspace(monkeypatch):
    """One portal's company ID must never prefill another portal's meeting."""
    monkeypatch.setattr(domain_match, "own_domains", lambda _email: frozenset())
    a_calls: list = []
    b_calls: list = []
    a = _client([_company("A-123", "Acme A", "shared-client.com")], a_calls)
    b = _client([_company("B-987", "Acme B", "shared-client.com")], b_calls)

    with use_workspace("test"):
        first = domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=a
        )
    with use_workspace("test-b"):
        second = domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=b
        )
    with use_workspace("test"):
        again = domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=a
        )

    assert first["shared-client.com"].company.hubspot_company_id == "A-123"
    assert second["shared-client.com"].company.hubspot_company_id == "B-987"
    assert again["shared-client.com"].company.hubspot_company_id == "A-123"
    assert len(a_calls) == 2 and len(b_calls) == 1
    assert domain_match._MATCH_CACHE == {}


def test_domain_cache_requires_workspace_context_even_when_warm(monkeypatch):
    from rubik_core.tenancy import context

    monkeypatch.setattr(domain_match, "own_domains", lambda _email: frozenset())
    calls: list = []
    client = _client([_company("A-123", "Acme A", "shared-client.com")], calls)
    with use_workspace("test"):
        domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=client
        )
    monkeypatch.setattr(context, "_process_workspace", None)

    with pytest.raises(NoWorkspaceContext):
        domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=client
        )
    assert len(calls) == 1


def test_cached_company_is_unavailable_after_token_clear_or_rotation(monkeypatch):
    monkeypatch.setattr(domain_match, "own_domains", lambda _email: frozenset())
    token = ["first-token"]
    monkeypatch.setattr(domain_match, "workspace_secret", lambda *_: token[0])
    a_calls: list = []
    b_calls: list = []
    a = _client(
        [_company("A-123", "Old Portal", "shared-client.com")],
        a_calls,
        token="first-token",
    )
    b = _client(
        [_company("B-987", "New Portal", "shared-client.com")],
        b_calls,
        token="second-token",
    )

    first = domain_match.resolve_domain_matches(
        ["shared-client.com"], ae_email=AE, client=a
    )
    assert first["shared-client.com"].company.hubspot_company_id == "A-123"
    token[0] = None
    revoked = domain_match.resolve_domain_matches(
        ["shared-client.com"], ae_email=AE, client=a
    )
    assert revoked["shared-client.com"].error == "HubSpot read credential unavailable"
    assert len(a_calls) == 1

    token[0] = "second-token"
    rotated = domain_match.resolve_domain_matches(
        ["shared-client.com"], ae_email=AE, client=b
    )
    assert rotated["shared-client.com"].company.hubspot_company_id == "B-987"
    assert len(b_calls) == 1


def test_injected_other_workspace_client_cannot_use_warm_domain_cache(monkeypatch):
    monkeypatch.setattr(domain_match, "own_domains", lambda _email: frozenset())
    token_by_workspace = {"test": "a-token", "test-b": "b-token"}
    from rubik_core.tenancy.context import current_workspace

    monkeypatch.setattr(
        domain_match,
        "workspace_secret",
        lambda *_: token_by_workspace[current_workspace()],
    )
    a_calls: list = []
    b_calls: list = []
    a = _client(
        [_company("A-123", "Portal A", "shared-client.com")],
        a_calls,
        token="a-token",
    )
    b = _client(
        [_company("B-987", "Portal B", "shared-client.com")],
        b_calls,
        token="b-token",
    )

    with use_workspace("test"):
        warm = domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=a
        )
        wrong = domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=b
        )
    with use_workspace("test-b"):
        other = domain_match.resolve_domain_matches(
            ["shared-client.com"], ae_email=AE, client=b
        )

    assert warm["shared-client.com"].company.hubspot_company_id == "A-123"
    assert wrong["shared-client.com"].error == (
        "HubSpot client credential does not match workspace"
    )
    assert other["shared-client.com"].company.hubspot_company_id == "B-987"
    assert len(a_calls) == len(b_calls) == 1


def test_resolve_dedupes_and_normalizes_before_asking():
    calls: list = []
    client = _client([], calls)

    domain_match.resolve_domain_matches(
        ["AcmeIndustries.alt", "acmeindustries.alt ", "www.acmeindustries.alt"],
        ae_email=AE,
        client=client,
    )

    assert len(calls) == 1
    assert calls[0][1]["filterGroups"][0]["filters"][0]["values"] == [
        "acmeindustries.alt"
    ]


# ── domain_match.external_domains_for_event ──────────────────────────────────


def test_gmail_only_external_attendee_is_still_external_but_skipped():
    """The row must not read as internal, and must not read as matched."""
    event = {
        "attendees": [
            {"email": AE, "self": True, "organizer": True},
            {"email": "borrower@gmail.com"},
        ]
    }
    own = domain_match.own_domains(AE)

    domains, truncated = domain_match.external_domains_for_event(event, ae_email=AE)

    assert domains == [], "nothing worth asking HubSpot about"
    assert truncated is False
    # ...but the attendee IS external, so the caller keeps the row and renders
    # company match "skipped" rather than dropping the meeting.
    assert "gmail.com" not in own
    assert domain_match.is_company_matchable("gmail.com", own=own) is False


def test_room_resource_attendee_never_becomes_a_domain():
    event = {
        "attendees": [
            {"email": AE, "self": True},
            {
                "email": "boardroom@acme.example",
                "resource": True,
                "displayName": "Boardroom",
            },
            {
                "email": "room-2@somevendor.com",
                "resource": True,
                "displayName": "Rented room",
            },
            {"email": "cfo@acmeindustries.alt", "optional": True},
        ]
    }

    domains, _ = domain_match.external_domains_for_event(event, ae_email=AE)

    assert domains == ["acmeindustries.alt"], (
        "a room read as a company is a wrong write"
    )


def test_attendees_omitted_is_surfaced_as_true():
    event = {
        "attendees": [
            {"email": AE, "self": True},
            {"email": "cfo@acmeindustries.alt"},
        ],
        "attendeesOmitted": True,
    }

    domains, truncated = domain_match.external_domains_for_event(event, ae_email=AE)

    assert domains == ["acmeindustries.alt"]
    assert truncated is True, "the invite was cut; the company match is ambiguous"


def test_self_and_own_domain_are_dropped_without_a_self_flag():
    event = {
        "attendees": [
            {"email": "AE@ACME.example"},  # no `self` flag — match by address
            {"email": "colleague@acme.example"},
            {"email": "cfo@acmeindustries.alt"},
        ]
    }

    domains, _ = domain_match.external_domains_for_event(event, ae_email=AE)

    assert domains == ["acmeindustries.alt"]


def test_no_attendees_yields_no_domains():
    domains, truncated = domain_match.external_domains_for_event({}, ae_email=AE)

    assert (domains, truncated) == ([], False)


def test_own_domains_unions_workspace_and_caller():
    assert domain_match.own_domains(AE) == frozenset({"acme.example"})
    assert domain_match.own_domains("rep@acme-labs.example") == frozenset(
        {"acme.example", "acme-labs.example"}
    )
    assert domain_match.own_domains(None) == frozenset({"acme.example"})


# ── the ISP tail lives in graph/keys.py, with the rest of the consumer list ──


def test_us_isp_tail_is_a_generic_email_domain():
    """Added to ``_GENERIC_EMAIL_DOMAINS``, not to a fifth blocklist here."""
    own = domain_match.own_domains(AE)
    for domain in (
        "att.net",
        "cox.net",
        "bellsouth.net",
        "charter.net",
        "earthlink.net",
        "mac.com",
        "ymail.com",
        "gmx.com",
        "zoho.com",
        "fastmail.com",
        "yandex.com",
        "qq.com",
        "163.com",
    ):
        assert graph_keys.is_generic_email_domain(domain) is True, domain
        assert graph_keys.company_key(domain) is None, domain
        assert domain_match.is_company_matchable(domain, own=own) is False, domain
    # And a real client domain is untouched by the widening.
    assert graph_keys.is_generic_email_domain("acmeindustries.alt") is False
