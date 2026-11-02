# rubik_core/dam/audit.py
import logging
from typing import Any

from rubik_core.database.repository import log_audit_action

logger = logging.getLogger(__name__)


def log_action(
    actor_email: str, action: str, doc_id: str, status: str, details: Any = None
):
    """
    Logs an action to the audit database using the SQLAlchemy repository.
    status: 'success' or 'failure'
    """
    try:
        log_audit_action(actor_email, action, doc_id, status, details)
        logger.info(f"Audit Log: {actor_email} {action} {doc_id} -> {status}")
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")
