"""
Review orchestrator.

Entry point for processing a PR:
  1. Fetch PR metadata + diff files from GitHub
  2. Build RAG index from changed files + related context
  3. Run LLM review agent
  4. Post results back as a GitHub PR review with inline comments
"""

from __future__ import annotations
import logging
import time
from dataclasses import dataclass

from agent.rag_context import RepoIndex
from agent.reviewer import ReviewAgent, ReviewResult
from github.client import GitHubClient

logger = logging.getLogger(__name__)

# File extensions to fetch full content for (skip binaries, generated files)
REVIEWABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".rs",
    ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".swift", ".kt",
}
SKIP_PATTERNS = {
    "package-lock.json",
    "yarn.lock",
    ".min.js",
    "_pb2.py",
}
SKIP_DIRECTORIES = {
    ".next",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "vendor",
}


@dataclass
class OrchestratorConfig:
    max_files: int = 20           # skip PRs with > N files
    max_file_size_bytes: int = 100_000
    fetch_full_content: bool = True
    include_repo_context: bool = False
    repo_context_file_limit: int = 200
    rag_context_chunks: int = 5
    post_review: bool = True      # False = dry run, log only


class ReviewOrchestrator:
    def __init__(
        self,
        github: GitHubClient,
        agent: ReviewAgent,
        config: OrchestratorConfig | None = None,
    ):
        self.github = github
        self.agent = agent
        self.config = config or OrchestratorConfig()

    def process_pr(self, owner: str, repo: str, pr_number: int) -> ReviewResult | None:
        start = time.perf_counter()
        logger.info(f"Starting review: {owner}/{repo}#{pr_number}")

        pr = self.github.get_pull_request(owner, repo, pr_number)
        files = self.github.get_pr_files(owner, repo, pr_number)

        # Filter down to reviewable files
        files = [
            f for f in files
            if self._should_review(f.filename)
        ][:self.config.max_files]

        if not files:
            logger.info("No reviewable files found — skipping")
            return None

        logger.info(f"Reviewing {len(files)} files in PR '{pr.title}'")

        # Fetch full file content for changed files
        file_contents: dict[str, str] = {}
        if self.config.fetch_full_content:
            for f in files:
                content = self.github.get_file_content(owner, repo, f.filename, pr.head_sha)
                if content:
                    file_contents[f.filename] = content

        if self.config.include_repo_context:
            repo_context_files = [
                filename
                for filename in self.github.get_repository_files(owner, repo, pr.head_sha)
                if self._should_review(filename)
            ][:self.config.repo_context_file_limit]
            for filename in repo_context_files:
                if filename in file_contents:
                    continue
                content = self.github.get_file_content(owner, repo, filename, pr.head_sha)
                if content:
                    file_contents[filename] = content

            # Build RAG index and retrieve related context.
            rag_index = RepoIndex()
            for filename, content in file_contents.items():
                rag_index.add_file(filename, content)
            rag_index.build()

            diff_text = " ".join(f.patch for f in files)
            related = rag_index.query(diff_text, top_k=self.config.rag_context_chunks)
            for chunk in related:
                key = f"[context] {chunk.header}"
                file_contents[key] = chunk.content

        # Run the review
        result = self.agent.review_pr(pr, files, file_contents)

        elapsed = time.perf_counter() - start
        logger.info(
            f"Review complete in {elapsed:.1f}s — "
            f"{len(result.findings)} findings, verdict={result.verdict}"
        )

        # Post back to GitHub
        if self.config.post_review and result.comments:
            try:
                self.github.post_review(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=pr.head_sha,
                    comments=result.comments,
                    summary=result.summary,
                    event=result.verdict,
                )
                logger.info(f"Posted review with {len(result.comments)} inline comments")
            except Exception as e:
                logger.error(f"Failed to post review: {e}")

        return result

    def _should_review(self, filename: str) -> bool:
        normalized = filename.replace("\\", "/")
        parts = normalized.split("/")
        if any(part in SKIP_DIRECTORIES for part in parts):
            return False
        if any(skip in filename for skip in SKIP_PATTERNS):
            return False
        ext = "." + normalized.rsplit(".", 1)[-1] if "." in normalized else ""
        return ext in REVIEWABLE_EXTENSIONS
