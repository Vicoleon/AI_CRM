"""UC-17 Signal contracts.

Every source module (HubSpot/Gmail/Slack/Calendar/Transcripts) emits `Signal`s
that the correlator groups and the Gemini judge ranks. This package owns the
shared dataclass and the `SignalSource` protocol — source-specific code lives
alongside (e.g. `backend/signals/hubspot.py`).
"""

from backend.signals.types import KNOWN_SOURCES, Signal, SignalSource

__all__ = ["Signal", "SignalSource", "KNOWN_SOURCES"]
