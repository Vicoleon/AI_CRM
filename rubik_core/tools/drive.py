import logging
import re
from typing import Any

from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Reading Google Drive Labels needs scopes BEYOND the plain `…/auth/drive` above
# and a separate Workspace-admin DWD grant (same situation as gmail.modify). These
# are used ONLY by an isolated, lazily-built service in `list_recent_labeled_files`
# — never added to the shared `self.service` — so that a missing grant fails only
# the label read, not meeting-prep / transcript-sync / the rest of Drive.
LABELS_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.labels.readonly",
]


class GoogleDriveClient:
    def __init__(self, user_email: str):
        """
        Initializes the Google Drive API client for a specific user.
        Always uses the authenticated user's email via domain-wide delegation.
        """
        self.user_email = user_email
        self.service = self._build_service()

    def _build_service(self):
        """Builds the Drive API service using domain-wide delegation."""
        from rubik_core.tools._workspace_auth import google_workspace_configured

        if not google_workspace_configured():
            logger.error("Workspace DWD credentials are not configured.")
            return None

        try:
            from rubik_core.tools._workspace_auth import (
                load_workspace_credentials,
                safe_refresh,
            )

            creds = load_workspace_credentials(SCOPES)
            if creds is None:
                return None

            # Domain-wide delegation: impersonate the user
            delegated_creds = creds.with_subject(self.user_email)
            # UC-40.2: serialize OAuth refresh — concurrent refreshes across
            # parallel channel agents race on SSL state and produce SSLEOFError.
            safe_refresh(delegated_creds)
            return build(
                "drive", "v3", credentials=delegated_creds, cache_discovery=False
            )
        except Exception as e:
            logger.error(
                f"Failed to initialize Google Drive client for {self.user_email}: {e}"
            )
            return None

    def get_meet_recordings_folder_id(self) -> str | None:
        """Finds the 'Meet Recordings' folder in the user's My Drive."""
        if not self.service:
            return None
        try:
            query = "name='Meet Recordings' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = (
                self.service.files()
                .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
                .execute()
            )
            files = results.get("files", [])
            if not files:
                logger.info(f"'Meet Recordings' folder not found for {self.user_email}")
                return None
            return files[0].get("id")
        except Exception as e:
            logger.error(
                f"Error finding Meet Recordings folder for {self.user_email}: {e}"
            )
            return None

    def list_recent_files(
        self,
        *,
        days_back: int = 7,
        max_results: int = 25,
    ) -> list[dict[str, Any]]:
        """List the user's recently modified Drive files (any mime type).

        Powers the UC-29 ``DriveChannelAgent``. Returns files modified
        within the lookback window across "My Drive" (owners include
        the user) plus shared-with-me items, so the decision-maker can
        correlate a doc the user touched yesterday with a meeting on
        the calendar today.

        Each result includes the fields the channel agent needs:
        ``id``, ``name``, ``mimeType``, ``modifiedTime``, ``createdTime``,
        ``lastModifyingUser.emailAddress``, ``owners[].emailAddress``,
        ``shared``, ``webViewLink``.
        """
        if not self.service:
            return []
        try:
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
            cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
            # Drive's `q` does not accept a top-level OR with mimeType, so
            # we filter to non-folder items and let UC-29 decide which
            # mime types to highlight per attrs.
            query = (
                f"modifiedTime > '{cutoff_iso}' "
                "and trashed = false "
                "and mimeType != 'application/vnd.google-apps.folder'"
            )
            results = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    corpora="user",
                    fields=(
                        "files("
                        "id, name, mimeType, modifiedTime, createdTime, "
                        "lastModifyingUser(emailAddress, displayName), "
                        "owners(emailAddress, displayName), shared, "
                        "webViewLink, viewedByMe, viewedByMeTime"
                        ")"
                    ),
                    pageSize=max_results,
                    orderBy="modifiedTime desc",
                )
                .execute()
            )
            return results.get("files", []) or []
        except Exception as e:
            logger.error(f"Error listing recent Drive files for {self.user_email}: {e}")
            return []

    # ── Drive Labels (grant-gated) — backs the AI-Priorities Drive signal ────

    def _build_labeled_credentials(self):
        """Build DWD credentials scoped for Drive Labels reads (isolated).

        Mirrors ``_build_service``'s credential loading but with
        ``LABELS_SCOPES``. Kept separate and built lazily so a missing
        ``drive.labels.readonly`` grant can only fail the label path.
        Returns ``None`` (logged, not raised) if DWD isn't configured or
        the token request fails.
        """
        from rubik_core.tools._workspace_auth import google_workspace_configured

        if not google_workspace_configured():
            return None
        try:
            from rubik_core.tools._workspace_auth import (
                load_workspace_credentials,
                safe_refresh,
            )

            creds = load_workspace_credentials(LABELS_SCOPES)
            if creds is None:
                return None
            delegated = creds.with_subject(self.user_email)
            safe_refresh(delegated)
            return delegated
        except Exception as e:
            # Almost always "scope not granted yet" — expected until the
            # Workspace admin authorizes the labels scope for the SA client id.
            logger.warning(
                "Drive Labels scope unavailable for %s (DWD grant pending?): %s",
                self.user_email,
                e,
            )
            return None

    def _published_label_choice_index(self, creds) -> dict[str, str]:
        """Map every label choice id → its human display name, across published labels.

        Drive's ``files.list`` returns *selection* labels as opaque choice ids;
        the human value ("Important", "Cubie", …) lives in the label taxonomy
        served by the Drive Labels API (``drivelabels`` v2). We fetch it once per
        run and use it to resolve ``labelInfo`` selections to display names.

        ⚠️ Greenfield — the exact ``drivelabels`` response shape must be confirmed
        against the live API once the DWD grant lands. Defensive
        by design: returns ``{}`` on any error, which simply means selection
        labels can't be resolved (text labels still work).
        """
        index: dict[str, str] = {}
        try:
            svc = build("drivelabels", "v2", credentials=creds, cache_discovery=False)
            resp = (
                svc.labels().list(view="LABEL_VIEW_FULL", publishedOnly=True).execute()
            )
            for label in resp.get("labels", []) or []:
                for field in label.get("fields", []) or []:
                    choices = (field.get("selectionOptions") or {}).get("choices", [])
                    for choice in choices:
                        cid = choice.get("id")
                        name = (choice.get("properties") or {}).get("displayName")
                        if cid and name:
                            index[cid] = name
        except Exception as e:
            logger.warning(
                "Could not load Drive Labels taxonomy for %s: %s", self.user_email, e
            )
        return index

    def list_recent_labeled_files(
        self,
        *,
        since: "datetime | None" = None,  # noqa: F821
        days_back: int = 1,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """Recent Drive files carrying a Drive Label, in a normalized shape.

        Returns one dict per labeled file::

            {"id", "name", "web_view_link", "modified_time" (iso str),
             "owner" (email|None), "label_values": list[str]}

        ``label_values`` are resolved human strings (selection display names +
        text values), which the Drive AI-Priorities signal matches against the
        admin label→priority catalog. Only files with at least one resolved
        label value are returned.

        Uses an ISOLATED labels-scoped service (``_build_labeled_credentials``);
        returns ``[]`` on any error (grant pending, API disabled, etc.) so the
        signal source no-ops cleanly until the scope is granted.
        """
        from datetime import datetime, timedelta, timezone

        creds = self._build_labeled_credentials()
        if creds is None:
            return []
        try:
            cutoff = since or (datetime.now(timezone.utc) - timedelta(days=days_back))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
            cutoff_iso = (
                cutoff.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )

            svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            choice_index = self._published_label_choice_index(creds)

            query = (
                f"modifiedTime > '{cutoff_iso}' "
                "and trashed = false "
                "and mimeType != 'application/vnd.google-apps.folder'"
            )
            results = (
                svc.files()
                .list(
                    q=query,
                    spaces="drive",
                    corpora="user",
                    fields=(
                        "files(id, name, mimeType, modifiedTime, webViewLink, "
                        "owners(emailAddress), labelInfo)"
                    ),
                    pageSize=max_results,
                    orderBy="modifiedTime desc",
                    includeLabels="*",
                )
                .execute()
            )

            out: list[dict[str, Any]] = []
            for f in results.get("files", []) or []:
                values = extract_label_values(f.get("labelInfo"), choice_index)
                if not values:
                    continue
                owners = f.get("owners") or []
                out.append(
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "mime_type": f.get("mimeType"),
                        "web_view_link": f.get("webViewLink"),
                        "modified_time": f.get("modifiedTime"),
                        "owner": owners[0].get("emailAddress") if owners else None,
                        "label_values": values,
                    }
                )
            return out
        except Exception as e:
            logger.warning(
                "Error listing labeled Drive files for %s: %s", self.user_email, e
            )
            return []

    def list_transcripts(
        self, folder_id: str, limit: int = 50, days_ago: int | None = None
    ) -> list[dict[str, Any]]:
        """Lists Google Docs, Word, and PDF files within a specific folder."""
        if not self.service:
            return []
        try:
            from datetime import datetime, timedelta, timezone

            # We want Google Docs, Word files, and PDFs
            mimetypes = [
                "application/vnd.google-apps.document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/pdf",
            ]
            query_parts = [f"mimeType='{m}'" for m in mimetypes]
            query = f"'{folder_id}' in parents and ({' or '.join(query_parts)}) and trashed=false"

            if days_ago:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days_ago)
                rfc3339_cutoff = cutoff.isoformat().replace("+00:00", "Z")
                query += f" and (modifiedTime > '{rfc3339_cutoff}' or createdTime > '{rfc3339_cutoff}')"

            results = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)",
                    pageSize=limit,
                    orderBy="createdTime desc",
                )
                .execute()
            )
            return results.get("files", [])
        except Exception as e:
            logger.error(
                f"Error listing transcripts in folder {folder_id} for {self.user_email}: {e}"
            )
            return []

    def export_document_text(
        self,
        file_id: str,
        mime_type: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> str | None:
        """Extract text from a Drive file.

        Handles Google-native Docs / Sheets / Slides (via ``files.export``) and
        binary ``.docx`` / ``.pdf`` / ``.xlsx`` / ``.txt`` / ``.csv`` (via media
        download + parse). Returns ``None`` for genuinely unsupported types
        (e.g. images, ``.pptx``) or on any error.

        ``max_chars`` (optional) truncates the returned text. Callers that only
        need a content *excerpt* — e.g. the AI-Priorities Drive-label signal —
        pass a cap to bound token cost; existing callers omit it and get the
        full text unchanged.
        """
        if not self.service:
            return None

        # If mime_type isn't provided, fetch it
        if not mime_type:
            try:
                file_metadata = (
                    self.service.files()
                    .get(fileId=file_id, fields="mimeType")
                    .execute()
                )
                mime_type = file_metadata.get("mimeType")
            except Exception as e:
                logger.error(f"Error fetching metadata for {file_id}: {e}")
                return None

        try:
            text: str | None = None

            if mime_type == "application/vnd.google-apps.document":
                # Google Doc: export to plain text.
                content = (
                    self.service.files()
                    .export(fileId=file_id, mimeType="text/plain")
                    .execute()
                )
                text = content.decode("utf-8")

            elif mime_type == "application/vnd.google-apps.spreadsheet":
                # Google Sheet: export to CSV (cell values as text).
                content = (
                    self.service.files()
                    .export(fileId=file_id, mimeType="text/csv")
                    .execute()
                )
                text = content.decode("utf-8", errors="replace")

            elif mime_type == "application/vnd.google-apps.presentation":
                # Google Slides: export to plain text (slide text runs).
                content = (
                    self.service.files()
                    .export(fileId=file_id, mimeType="text/plain")
                    .execute()
                )
                text = content.decode("utf-8", errors="replace")

            elif (
                mime_type
                == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ):
                # .docx: download media and use python-docx.
                import io

                import docx

                request = self.service.files().get_media(fileId=file_id)
                file_stream = io.BytesIO(request.execute())
                doc = docx.Document(file_stream)
                text = "\n".join([para.text for para in doc.paragraphs])

            elif (
                mime_type
                == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ):
                # .xlsx: download media and read cell text with openpyxl
                # (read-only, values only — no formula evaluation). Stops early
                # once max_chars is reached so a huge workbook can't stall us.
                import io

                from openpyxl import load_workbook

                request = self.service.files().get_media(fileId=file_id)
                file_stream = io.BytesIO(request.execute())
                workbook = load_workbook(file_stream, read_only=True, data_only=True)
                lines: list[str] = []
                running = 0
                for sheet in workbook.worksheets:
                    lines.append(f"# Sheet: {sheet.title}")
                    for row in sheet.iter_rows(values_only=True):
                        cells = [str(c) for c in row if c not in (None, "")]
                        if cells:
                            line = "\t".join(cells)
                            lines.append(line)
                            running += len(line)
                        if max_chars is not None and running >= max_chars:
                            break
                    if max_chars is not None and running >= max_chars:
                        break
                workbook.close()
                text = "\n".join(lines)

            elif mime_type == "application/pdf":
                # .pdf: download media and use pdfplumber. Stops early once
                # max_chars is reached to bound work on large PDFs.
                import io

                import pdfplumber

                request = self.service.files().get_media(fileId=file_id)
                file_stream = io.BytesIO(request.execute())
                with pdfplumber.open(file_stream) as pdf:
                    pages: list[str] = []
                    running = 0
                    for page in pdf.pages:
                        page_text = page.extract_text() or ""
                        pages.append(page_text)
                        running += len(page_text)
                        if max_chars is not None and running >= max_chars:
                            break
                    text = "\n".join(pages)

            elif mime_type in ("text/plain", "text/csv", "text/markdown"):
                # Plain-text family: download and decode.
                raw = self.service.files().get_media(fileId=file_id).execute()
                text = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, (bytes, bytearray))
                    else str(raw)
                )

            else:
                logger.warning(
                    f"Unsupported MIME type for text extraction: {mime_type}"
                )
                return None

            if text is None:
                return None
            if max_chars is not None and len(text) > max_chars:
                text = text[:max_chars].rstrip() + "…"
            return text

        except Exception as e:
            logger.error(f"Error extracting text from {file_id} ({mime_type}): {e}")
            return None

    # ── UC-44: folder-scoped Drive surfacing for the Productivity tab ────────

    def list_folders(
        self,
        *,
        query: str = "",
        max_results: int = 50,
        root_only: bool = True,
    ) -> list[dict[str, Any]]:
        """List the user's Drive folders, optionally filtered by name.

        Used by the Documents tab's folder picker. Returns folder
        metadata only (no contents).

        ``root_only`` (default ``True``): only return top-level folders
        in My Drive — what the user sees when they open Drive's web UI.
        Avoids drowning the picker in nested git/dev directory names
        (``.git/objects``, ``.git/refs/origin``, etc.) when Drive
        Desktop sync or one-off uploads put dev folders in Drive.

        When the user types a search query, root-only is automatically
        relaxed so they can still pick a nested folder if needed.
        """
        if not self.service:
            return []
        try:
            clauses = [
                "mimeType='application/vnd.google-apps.folder'",
                "trashed=false",
                # Filter out folders the user doesn't own — shared-with-me
                # entries clutter the picker for what is meant to be
                # "MY Drive folders to surface".
                "'me' in owners",
            ]
            if query:
                # Escape single quotes in the user-supplied query.
                safe = query.replace("'", "\\'")
                clauses.append(f"name contains '{safe}'")
                # Search is broad by design — let it cross nested folders.
            elif root_only:
                # No search → mimic Drive's My-Drive root listing.
                clauses.append("'root' in parents")
            q = " and ".join(clauses)
            results = (
                self.service.files()
                .list(
                    q=q,
                    spaces="drive",
                    corpora="user",
                    fields="files(id, name, parents, webViewLink, modifiedTime)",
                    pageSize=max_results,
                    orderBy="name" if not query else "modifiedTime desc",
                )
                .execute()
            )
            return results.get("files", []) or []
        except Exception as e:
            logger.error(
                f"Error listing folders (query={query!r}) for {self.user_email}: {e}"
            )
            return []

    def list_files_in_folders(
        self,
        folder_ids: list[str],
        *,
        days_back: int = 30,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """List recent non-folder files inside any of the given folder ids.

        Powers the Documents tab when the user has restricted surfacing
        to specific folders. Returns the same fields as
        ``list_recent_files`` so callers can swap one for the other.
        """
        if not self.service or not folder_ids:
            return []
        try:
            from datetime import datetime, timedelta, timezone

            cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
            cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
            # Drive's `q` supports `'<id>' in parents` ORed across folder ids.
            parents_clause = " or ".join(f"'{fid}' in parents" for fid in folder_ids)
            query = (
                f"modifiedTime > '{cutoff_iso}' "
                f"and trashed = false "
                f"and mimeType != 'application/vnd.google-apps.folder' "
                f"and ({parents_clause})"
            )
            results = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    corpora="user",
                    fields=(
                        "files("
                        "id, name, mimeType, modifiedTime, createdTime, "
                        "lastModifyingUser(emailAddress, displayName), "
                        "owners(emailAddress, displayName), shared, "
                        "webViewLink, viewedByMe, viewedByMeTime, parents"
                        ")"
                    ),
                    pageSize=max_results,
                    orderBy="modifiedTime desc",
                )
                .execute()
            )
            return results.get("files", []) or []
        except Exception as e:
            logger.error(
                f"Error listing files in folders {folder_ids} for {self.user_email}: {e}"
            )
            return []

    def download_file_bytes(self, file_id: str) -> bytes | None:
        """Download the raw bytes of a Drive file.

        For native Google Docs / Sheets / Slides, exports to a sensible
        binary format (.docx / .xlsx / .pptx) so downstream ingestion can
        treat them like any other office document. Returns None if the
        file mime is unsupported or the download fails.
        """
        if not self.service:
            return None
        try:
            meta = (
                self.service.files()
                .get(
                    fileId=file_id,
                    fields="id, mimeType, name",
                )
                .execute()
            )
            mime = meta.get("mimeType", "")
            # Google-native types → export to office-friendly binary.
            export_map = {
                "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            if mime in export_map:
                return (
                    self.service.files()
                    .export(
                        fileId=file_id,
                        mimeType=export_map[mime],
                    )
                    .execute()
                )
            # Binary files (PDF, docx, txt, csv, images) — direct media download.
            return self.service.files().get_media(fileId=file_id).execute()
        except Exception as e:
            logger.error(
                f"Error downloading bytes for file {file_id} from Drive "
                f"({self.user_email}): {e}"
            )
            return None

    def get_export_extension(self, mime_type: str) -> str:
        """Return the filename extension we'd use after exporting a Google-native
        file via ``download_file_bytes``. Helps callers build a sensible
        ``original_filename`` for Asset registration.
        """
        extension_map = {
            "application/vnd.google-apps.document": ".docx",
            "application/vnd.google-apps.spreadsheet": ".xlsx",
            "application/vnd.google-apps.presentation": ".pptx",
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
            "text/plain": ".txt",
            "text/csv": ".csv",
        }
        return extension_map.get(mime_type, "")

    def search_transcripts_by_pattern(
        self,
        name_pattern: str = "Notes by Gemini",
        limit: int = 50,
        days_ago: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search across the whole Drive for files containing a specific naming pattern."""
        if not self.service:
            return []
        try:
            from datetime import datetime, timedelta, timezone

            # We look for Google Docs with the specific naming pattern
            query = f"name contains '{name_pattern}' and mimeType='application/vnd.google-apps.document' and trashed=false"

            if days_ago:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days_ago)
                rfc3339_cutoff = cutoff.isoformat().replace("+00:00", "Z")
                query += f" and (modifiedTime > '{rfc3339_cutoff}' or createdTime > '{rfc3339_cutoff}')"

            results = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)",
                    pageSize=limit,
                    orderBy="createdTime desc",
                )
                .execute()
            )
            return results.get("files", [])
        except Exception as e:
            logger.error(
                f"Error searching transcripts by pattern '{name_pattern}' for {self.user_email}: {e}"
            )
            return []

    def extract_participants_from_transcript(self, text: str) -> list[str]:
        """
        Attempts to extract email addresses and names from typical Meet Transcripts.
        Meet transcripts usually list the attendees at the top or have their names throughout.
        This provides a basic extraction using regex for emails.
        """
        # Google Meet transcripts might not directly contain emails if joining outside the org,
        # but if calendar invitations or users are logged in, emails might appear.
        # Often it just lists the names like: "Attendees: John Doe, Jane Smith".

        # This is a naive email extractor to ensure vector DB ACL security.
        # It scans the entire document text for emails.
        # Note: If emails are not present in the text, we might need a more advanced approach
        # integrating with the Calendar API using the meeting title/time to find the exact event & attendees.

        emails = set()
        # Regex to find emails
        email_pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
        matches = re.findall(email_pattern, text)
        for match in matches:
            emails.add(match.lower())

        # Always make sure the owner is included
        emails.add(self.user_email.lower())

        return list(emails)


def extract_label_values(
    label_info: dict | None, choice_index: dict[str, str] | None = None
) -> list[str]:
    """Pull human-readable label strings out of a Drive file's ``labelInfo``.

    Returns selection display names (resolved via ``choice_index``: choice id →
    display name) plus raw text-field values, de-duplicated, order-preserved.
    Defensive against the ``labelInfo`` shape (``fields`` may arrive as a list or
    a dict keyed by field id) — returns ``[]`` when there's nothing usable.

    Pure + side-effect free so the signal logic can be unit-tested without any
    Google API access. ⚠️ The real ``labelInfo`` shape is greenfield; confirm
    against the live API once the DWD grant lands.
    """
    choice_index = choice_index or {}
    if not isinstance(label_info, dict):
        return []
    values: list[str] = []
    for label in label_info.get("labels") or []:
        if not isinstance(label, dict):
            continue
        fields = label.get("fields")
        field_iter = fields.values() if isinstance(fields, dict) else (fields or [])
        for field in field_iter:
            if not isinstance(field, dict):
                continue
            for t in field.get("text") or []:
                if isinstance(t, str) and t.strip():
                    values.append(t.strip())
            for cid in field.get("selection") or []:
                name = choice_index.get(cid)
                if name:
                    values.append(name)
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        k = v.lower()
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out
