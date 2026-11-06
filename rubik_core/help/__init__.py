"""Product help as text the chat coordinator can retrieve.

:mod:`rubik_core.help.content` is Cubie's own Help — one page per topic,
mirrored from ``frontend/src/pages/help/`` in this repo (registry
``helpTopics.ts``, page bodies ``topics/``), and kept honest by a parity test.
It is served by the lexical index in :mod:`rubik_core.help._retrieval`.
"""

from rubik_core.help._retrieval import HelpSection
from rubik_core.help.content import (
    HELP_PAGE_PATH,
    HELP_SECTIONS,
    help_link,
    render_help_report,
    search_help,
    section_directory,
)

__all__ = [
    # Shared
    "HelpSection",
    # Cubie's own Help page
    "HELP_PAGE_PATH",
    "HELP_SECTIONS",
    "help_link",
    "render_help_report",
    "search_help",
    "section_directory",
]
