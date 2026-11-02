"""Meeting Skills — Plaud-style Templates, Skills and Routines in Ask Cubie.

Three primitives, kept apart on purpose:

* a **Template** is only the outline of a write-up — which sections, in what
  order, what goes in each. Cubie writes it from whatever the person asks for
  ("give me this call as a BANT note"), or copies it from a form they attach;
* a **Skill** is a whole repeatable job — which meetings, the playbook, tone,
  depth, an optional Template, and where the result goes;
* a **Routine** is a standing order that runs a Skill on its own: on a clock
  (an ordinary scheduled task whose instruction runs the Skill) or when a new
  transcript of the owner's lands.

A Routine runs Skills; a Skill may render through a Template. Templates never
run on their own, Skills never schedule themselves, Routines never format.

Every run goes through the same contract (``executor.run``): load the
meetings the person attended → one structured extraction where every item
cites a numbered transcript line and quotes it → deterministic verification
that drops what the transcript does not support → render → deliver → log.
The transcript is DATA: something said in a meeting never runs as an
instruction.

Chat is the only surface. The tools live in
``rubik_core/tools/meeting_skills_tools.py``; the "/" menu in the chat box
reads ``backend/routers/meeting_skills.py``.
"""

#: Admin → AI Cost category for every model call made here.
COST_FEATURE = "meeting_skills"
