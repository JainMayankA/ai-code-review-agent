"""FastAPI app: GitHub webhook handler plus a manual review trigger."""

from __future__ import annotations
import logging
import os
import threading
from collections import deque

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from agent.orchestrator import ReviewOrchestrator, OrchestratorConfig
from agent.reviewer import ReviewAgent
from github.client import GitHubClient
from github.webhook import parse_pr_event, verify_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI Code Review Agent", version="1.0.0")

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
OPENAI_KEY = os.getenv("OPENAI_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
REVIEW_REPO_CONTEXT = os.getenv("REVIEW_REPO_CONTEXT", "false").lower() == "true"

# Dedupe webhook retries by commit (owner/repo#pr@sha). Bounded so it can't grow
# forever; a force-push has a new sha and still triggers a fresh review.
_MAX_SEEN = 500
_seen_lock = threading.Lock()
_seen_keys: set[str] = set()
_seen_order: deque[str] = deque()


def _mark_seen(key: str) -> bool:
    """Return True if this is the first time we've seen this key."""
    with _seen_lock:
        if key in _seen_keys:
            return False
        _seen_keys.add(key)
        _seen_order.append(key)
        if len(_seen_order) > _MAX_SEEN:
            _seen_keys.discard(_seen_order.popleft())
        return True


def get_orchestrator() -> ReviewOrchestrator:
    github = GitHubClient(token=GITHUB_TOKEN)
    agent = ReviewAgent(api_key=OPENAI_KEY)
    config = OrchestratorConfig(
        post_review=not DRY_RUN,
        include_repo_context=REVIEW_REPO_CONTEXT,
    )
    return ReviewOrchestrator(github=github, agent=agent, config=config)


@app.get("/health")
def health():
    return {"status": "ok", "dry_run": DRY_RUN}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
):
    body = await request.body()

    # Reject anything not signed with our webhook secret.
    if WEBHOOK_SECRET:
        if not verify_signature(body, x_hub_signature_256 or "", WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if x_github_event != "pull_request":
        return {"status": "ignored", "event": x_github_event}

    payload = await request.json()
    event = parse_pr_event(payload)

    if not event:
        return {"status": "ignored", "reason": "not a PR open/sync event"}

    key = f"{event.owner}/{event.repo}#{event.pr_number}@{event.head_sha}"
    if not _mark_seen(key):
        logger.info("Duplicate webhook ignored: %s", key)
        return {"status": "duplicate", "pr": event.pr_number}

    logger.info(f"Webhook: PR #{event.pr_number} {event.action} in {event.owner}/{event.repo}")

    def run_review():
        try:
            orchestrator = get_orchestrator()
            orchestrator.process_pr(event.owner, event.repo, event.pr_number)
        except Exception:
            logger.exception("Review failed for PR #%s in %s/%s", event.pr_number, event.owner, event.repo)

    background_tasks.add_task(run_review)
    return {"status": "accepted", "pr": event.pr_number, "action": event.action}


class ManualReviewRequest(BaseModel):
    owner: str
    repo: str
    pr_number: int
    dry_run: bool = False


@app.post("/review")
def manual_review(req: ManualReviewRequest, background_tasks: BackgroundTasks):
    """Trigger a review manually, e.g. for testing without a live webhook."""
    def run():
        try:
            github = GitHubClient(token=GITHUB_TOKEN)
            agent = ReviewAgent(api_key=OPENAI_KEY)
            config = OrchestratorConfig(
                post_review=not req.dry_run,
                include_repo_context=REVIEW_REPO_CONTEXT,
            )
            orch = ReviewOrchestrator(github=github, agent=agent, config=config)
            result = orch.process_pr(req.owner, req.repo, req.pr_number)
            if result:
                logger.info(
                    "Manual review done: %d findings, verdict=%s",
                    len(result.findings), result.verdict,
                )
        except Exception:
            logger.exception("Manual review failed for %s/%s#%s", req.owner, req.repo, req.pr_number)

    background_tasks.add_task(run)
    return {"status": "accepted", "pr": req.pr_number}
