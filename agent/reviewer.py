"""
LLM review agent.

Uses the OpenAI API with structured JSON output to produce
typed findings (bug, security, performance, style) with exact line numbers.

Architecture:
  1. Build context window: PR metadata + file diffs + full file content
  2. Call OpenAI with a strict JSON response prompt
  3. Parse JSON output into typed ReviewFinding objects
  4. Map findings back to diff line numbers for inline comments
"""

from __future__ import annotations
import logging
import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openai import OpenAI

from agent.diff_parser import ParsedDiff, parse_diff
from github.client import PRFile, PullRequest, ReviewComment

logger = logging.getLogger(__name__)

REVIEW_TOOLS = [
    {
        "name": "report_bug",
        "description": "Report a definite bug: logic error, null dereference, off-by-one, incorrect condition, unhandled exception path.",
        "input_schema": {
            "type": "object",
            "required": ["filename", "line", "description", "suggestion"],
            "properties": {
                "filename":    {"type": "string"},
                "line":        {"type": "integer", "description": "Line number in the new file"},
                "description": {"type": "string", "description": "What the bug is and why it's wrong"},
                "suggestion":  {"type": "string", "description": "Concrete fix"},
            },
        },
    },
    {
        "name": "report_security",
        "description": "Report a security vulnerability: SQL injection, XSS, hardcoded secret, insecure deserialization, SSRF, path traversal, weak crypto.",
        "input_schema": {
            "type": "object",
            "required": ["filename", "line", "vulnerability_type", "description", "suggestion"],
            "properties": {
                "filename":          {"type": "string"},
                "line":              {"type": "integer"},
                "vulnerability_type": {"type": "string"},
                "description":       {"type": "string"},
                "suggestion":        {"type": "string"},
            },
        },
    },
    {
        "name": "report_performance",
        "description": "Report a performance issue: N+1 query, unnecessary loop, missing index hint, blocking I/O in hot path, excessive memory allocation.",
        "input_schema": {
            "type": "object",
            "required": ["filename", "line", "description", "suggestion"],
            "properties": {
                "filename":    {"type": "string"},
                "line":        {"type": "integer"},
                "description": {"type": "string"},
                "suggestion":  {"type": "string"},
                "impact":      {"type": "string", "description": "Estimated performance impact"},
            },
        },
    },
    {
        "name": "report_style",
        "description": "Report a minor style / maintainability issue. Only use this sparingly for things that genuinely hurt readability.",
        "input_schema": {
            "type": "object",
            "required": ["filename", "line", "description"],
            "properties": {
                "filename":    {"type": "string"},
                "line":        {"type": "integer"},
                "description": {"type": "string"},
                "suggestion":  {"type": "string"},
            },
        },
    },
    {
        "name": "post_summary",
        "description": "Post the overall review summary. Always call this exactly once after all findings.",
        "input_schema": {
            "type": "object",
            "required": ["summary", "verdict"],
            "properties": {
                "summary": {"type": "string", "description": "Markdown summary of the full review"},
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": "APPROVE if no bugs/security issues; REQUEST_CHANGES if bugs or security found; COMMENT otherwise",
                },
                "bugs_found":     {"type": "integer"},
                "security_found": {"type": "integer"},
                "perf_found":     {"type": "integer"},
            },
        },
    },
]

SEVERITY_MAP = {
    "report_bug":         "error",
    "report_security":    "error",
    "report_performance": "warning",
    "report_style":       "comment",
}


@dataclass
class ReviewFinding:
    tool: str
    filename: str
    line: int
    description: str
    suggestion: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return SEVERITY_MAP.get(self.tool, "comment")


@dataclass
class ReviewResult:
    findings: list[ReviewFinding]
    summary: str
    verdict: str
    comments: list[ReviewComment]


class ReviewAgent:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        rules_file: Optional[str] = None,
    ):
        self.client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")
        self.rules_file = rules_file or os.getenv("REVIEW_RULES_FILE", "review_rules.md")

    def review_pr(self, pr: PullRequest, files: list[PRFile],
                  file_contents: dict[str, str]) -> ReviewResult:
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_prompt(pr, files, file_contents)

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4096,
        )
        result = response.choices[0].message.content or "{}"

        findings: list[ReviewFinding] = []
        summary = "Review complete."
        verdict = "COMMENT"

        parsed = self._parse_response(result)
        summary = parsed.get("summary", summary)
        verdict = parsed.get("verdict", verdict)
        for item in parsed.get("findings", []):
            finding = ReviewFinding(
                tool=item.get("tool", "report_style"),
                filename=item.get("filename", ""),
                line=item.get("line", 1),
                description=item.get("description", ""),
                suggestion=item.get("suggestion", ""),
                extra={k: v for k, v in item.items()
                       if k not in ("tool", "filename", "line", "description", "suggestion")},
            )
            findings.append(finding)

        parsed_diffs = {
            pr_file.filename: parse_diff(pr_file.filename, pr_file.patch or "")
            for pr_file in files
        }
        comments = self._findings_to_comments(findings, parsed_diffs)
        return ReviewResult(findings=findings, summary=summary,
                            verdict=verdict, comments=comments)

    def _build_system_prompt(self) -> str:
        prompt = (
            "You are a senior code reviewer. Return only valid JSON with this shape: "
            '{"summary": "...", "verdict": "APPROVE|REQUEST_CHANGES|COMMENT", '
            '"findings": [{"tool": "report_bug|report_security|report_performance|report_style", '
            '"filename": "...", "line": 1, "description": "...", "suggestion": "..."}]}. '
            "Use REQUEST_CHANGES for definite bugs or security issues, COMMENT for non-blocking issues, "
            "and APPROVE only when there are no findings."
        )
        personal_rules = self._load_personal_rules()
        if personal_rules:
            prompt += (
                "\n\nFollow these personal review rules for this repository:\n"
                f"{personal_rules}"
            )
        return prompt

    def _load_personal_rules(self) -> str:
        if not self.rules_file:
            return ""

        path = Path(self.rules_file)
        if not path.is_absolute():
            path = Path.cwd() / path

        try:
            rules = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            logger.warning("Could not read review rules file %s: %s", path, exc)
            return ""

        return rules

    def _build_prompt(self, pr: PullRequest, files: list[PRFile],
                      file_contents: dict[str, str]) -> str:
        parts = [
            f"# PR Review: {pr.title}",
            f"**Repo:** {pr.repo}  **PR #{pr.number}**",
            f"**Branch:** `{pr.head_branch}` → `{pr.base_branch}`",
            f"\n**Description:**\n{pr.body or '(none)'}",
            "\n---\n## Changed files\n",
        ]

        for f in files:
            parts.append(f"### `{f.filename}` (+{f.additions} -{f.deletions})")
            if f.filename in file_contents:
                parts.append(f"**Full file content:**\n```\n{file_contents[f.filename][:3000]}\n```")
            if f.patch:
                parts.append(f"**Diff:**\n```diff\n{f.patch[:2000]}\n```")

        parts.append(
            "\n---\n"
            "Review this PR thoroughly. Report bugs, security issues, "
            "performance problems, and style issues. Be specific: always include the exact "
            "filename and line number. Only flag real issues — do not report style issues "
            "unless they significantly hurt readability. Return only the JSON object."
        )
        return "\n".join(parts)

    def _parse_response(self, result: str) -> dict:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            start = result.find("{")
            end = result.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(result[start:end + 1])
                except json.JSONDecodeError:
                    pass
        logger.warning("Could not parse model response as JSON")
        return {}

    def _findings_to_comments(self, findings: list[ReviewFinding],
                              parsed_diffs: dict[str, ParsedDiff] | None = None) -> list[ReviewComment]:
        comments = []
        for f in findings:
            body_parts = [f.description]
            if f.suggestion:
                body_parts.append(f"\n**Suggestion:** {f.suggestion}")
            if f.extra.get("vulnerability_type"):
                body_parts.insert(0, f"**Type:** {f.extra['vulnerability_type']}")
            if f.extra.get("impact"):
                body_parts.append(f"**Impact:** {f.extra['impact']}")

            original_line = max(1, f.line)
            line = original_line
            if parsed_diffs and f.filename in parsed_diffs:
                valid_lines = {
                    diff_line.line_number
                    for diff_line in parsed_diffs[f.filename].added_lines
                }
                if not valid_lines:
                    body_parts.insert(0, f"`{f.filename}:{original_line}`")
                    comments.append(ReviewComment(
                        path=None,
                        line=None,
                        body="\n".join(body_parts),
                        severity=f.severity,
                    ))
                    continue
                line = (
                    original_line
                    if original_line in valid_lines
                    else min(valid_lines, key=lambda valid_line: abs(valid_line - original_line))
                )
            elif parsed_diffs:
                body_parts.insert(0, f"`{f.filename}:{original_line}`")
                comments.append(ReviewComment(
                    path=None,
                    line=None,
                    body="\n".join(body_parts),
                    severity=f.severity,
                ))
                continue

            comments.append(ReviewComment(
                path=f.filename,
                line=line,
                body="\n".join(body_parts),
                severity=f.severity,
            ))
        return comments
