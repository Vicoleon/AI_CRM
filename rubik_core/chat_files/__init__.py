"""Files a person attaches in Ask Cubie: read once, private to them for good.

A person drops a Word document, a PDF, a spreadsheet, a deck or a picture into
the chat box. ``POST /api/chat/files`` (``backend/routers/chat_files.py``):

1. checks and reads it locally (:mod:`.extract`): the text of a document, the
   cells of a spreadsheet, the words on a slide, a picture shrunk for vision;
2. hands it to a small model once (:mod:`.reader`) for a summary and, for a
   picture or a scanned PDF, the text and what it shows;
3. keeps the original in the bucket (:mod:`.storage`) and the reading in
   ``chat_uploads``, a table of its own;
4. files that reading in the person's PRIVATE library (:mod:`.private_index`),
   so a later conversation can find it. When that library is not available,
   the file is still read and still used, but only in the chats it is attached
   to (``index_status = "chat_only"``).

When the message is sent, the reading (and, for a picture, the picture itself,
so the chat model sees it) goes into that one turn (:func:`.service.turn_parts`).
The agent's ``search_my_files`` tool reaches the person's files later.

PRIVATE MEANS THE OWNER, AND NOBODY ELSE: no admin, exec or department rule
reaches these files. That is structural, not a flag every reader must honour:

* the files are NOT ``assets`` rows, so the Library, its admin view, the
  department fallback in ``has_document_access``, sharing and the offboarding
  hand-off never see them;
* the Vertex corpus is NOT a ``rag/config.py`` registry slug, so no fan-out
  (``RagManager.query``, ``rags_for_user_email``) reaches it; every search of it
  names the owner's own files (``rag_file_ids``) and is re-checked in SQL;
* every row is read through an ``owner_email == caller`` filter;
* the routes are not on the impersonation allowlist
  (``rubik_core/auth/impersonation.py``), so an admin viewing Cubie as someone
  gets a 403, and the impersonated chat has no file tool.
"""

from __future__ import annotations

#: Admin → Feature Access row. Who may attach files in Ask Cubie.
FEATURE_KEY = "chat_file_upload"

#: The AI Cost category. ``@attribute_to(COST_FEATURE)`` sits on
#: ``reader.read_file``; the ``_FEATURE_LABELS`` entry in
#: ``backend/routers/admin.py`` is pinned by a test.
COST_FEATURE = "chat_file_upload"

#: ``chat_uploads.index_status`` values.
INDEX_PENDING = "pending"
INDEX_PRIVATE = "private_library"
INDEX_CHAT_ONLY = "chat_only"
