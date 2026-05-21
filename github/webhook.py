"""Verifies GitHub webhook signatures and extracts PR open/sync events."""

from __future__ import annotations
import hashlib
import hmac
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class PREvent:
    action: str          # opened | synchronize | reopened
    owner: str
    repo: str
    pr_number: int
    head_sha: str
    sender: str


def verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """Check the X-Hub-Signature-256 header (HMAC-SHA256 of the raw body)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def parse_pr_event(payload: dict) -> PREvent | None:
    """Return a PREvent for opened/synchronize/reopened PRs, else None."""
    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return None

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    owner = repo.get("owner", {}).get("login", "")
    repo_name = repo.get("name", "")

    if not owner or not repo_name:
        logger.warning("Webhook payload missing owner or repo name")
        return None

    return PREvent(
        action=action,
        owner=owner,
        repo=repo_name,
        pr_number=pr.get("number", 0),
        head_sha=pr.get("head", {}).get("sha", ""),
        sender=payload.get("sender", {}).get("login", ""),
    )
