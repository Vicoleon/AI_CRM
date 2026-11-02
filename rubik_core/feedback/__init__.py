"""WGLL feedback — user ratings on Cubie's proposals/messages.

``store`` holds the persistence/query helpers (upsert + admin reads + export).
``wgll`` turns those ratings into a compact "what good looks like" / "what to
avoid" prompt block that is auto-injected into Cubie's priorities and chat
prompts so the assistant learns from real user judgments.
"""
