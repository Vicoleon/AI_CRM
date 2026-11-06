"""Pure-function tests for the model-probe script. No network.

The script's value is that it asks the RIGHT ids; these lock in the two rules
that decide that, both of which were gotten wrong first.
"""

from __future__ import annotations

from rubik_core.scripts.probe_models import (
    NEGATIVE_CONTROLS,
    _classify,
    _family_controls,
    _sweep_candidates,
)

# Reflects the real 2026-09 pins: FLASH two generations ahead of a stranded PRO.
PINNED = {
    "LITE": "gemini-3.5-flash-lite",
    "FLASH": "gemini-3.8-flash",
    "PRO": "gemini-2.5-pro",
}


def test_sweep_levels_a_stranded_tier_up_to_the_frontier():
    """The bug this script existed to answer, encoded.

    Bumping each tier from its OWN pin asks about gemini-3.1-pro and stops —
    so the sweep would have missed gemini-3.8-pro, the single id the whole
    "is there a newer Pro?" question is about. The ceiling must come from the
    highest version pinned anywhere, not from the tier being swept.
    """
    got = _sweep_candidates(PINNED, depth=2)
    assert "gemini-3.8-pro" in got, "stranded PRO never reached the FLASH frontier"
    assert "gemini-4.0-pro" in got  # and `depth` past it


def test_sweep_excludes_what_is_already_pinned_and_dedupes():
    got = _sweep_candidates(PINNED, depth=3)
    for pin in PINNED.values():
        assert pin not in got, f"{pin} is already pinned — probing it is not a sweep"
    assert len(got) == len(set(got))


def test_sweep_rolls_the_minor_version_into_the_next_major():
    got = _sweep_candidates({"FLASH": "gemini-3.9-flash"}, depth=2)
    assert "gemini-4.0-flash" in got
    assert "gemini-3.10-flash" not in got  # not a version Google mints


def test_sweep_includes_the_floating_alias_per_tier():
    got = _sweep_candidates(PINNED, depth=1)
    assert "gemini-pro-latest" in got
    assert "gemini-flash-latest" in got


def test_sweep_survives_an_unparseable_pin():
    # A pin that is an alias rather than a versioned id must not crash the sweep.
    assert _sweep_candidates({"FLASH": "gemini-flash-latest"}, depth=2) == []


def test_classify_buckets_match_the_health_check():
    assert _classify("404 NOT_FOUND. Publisher model ... was not found") == "not_found"
    assert _classify("429 RESOURCE_EXHAUSTED. Resource exhausted.") == "quota_exhausted"
    assert _classify("400 INVALID_ARGUMENT. thinking_level") == "bad_request"
    assert _classify("403 PERMISSION_DENIED") == "permission_denied"
    assert _classify("connection reset by peer") == "error"


def test_negative_controls_are_not_plausibly_real():
    """These must stay obviously fake. A control that Google later ships would
    make every run exit 2 and report nothing."""
    assert NEGATIVE_CONTROLS
    for ctl in NEGATIVE_CONTROLS:
        major = int(ctl.split("-")[1].split(".")[0])
        # A 4.x control stopped being obviously fake once Gemini 4 shipped.
        assert major >= 9, ctl


def test_requested_ids_get_a_control_from_their_own_naming_family():
    """The fixed controls are all pro/flash/flash-lite, so they say nothing
    about how Vertex answers an unknown id in a NEW family. The Argon probe
    used exactly this shape: gemini-9.9-argon."""
    assert _family_controls(["gemini-4-argon"]) == ["gemini-9.9-argon"]
    assert _family_controls(["gemini-3.8-flash"]) == ["gemini-9.9-flash"]
    # An unversioned alias still gets an impossible sibling.
    assert _family_controls(["gemini-flash-latest"]) == ["gemini-9.9-flash-latest"]


def test_family_controls_never_control_against_a_requested_id():
    got = _family_controls(["gemini-4-argon", "gemini-9.9-argon", "gemini-5-argon"])
    assert "gemini-9.9-argon" not in got
    assert len(got) == len(set(got))
