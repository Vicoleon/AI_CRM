"""Server-side agent utilities that compose into the ADK supervisor.

`alignment_context` is the EOS alignment module: builds a compact text block
of the company's current Vision, quarterly Rocks, weekly Scorecard health,
top Issues, and the calling user's Rocks/To-Dos, ready to be prepended
to the supervisor's system prompt on every chat turn.
"""
