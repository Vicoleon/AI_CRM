"""An unfinished "Notes by Gemini" doc must NOT be ingested.

Google creates the notes doc as an empty shell when the meeting ends and fills
it in over the next few minutes. ``transcript_sync`` runs every 15 minutes, so
a pass lands in that gap regularly. The correct response is to skip: no asset is
registered, so the next pass re-exports the doc once Gemini has finished.

That guard existed (``if not text``) but never fired, because an empty Google
Doc exports as a bare UTF-8 BOM and ``"﻿"`` is truthy. The shell was
ingested and marked ``approved``, which the status check then treats as done
forever — leaving a 3-byte transcript that is invisible in Chat and unusable by
the L10 recap email. Up to a third of a week's transcripts can be poisoned
this way, including an L10 whose real notes run to 17,000 characters in Drive.
"""

from __future__ import annotations

import pytest

from rubik_core.eos.l10_recording import MIN_TRANSCRIPT_CHARS


@pytest.mark.parametrize(
    "exported",
    [
        pytest.param("﻿", id="bom-only (the real-world case)"),
        pytest.param("", id="truly-empty"),
        pytest.param("﻿   \n\n  ", id="bom-plus-whitespace"),
        pytest.param("﻿" + "x" * (MIN_TRANSCRIPT_CHARS - 1), id="still-writing"),
    ],
)
def test_unfinished_export_is_not_ingestable(exported: str):
    """The gate the sync applies before registering an asset."""
    text = (exported or "").lstrip("﻿")
    assert len(text.strip()) < MIN_TRANSCRIPT_CHARS, (
        "an unfinished Gemini doc must fail the gate, or it gets ingested and "
        "marked approved — which the sync never revisits"
    )


def test_a_finished_transcript_passes():
    exported = "﻿" + ("Blair Ellison: real meeting content. " * 40)
    text = exported.lstrip("﻿")
    assert len(text.strip()) >= MIN_TRANSCRIPT_CHARS
    assert not text.startswith("﻿")


def test_the_old_guard_was_defeated_by_the_bom():
    """Pins WHY this shipped broken, so nobody reinstates the old check."""
    bom_only = "﻿"
    assert bool(bom_only), "a BOM is truthy — `if not text` could never catch it"
    assert bom_only.strip() == bom_only, "and U+FEFF is not whitespace to strip()"
    # The fix is the lstrip, which is what makes both checks work.
    assert bom_only.lstrip("﻿") == ""


def test_sync_module_applies_the_gate():
    """The gate lives in the sync's ingest loop, not just in this test."""
    import inspect

    from rubik_core.scripts import transcript_sync

    src = inspect.getsource(transcript_sync._sync_user)
    assert 'lstrip("\\ufeff")' in src
    assert "MIN_TRANSCRIPT_CHARS" in src
