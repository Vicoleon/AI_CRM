"""What's New — the reader of the Cubie changelog.

Spec §4.6.1: RubikSoft writes each release note once and every workspace
reads it. A workspace sees the notes through the What's New drawer, its
button and the Daily Briefing's spotlight bar, filtered to the features it
has switched on and each person may use. What stays per workspace is who
opened a note (``whats_new_opens``), what each person has already seen (in
their browser), and the opt-in morning post of new notes. Nothing here drafts,
approves or edits a note. Design: ``docs/whats_new/README.md``.

Module map:

* ``changelog`` — :class:`ChangelogEntry`, the :class:`ChangelogSource`
  protocol and :func:`get_changelog_source` (empty until Phase 3 adds the
  control-database source).
* ``audience`` — which notes reach whom (the feature switched on, Feature
  Access for the person).
* ``stats`` — recording an open as people, never who.
* ``settings`` — Admin → What's New (the morning post's switch and channel,
  the weekly tease) and the post's mark, in ``workspace_settings``.
* ``digest`` — ``post_digest``: the weekday morning post (the only Slack
  channel writer).
* ``links`` — links back into the app from that post.

Nothing is imported here on purpose: the routers import the one module they
need. ``rubik_core`` never imports ``backend`` at module scope; the Slack
client is imported inside the functions that post.
"""
