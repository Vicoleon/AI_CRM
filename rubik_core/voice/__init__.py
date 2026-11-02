"""Voice input — a spoken clip in, the words the person said out.

The browser records, ``/api/voice/transcribe`` hands the clip to Gemini on
Vertex once (:func:`rubik_core.voice.transcribe.transcribe_clip`), and the text
goes back to the box the person was typing in. Nothing is saved or acted on
from here; in Ask Cubie the browser then sends the words as the person's chat
message, because stopping the mic is the send.

Voice commands (:mod:`rubik_core.voice.commands`) are the other half: a spoken
command anywhere in Cubie is read into one of a fixed set of things to do
(open a page, ask Cubie, an EOS change), and an EOS change is only made after
the person confirms it.

The audio is never stored and the transcript is never logged.
"""

from __future__ import annotations

#: The AI Cost category. ``@attribute_to(COST_FEATURE)`` sits on
#: ``transcribe.transcribe_clip``; the matching ``_FEATURE_LABELS`` entry in
#: ``backend/routers/admin.py`` is pinned by a test.
COST_FEATURE = "voice_input"
#: The AI Cost category for voice commands (``commands.read_command`` and
#: ``commands.read_confirmation``), pinned the same way.
COMMANDS_COST_FEATURE = "voice_commands"
