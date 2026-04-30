"""
FastAPI server.
Handles GitHub webhook events and exposes a manual review trigger endpoint.
"""

from __future__ import annotations
import logging
import os
from typing import Optional

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
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None),
):
    body = await request.body()

    # Validate signature — reject anything not from GitHub
    if WEBHOOK_SECRET:
        if not verify_signature(body, x_hub_signature_256 or "", WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if x_github_event != "pull_request":
        return {"status": "ignored", "event": x_github_event}

    payload = await request.json()
    event = parse_pr_event(payload)

    if not event:
        return {"status": "ignored", "reason": "not a PR open/sync event"}

    logger.info(f"Webhook: PR #{event.pr_number} {event.action} in {event.owner}/{event.repo}")

    def run_review():
        orchestrator = get_orchestrator()
        orchestrator.process_pr(event.owner, event.repo, event.pr_number)

    background_tasks.add_task(run_review)
    return {"status": "accepted", "pr": event.pr_number, "action": event.action}


class ManualReviewRequest(BaseModel):
    owner: str
    repo: str
    pr_number: int
    dry_run: bool = False


@app.post("/review")
def manual_review(req: ManualReviewRequest, background_tasks: BackgroundTasks):
    """Trigger a review manually — useful for testing without a live webhook."""
    def run():
        github = GitHubClient(token=GITHUB_TOKEN)
        agent = ReviewAgent(api_key=OPENAI_KEY)
        config = OrchestratorConfig(
            post_review=not req.dry_run,
            include_repo_context=REVIEW_REPO_CONTEXT,
        )
        orch   = ReviewOrchestrator(github=github, agent=agent, config=config)
        result = orch.process_pr(req.owner, req.repo, req.pr_number)
        if result:
            logger.info(
                f"Manual review done: {len(result.findings)} findings, "
                f"verdict={result.verdict}"
            )

    background_tasks.add_task(run)
    return {"status": "accepted", "pr": req.pr_number}
