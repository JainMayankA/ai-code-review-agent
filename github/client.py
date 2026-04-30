"""
GitHub API client.
Fetches PR diffs and file contents, posts inline review comments.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"


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
    path: Optional[str]
    line: Optional[int]
    body: str
    severity: str = "comment"  # comment | warning | error


class GitHubClient:
    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("GITHUB_TOKEN", "")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, url: str) -> dict | list:
        with httpx.Client(headers=self.headers, timeout=15) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()

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
        data = self._get(f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files")
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
        data = self._get(f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
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
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.post(url, json=body)
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
        with httpx.Client(headers=self.headers, timeout=30) as client:
            resp = client.post(url, json={"body": body})
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
