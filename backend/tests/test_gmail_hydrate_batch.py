"""GmailClient.search_threads hydrates threads in one batched round trip.

The one-call-per-thread loop was most of the Productivity → Email load (25
serial ``threads.get`` calls) and every Priorities / Cockpit Gmail sweep paid it
too. These pin the batched path: same threads, same order, one batch call, and
anything the batch could not return is fetched on its own so a failure still
raises exactly as before.
"""

from __future__ import annotations

import pytest

from rubik_core.tools.gmail import GmailClient


class _Req:
    def __init__(self, svc, tid):
        self.svc, self.tid = svc, tid

    def execute(self):
        self.svc.single_gets.append(self.tid)
        if self.tid in self.svc.missing:
            raise RuntimeError(f"404 {self.tid}")
        return {"id": self.tid, "messages": []}


class _Batch:
    def __init__(self, svc, callback):
        self.svc, self.callback, self.reqs = svc, callback, []

    def add(self, req, request_id):
        self.reqs.append((request_id, req))

    def execute(self):
        self.svc.batches.append([r.tid for _, r in self.reqs])
        if self.svc.batch_explodes:
            raise RuntimeError("batch transport error")
        for rid, req in self.reqs:
            if req.tid in self.svc.throttled or req.tid in self.svc.missing:
                self.callback(rid, None, RuntimeError("429 rateLimitExceeded"))
            else:
                self.callback(rid, {"id": req.tid, "messages": []}, None)


class _Service:
    def __init__(self, ids, *, throttled=(), missing=(), batch_explodes=False):
        self.ids = ids
        self.throttled, self.missing = set(throttled), set(missing)
        self.batch_explodes = batch_explodes
        self.batches: list[list[str]] = []
        self.single_gets: list[str] = []

    def users(self):
        return self

    def threads(self):
        return self

    def list(self, **_):
        svc = self

        class _List:
            def execute(self):
                return {"threads": [{"id": i} for i in svc.ids]}

        return _List()

    def get(self, userId, id, format):  # noqa: A002 — mirrors the Gmail API kwarg
        assert format == "metadata"
        return _Req(self, id)

    def new_batch_http_request(self, callback):
        return _Batch(self, callback)


def _client(svc) -> GmailClient:
    client = GmailClient.__new__(GmailClient)
    client.user_email = "me@acme.example"
    client.service = svc
    return client


def test_hydrates_every_thread_in_one_batch_in_list_order():
    ids = [f"t{i}" for i in range(25)]
    svc = _Service(ids)

    out = _client(svc).search_threads(query="is:unread in:inbox", max_results=25)

    assert [t["id"] for t in out] == ids
    assert svc.batches == [ids]
    assert svc.single_gets == []


def test_batches_are_capped_at_fifty_threads():
    ids = [f"t{i}" for i in range(120)]
    svc = _Service(ids)

    out = _client(svc).search_threads(max_results=120)

    assert [t["id"] for t in out] == ids
    assert [len(b) for b in svc.batches] == [50, 50, 20]


def test_throttled_threads_are_refetched_one_by_one_and_keep_their_place():
    ids = ["a", "b", "c", "d"]
    svc = _Service(ids, throttled={"b", "d"})

    out = _client(svc).search_threads()

    assert [t["id"] for t in out] == ids
    assert svc.single_gets == ["b", "d"]


def test_a_failed_batch_call_falls_back_to_the_old_loop():
    ids = ["a", "b", "c"]
    svc = _Service(ids, batch_explodes=True)

    out = _client(svc).search_threads()

    assert [t["id"] for t in out] == ids
    assert svc.single_gets == ids


def test_a_thread_that_fails_on_its_own_still_raises():
    svc = _Service(["a", "gone"], missing={"gone"})

    with pytest.raises(RuntimeError, match="404 gone"):
        _client(svc).search_threads()


def test_empty_inbox_makes_no_batch_call():
    svc = _Service([])

    assert _client(svc).search_threads() == []
    assert svc.batches == []
