"""
GitHub API client.
Fetches PR diffs and file contents, posts inline review comments.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"

# Statuses worth retrying: rate-limited or transient server errors.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass
class PRFile:
    filename: str
    status: str
    patch: str
    additions: int
    deletions: int
    raw_url: str


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    head_sha: str
    base_sha: str
    head_branch: str
    base_branch: str
    repo: str
    files: list[PRFile] = field(default_factory=list)


@dataclass
class ReviewComment:
    path: str | None
    line: int | None
    body: str
    severity: str = "comment"  # comment | warning | error


class GitHubClient:
    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, url: str, timeout: int = 15, **kwargs) -> httpx.Response:
        """Send an HTTP request with up to 3 attempts on transient failures."""
        for attempt in range(3):
            try:
                with httpx.Client(headers=self.headers, timeout=timeout) as client:
                    resp = client.request(method, url, **kwargs)
            except httpx.TimeoutException:
                if attempt == 2:
                    raise
                delay = 2 ** attempt
                logger.warning("Request timeout (attempt %d), retrying in %ds: %s", attempt + 1, delay, url)
                time.sleep(delay)
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < 2:
                delay = 2 ** attempt
                logger.warning(
                    "HTTP %s (attempt %d), retrying in %ds: %s",
                    resp.status_code, attempt + 1, delay, url,
                )
                time.sleep(delay)
                continue
            return resp
        return resp  # final attempt; caller decides whether to raise_for_status

    def _get(self, url: str) -> dict | list:
        resp = self._request("GET", url)
        resp.raise_for_status()
        return resp.json()

    def _get_paginated(self, url: str) -> list:
        """Accumulate all pages of a GitHub list endpoint (max 100 per page)."""
        results: list = []
        page = 1
        while True:
            sep = "&" if "?" in url else "?"
            resp = self._request("GET", f"{url}{sep}per_page=100&page={page}")
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> PullRequest:
        resp = self._get(f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}")
        return PullRequest(
            number=resp["number"],
            title=resp["title"],
            body=resp.get("body") or "",
            head_sha=resp["head"]["sha"],
            base_sha=resp["base"]["sha"],
            head_branch=resp["head"]["ref"],
            base_branch=resp["base"]["ref"],
            repo=f"{owner}/{repo}",
        )

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[PRFile]:
        data = self._get_paginated(f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files")
        return [
            PRFile(
                filename=f["filename"],
                status=f["status"],
                patch=f.get("patch", ""),
                additions=f["additions"],
                deletions=f["deletions"],
                raw_url=f.get("raw_url", ""),
            )
            for f in data
            if f.get("patch")
        ]

    def get_repository_files(self, owner: str, repo: str, ref: str) -> list[str]:
        resp = self._request("GET", f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
        resp.raise_for_status()
        data = resp.json()
        if data.get("truncated"):
            logger.warning(
                "Repository tree truncated for %s/%s; some files may be missing from RAG context",
                owner, repo,
            )
        return [
            item["path"]
            for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> str:
        try:
            resp = self._get(f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}?ref={ref}")
            return base64.b64decode(resp["content"]).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Could not fetch %s@%s: %s", path, ref, exc)
            return ""

    def post_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        comments: list[ReviewComment],
        summary: str,
        event: str = "COMMENT",
    ) -> dict:
        icons = {"error": "[ERROR]", "warning": "[WARNING]", "comment": "[COMMENT]"}
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        inline_comments = [c for c in comments if c.path and c.line]
        top_level_comments = [c for c in comments if not c.path or not c.line]
        if top_level_comments:
            summary = summary + "\n\n" + "\n\n".join(
                f"{icons.get(c.severity, '[COMMENT]')} **{c.severity.upper()}**\n\n{c.body}"
                for c in top_level_comments
            )

        body = {
            "commit_id": head_sha,
            "body": summary,
            "event": event,
            "comments": [
                {
                    "path": c.path,
                    "line": c.line,
                    "side": "RIGHT",
                    "body": f"{icons.get(c.severity, '[COMMENT]')} **{c.severity.upper()}**\n\n{c.body}",
                }
                for c in inline_comments
            ],
        }
        resp = self._request("POST", url, timeout=30, json=body)
        if resp.status_code == 422:
            logger.warning("GitHub rejected inline review: %s", resp.text)
            fallback_body = self._format_fallback_review_body(
                summary=summary,
                comments=inline_comments,
                icons=icons,
            )
            return self.post_pr_comment(owner, repo, pr_number, fallback_body)
        if resp.is_error:
            logger.error("GitHub review API error: %s", resp.text)
        resp.raise_for_status()
        return resp.json()

    def post_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> dict:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        resp = self._request("POST", url, timeout=30, json={"body": body})
        if resp.is_error:
            logger.error("GitHub PR comment API error: %s", resp.text)
        resp.raise_for_status()
        return resp.json()

    def _format_fallback_review_body(
        self,
        summary: str,
        comments: list[ReviewComment],
        icons: dict[str, str],
    ) -> str:
        if not comments:
            return summary

        comment_blocks = []
        for comment in comments:
            location = f"`{comment.path}:{comment.line}`" if comment.path and comment.line else ""
            comment_blocks.append(
                f"{location}\n\n"
                f"{icons.get(comment.severity, '[COMMENT]')} **{comment.severity.upper()}**\n\n"
                f"{comment.body}"
            )
        return summary + "\n\n" + "\n\n".join(comment_blocks)
