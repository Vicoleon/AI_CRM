import logging
from typing import Any

from googleapiclient.discovery import build

from rubik_core.tools._workspace_auth import workspace_dwd_subject

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/admin.directory.user.readonly"]


class GoogleDirectoryClient:
    def __init__(self, admin_email: str = None):
        """
        Initializes the Google Admin Directory API client.
        Uses the provided admin email for domain-wide delegation.
        """
        self.admin_email = admin_email or workspace_dwd_subject()
        self.service = self._build_service()

    def _build_service(self):
        """Builds the Admin Directory API service using domain-wide delegation."""
        if not self.admin_email:
            raise ValueError("Workspace DWD admin subject is not configured.")

        try:
            from rubik_core.tools._workspace_auth import load_workspace_credentials

            creds = load_workspace_credentials(SCOPES)
            if creds is None:
                raise ValueError("Workspace DWD credentials are not configured.")

            # Domain-wide delegation: impersonate the admin
            logger.info("Building Directory service with subject: %s", self.admin_email)
            delegated_creds = creds.with_subject(self.admin_email)
            return build(
                "admin",
                "directory_v1",
                credentials=delegated_creds,
                cache_discovery=False,
            )
        except Exception as e:
            logger.error(
                f"Failed to initialize Google Directory client for {self.admin_email}: {e}"
            )
            raise RuntimeError(f"Directory Service Init Failed: {str(e)}")

    @staticmethod
    def _build_queries(query_text: str) -> list[str]:
        """
        Build a prioritized list of Admin SDK ``query`` clauses for a raw
        search term.

        The Admin Directory API does NOT accept a bare term (e.g. ``jordan``) —
        every clause must be ``field:operator:value`` or it returns
        ``400 Invalid Input: query``. The API also has no OR operator, so we
        fan a term out across the relevant fields and merge the results.

        - ``:`` does a prefix match when the value ends in ``*`` (good for
          incremental autocomplete typing).
        - Multi-word terms are treated as a full name.
        """
        term = query_text.strip()
        if not term:
            return []

        # Email (or partial email) — match the email/alias fields directly.
        if "@" in term:
            return [f"email:{term}", f"email:{term}*"]

        parts = term.split()
        if len(parts) > 1:
            first, last = parts[0], parts[-1]
            return [
                f"name:'{term}'",  # whole words in order, e.g. "Jordan Hayes"
                f"givenName:{first}* familyName:{last}*",  # AND of prefixes
            ]

        # Single token: prefix-match the most likely fields someone types.
        return [
            f"givenName:{term}*",
            f"email:{term}*",
            f"familyName:{term}*",
        ]

    def suspended_emails(self, emails: Any) -> set[str]:
        """Return the subset of ``emails`` whose Workspace account is deactivated.

        "Deactivated" == ``suspended`` OR ``archived`` on the Admin Directory
        user record — either state means the account can no longer be
        DWD-impersonated, so every gmail/calendar/drive/meet feed for that user
        is (or will be) empty. Backs the offboarded-user gate
        (:class:`rubik_core.directory.active_status.GoogleDirectorySuspendedSource`).

        One ``users.get`` per email (the candidate set is a single team's
        roster, not the whole directory). Conservative on purpose:

        * A user is added ONLY on a positive ``suspended``/``archived`` flag.
        * A ``404`` (email isn't a directory user — an alias, a group, a typo)
          is skipped, never treated as deactivated.
        * The FIRST ``401``/``403`` (scope not granted, or delegation broken)
          raises, so the caller self-disables instead of firing N failing calls.
        """
        from googleapiclient.errors import HttpError

        wanted = sorted(
            {(e or "").strip().lower() for e in emails if (e or "").strip()}
        )
        if not wanted:
            return set()
        if not self.service:
            raise RuntimeError("Directory service not initialized.")

        deactivated: set[str] = set()
        for email in wanted:
            try:
                user = (
                    self.service.users()
                    .get(userKey=email, fields="primaryEmail,suspended,archived")
                    .execute()
                )
            except HttpError as e:
                status = getattr(getattr(e, "resp", None), "status", None)
                if status in (401, 403):
                    # Auth / scope failure — don't hammer the rest of the roster.
                    logger.info(
                        "Directory suspended-check auth error (%s) — aborting", status
                    )
                    raise
                if status == 404:
                    continue  # not a directory user; not evidence of deactivation
                logger.warning("Directory users.get failed for %s: %s", email, e)
                continue
            except Exception as e:  # noqa: BLE001
                logger.warning("Directory users.get errored for %s: %s", email, e)
                continue
            if user.get("suspended") or user.get("archived"):
                deactivated.add(email)
        return deactivated

    def search_users(
        self, query_text: str, max_results: int = 5
    ) -> list[dict[str, Any]]:
        """
        Searches for users in the directory by name or email.

        Runs each candidate query independently and merges the results so a
        single malformed/erroring clause can't abort the whole search.
        """
        if not self.service:
            logger.error("Search called but service is None")
            return []

        queries = self._build_queries(query_text)
        if not queries:
            return []

        logger.info(
            "Searching directory for %r via %d queries", query_text, len(queries)
        )

        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        last_error: Exception | None = None

        for q in queries:
            if len(merged) >= max_results:
                break
            try:
                results = (
                    self.service.users()
                    .list(
                        customer="my_customer",
                        query=q,
                        maxResults=max_results,
                        orderBy="email",
                    )
                    .execute()
                )
            except Exception as e:
                # A bad clause (or a transient API error) shouldn't kill the
                # other strategies — log and move on.
                logger.warning("Directory query %r failed: %s", q, e)
                last_error = e
                continue

            for user in results.get("users", []):
                uid = user.get("id") or user.get("primaryEmail")
                if uid and uid not in seen:
                    seen.add(uid)
                    merged.append(user)

        # Only surface an error if EVERY query failed (e.g. auth/scope/config),
        # so the caller can distinguish "no matches" from "search broke".
        if not merged and last_error is not None:
            logger.error(
                "All directory queries failed for %r: %s", query_text, last_error
            )
            raise last_error

        return merged[:max_results]
