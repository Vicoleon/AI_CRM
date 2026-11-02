import base64
import html
import logging
import re
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote

from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Thread hydrations per Gmail batch round trip. Google advises against batches
# above 50: larger ones start drawing per-user rate limiting.
_HYDRATE_BATCH = 50

# Minimal, dependency-free markdown subset for AI-drafted email bodies. The
# drafters emit light markdown (bold, italic, links, bullet lists, headings);
# without conversion those tokens reach the user as literal `**`/`[x](y)` text.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)")
_MD_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*)")
_SAFE_URL_RE = re.compile(r"^(?:https?:|mailto:)", re.IGNORECASE)


def _md_inline(escaped: str) -> str:
    """Apply inline markdown (links, bold, italic) to an ALREADY-escaped line.

    Runs on html-escaped text so no raw HTML can be injected; only http(s)/
    mailto links are linkified (anything else is left as literal text).
    """

    def _link(m: "re.Match[str]") -> str:
        label, url = m.group(1), m.group(2)
        if not _SAFE_URL_RE.match(html.unescape(url)):
            return m.group(0)  # unsafe scheme → leave the markdown as text
        return f'<a href="{url}">{label}</a>'

    s = _MD_LINK_RE.sub(_link, escaped)
    s = _MD_BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = _MD_ITALIC_RE.sub(r"<em>\1</em>", s)
    return s


def _body_to_html(text: str) -> str:
    """Render a plain-text / markdown body to HTML for the draft's HTML part.

    Escapes first (no raw-HTML injection), then applies a small, dependency-free
    markdown subset — bold, italic, safe links, bullet lists, headings — so the
    AI drafter's markdown doesn't reach the user as literal syntax. Sits in the
    HTML part alongside the rich (HTML) signature; the plain-text alternative
    keeps the original markdown, which reads fine unrendered.
    """
    out: list[str] = []
    bullets: list[str] = []

    def _flush_bullets() -> None:
        if bullets:
            out.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for line in text.split("\n"):
        bullet = _MD_BULLET_RE.match(line)
        if bullet:
            bullets.append(_md_inline(html.escape(bullet.group(1))))
            continue
        _flush_bullets()

        heading = _MD_HEADING_RE.match(line)
        if heading:
            level = min(len(heading.group(1)) + 2, 6)  # markdown ## → <h4>, capped
            out.append(
                f"<h{level}>{_md_inline(html.escape(heading.group(2)))}</h{level}>"
            )
            continue

        if not line.strip():
            out.append("<br>")
            continue

        out.append(_md_inline(html.escape(line)) + "<br>")

    _flush_bullets()
    return "\n".join(out)


def _html_to_text(markup: str) -> str:
    """Best-effort HTML → plain text for the message's text/plain alternative.

    Keeps the words of the signature (incl. any confidentiality disclaimer)
    for clients that render the plain part. Converts breaks to newlines, strips
    tags, unescapes entities, and collapses runs of blank lines.
    """
    s = re.sub(r"(?i)<br\s*/?>", "\n", markup)
    s = re.sub(r"(?i)</p\s*>", "\n\n", s)
    s = re.sub(r"(?i)</div\s*>", "\n", s)
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


# Essential scopes for read-only thread inspection and draft creation
# IMPORTANT: Send scopes (e.g., https://www.googleapis.com/auth/gmail.send) are explicitly excluded to enforce Draft-Only policy.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class GmailClient:
    """
    Gmail API client for the owner's work or explicitly selected extra mailbox.

    The default mailbox uses workspace Domain-Wide Delegation. An extra account
    uses only that owner's workspace-bound OAuth refresh token; callers cannot
    pass an arbitrary email address as an impersonation target.
    """

    def __init__(self, user_email: str, *, account_id: str | None = None):
        self.owner_email = user_email
        self.account_id = account_id
        self.user_email = user_email
        self._oauth_refresh_token: str | None = None
        self.service = self._build_service()

    def _build_service(self):
        """
        Build the resource for the selected mailbox, with no fallback between
        OAuth and Domain-Wide Delegation on a token failure.
        """
        if self.account_id is not None:
            from rubik_core.tenancy.gmail_accounts import (
                get_account_credentials,
                mark_account_reauth_required,
            )

            creds, mailbox_email = get_account_credentials(
                self.owner_email, self.account_id
            )
            self.user_email = mailbox_email
            self._oauth_refresh_token = creds.refresh_token
            # Do not catch credential or refresh failures and then try DWD.
            from rubik_core.tools._workspace_auth import safe_refresh

            try:
                safe_refresh(creds)
            except RefreshError as exc:
                mark_account_reauth_required(
                    self.owner_email,
                    self.account_id,
                    failed_refresh_token=creds.refresh_token,
                )
                raise PermissionError("Reconnect this Gmail account") from exc
            return build("gmail", "v1", credentials=creds, cache_discovery=False)
        try:
            from rubik_core.tools._workspace_auth import (
                load_workspace_credentials,
                safe_refresh,
            )

            creds = load_workspace_credentials(SCOPES)
            if creds is None:
                logger.error("No service account credentials found for Gmail API.")
                return None

            # Crucial: This enforces that all calls act strictly as the end user.
            delegated_creds = creds.with_subject(self.user_email)
            # UC-40.2: serialize the OAuth refresh across all Workspace clients
            # in this process. Concurrent refreshes race on the underlying SSL
            # context and produce SSLEOFError on oauth2.googleapis.com. After
            # this single refresh, API calls reuse the cached token.
            safe_refresh(delegated_creds)
            service = build(
                "gmail", "v1", credentials=delegated_creds, cache_discovery=False
            )
            return service

        except Exception as e:
            logger.error("Failed to initialize Gmail API service: %s", type(e).__name__)
            return None

    def _assert_extra_account_access(self) -> None:
        """Recheck access before each selected-mailbox Google operation."""
        if getattr(self, "account_id", None) is None:
            return
        from rubik_core.tenancy.gmail_accounts import get_account_credentials

        creds, mailbox_email = get_account_credentials(
            self.owner_email, self.account_id
        )
        if (
            creds.refresh_token != self._oauth_refresh_token
            or mailbox_email != self.user_email
        ):
            raise PermissionError("Reconnect this Gmail account")

    def search_threads(
        self, query: str = "", max_results: int = 20
    ) -> list[dict[str, Any]]:
        """
        Searches the user's Gmail threads using standard Gmail query syntax.
        """
        if not self.service:
            raise RuntimeError("Gmail service not initialized.")

        self._assert_extra_account_access()

        try:
            results = (
                self.service.users()
                .threads()
                .list(
                    userId="me",  # 'me' refers to the subject in delegated_creds
                    q=query,
                    maxResults=max_results,
                )
                .execute()
            )

            threads = results.get("threads", [])
            self._assert_extra_account_access()
            return self._hydrate_threads([t["id"] for t in threads])
        except Exception as e:
            logger.error("Failed to search Gmail threads: %s", type(e).__name__)
            raise

    def list_thread_page(
        self, query: str = "", max_results: int = 100, page_token: str | None = None
    ) -> tuple[list[str], str | None]:
        """One page of ``threads.list``: ``(thread ids, next page token)``.

        For callers that need more than one page (Loose Ends reads up to 300
        threads per lane and must know when Gmail had more). ``search_threads``
        stays single-page for everyone else. Raises like ``search_threads``.
        """
        if not self.service:
            raise RuntimeError("Gmail service not initialized.")
        self._assert_extra_account_access()
        params: dict[str, Any] = {
            "userId": "me",
            "q": query,
            "maxResults": max(1, min(int(max_results), 500)),
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            results = self.service.users().threads().list(**params).execute()
        except Exception as e:
            logger.error("Failed to list Gmail threads: %s", type(e).__name__)
            raise
        self._assert_extra_account_access()
        ids = [t["id"] for t in results.get("threads", []) if t.get("id")]
        return ids, results.get("nextPageToken") or None

    def _hydrate_threads(self, thread_ids: list[str]) -> list[dict[str, Any]]:
        """Thread metadata for each id, in the order given.

        One batched round trip per `_HYDRATE_BATCH` threads instead of one call
        per thread: 25 serial `threads.get` calls were most of the Productivity
        → Email load, and every Priorities / Cockpit Gmail sweep paid the same.
        Any thread the batch could not return (throttled, or the whole batch
        call failed) is fetched on its own, so a failure raises exactly as the
        one-by-one loop did.
        """
        if not thread_ids:
            return []
        self._assert_extra_account_access()
        threads_api = self.service.users().threads()
        got: dict[str, dict[str, Any]] = {}

        def _cb(request_id, response, exception):
            if exception is None:
                got[request_id] = response

        for start in range(0, len(thread_ids), _HYDRATE_BATCH):
            self._assert_extra_account_access()
            batch = self.service.new_batch_http_request(callback=_cb)
            for i in range(start, min(start + _HYDRATE_BATCH, len(thread_ids))):
                batch.add(
                    threads_api.get(userId="me", id=thread_ids[i], format="metadata"),
                    request_id=str(i),
                )
            self._assert_extra_account_access()
            try:
                batch.execute()
            except Exception as e:
                logger.warning(
                    "Gmail batch hydrate failed; fetching one by one: %s",
                    type(e).__name__,
                )

        for i, tid in enumerate(thread_ids):
            if str(i) not in got:
                self._assert_extra_account_access()
                got[str(i)] = threads_api.get(
                    userId="me", id=tid, format="metadata"
                ).execute()
        self._assert_extra_account_access()
        return [got[str(i)] for i in range(len(thread_ids))]

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        """
        Retrieves a full thread including message headers and snippets.
        """
        if not self.service:
            raise RuntimeError("Gmail service not initialized.")

        self._assert_extra_account_access()

        try:
            # format='full' gets the raw body, 'metadata' gets headers
            thread = (
                self.service.users()
                .threads()
                .get(userId="me", id=thread_id, format="full")
                .execute()
            )
            self._assert_extra_account_access()
            return thread
        except Exception as e:
            logger.error("Failed to get Gmail thread: %s", type(e).__name__)
            raise

    def get_signature(self) -> str:
        """Return the user's primary Gmail *send-as* signature (HTML), or "".

        Reads ``users.settings.sendAs`` — a call the existing ``gmail.readonly``
        scope already authorizes, so this needs NO extra OAuth grant /
        re-consent. For many companies the send-as signature is where the
        standard confidentiality disclaimer lives, so returning it here is what
        lets every AI-drafted email carry both the personal signature and the
        disclaimer.

        Best-effort: returns "" on any error so draft creation never fails just
        because a signature couldn't be fetched.
        """
        if not self.service:
            return ""
        self._assert_extra_account_access()
        try:
            resp = self.service.users().settings().sendAs().list(userId="me").execute()
            self._assert_extra_account_access()
            send_as = resp.get("sendAs", []) or []
            if not send_as:
                return ""
            # Prefer the primary address; else the one matching the impersonated
            # user; else the first entry.
            primary = next((s for s in send_as if s.get("isPrimary")), None)
            if primary is None:
                primary = next(
                    (
                        s
                        for s in send_as
                        if (s.get("sendAsEmail") or "").lower()
                        == self.user_email.lower()
                    ),
                    send_as[0],
                )
            return (primary.get("signature") or "").strip()
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001 — never let this break drafting
            logger.warning("Could not fetch Gmail signature: %s", type(e).__name__)
            return ""

    def create_draft(
        self,
        to: list[str],
        subject: str,
        body_markdown: str,
        cc: list[str] = None,
        thread_id: str = None,
        attachments: list[tuple[str, str, bytes]] = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates a draft email in the user's Gmail account.
        NEVER sends the email.

        ``in_reply_to`` / ``references`` — optional RFC 5322 threading headers
        (the replied-to message's ``Message-ID``, and the chain before it).
        ``threadId`` alone files the draft in the thread inside Gmail, but the
        recipient's mail client threads on these headers; a reply without them
        lands on the client's side as a new conversation.

        ``attachments`` — optional list of ``(filename, mime_type, data)``
        tuples added after the body (e.g. a Cubie-generated Word doc the user
        forwards from their Assets page). Riding in the raw MIME, they need no
        scope beyond the existing draft-only ``gmail.compose``.

        The user's Gmail send-as signature (which may carry the company's
        confidentiality disclaimer) is appended automatically. A Gmail draft
        created via the API does NOT inherit the signature the way the web
        composer does, so we embed it ourselves — otherwise every AI draft would
        ship without the user's signature or the disclaimer.
        """
        if not self.service:
            raise RuntimeError("Gmail service not initialized.")

        self._assert_extra_account_access()

        try:
            message = EmailMessage()
            message["To"] = ", ".join(to)
            if cc:
                message["Cc"] = ", ".join(cc)
            message["Subject"] = subject
            if in_reply_to:
                message["In-Reply-To"] = in_reply_to.strip()
            if references:
                message["References"] = " ".join(references.split())

            signature_html = self.get_signature()
            if signature_html:
                # Multipart/alternative: plain-text (body + de-HTML'd signature so
                # the disclaimer text survives) plus an HTML part that renders the
                # real signature markup. Gmail shows the HTML part in the draft.
                signature_text = _html_to_text(signature_html)
                plain = body_markdown.rstrip()
                if signature_text:
                    plain = f"{plain}\n\n{signature_text}"
                message.set_content(plain + "\n")
                message.add_alternative(
                    f"<div>{_body_to_html(body_markdown)}</div><br>{signature_html}",
                    subtype="html",
                )
            else:
                # No signature available — preserve the original plain-text draft.
                message.set_content(body_markdown)

            for filename, mime_type, data in attachments or []:
                maintype, _, subtype = (
                    mime_type or "application/octet-stream"
                ).partition("/")
                message.add_attachment(
                    data,
                    maintype=maintype or "application",
                    subtype=subtype or "octet-stream",
                    filename=filename,
                )

            # Encode the message safely
            encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()

            draft_body = {"message": {"raw": encoded_message}}
            if thread_id:
                draft_body["message"]["threadId"] = thread_id

            # The admin may have disabled extra mail while the draft was
            # assembled or its signature was fetched.
            self._assert_extra_account_access()
            draft = (
                self.service.users()
                .drafts()
                .create(userId="me", body=draft_body)
                .execute()
            )

            # Construct a direct URL to the draft in the Gmail web interface
            draft_id = draft.get("id")
            mailbox = quote(self.user_email, safe="")
            draft_url = (
                "https://mail.google.com/mail/?authuser="
                f"{mailbox}#drafts?compose={draft_id}"
            )

            return {"draft_id": draft_id, "gmail_draft_url": draft_url}

        except Exception as e:
            logger.error("Failed to create Gmail draft: %s", type(e).__name__)
            raise
