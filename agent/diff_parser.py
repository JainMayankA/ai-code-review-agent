"""
Unified diff parser.

Converts raw GitHub patch strings into structured hunks with accurate
line numbers. This is critical for posting inline review comments —
GitHub's API requires the exact line number in the *new* file.
"""

from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class DiffLine:
    line_number: int     # line number in the new file
    content: str         # line content (without +/- prefix)
    change_type: str     # added | removed | context


@dataclass
class DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine]


@dataclass
class ParsedDiff:
    filename: str
    hunks: list[DiffHunk]

    @property
    def added_lines(self) -> list[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.lines if line.change_type == "added"]

    @property
    def all_lines(self) -> list[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.lines]

    def get_context(self, line_number: int, window: int = 5) -> str:
        """Return surrounding context lines for a given line number."""
        all_lines = self.all_lines
        target_idx = next(
            (i for i, line in enumerate(all_lines) if line.line_number == line_number), None
        )
        if target_idx is None:
            return ""
        start = max(0, target_idx - window)
        end   = min(len(all_lines), target_idx + window + 1)
        return "\n".join(
            f"{'>' if line.line_number == line_number else ' '} {line.line_number:4d} | {line.content}"
            for line in all_lines[start:end]
        )


HUNK_HEADER_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(filename: str, patch: str) -> ParsedDiff:
    """Parse a GitHub unified diff patch into structured hunks."""
    hunks: list[DiffHunk] = []
    current_hunk: DiffHunk | None = None
    new_line_num = 0

    for raw_line in patch.splitlines():
        header_match = HUNK_HEADER_RE.match(raw_line)
        if header_match:
            old_start  = int(header_match.group(1))
            old_count  = int(header_match.group(2) or 1)
            new_start  = int(header_match.group(3))
            new_count  = int(header_match.group(4) or 1)
            current_hunk = DiffHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=[],
            )
            hunks.append(current_hunk)
            new_line_num = new_start
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_hunk.lines.append(DiffLine(
                line_number=new_line_num,
                content=raw_line[1:],
                change_type="added",
            ))
            new_line_num += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            current_hunk.lines.append(DiffLine(
                line_number=new_line_num,
                content=raw_line[1:],
                change_type="removed",
            ))
            # removed lines don't advance new file line counter
        else:
            # context line
            content = raw_line[1:] if raw_line.startswith(" ") else raw_line
            current_hunk.lines.append(DiffLine(
                line_number=new_line_num,
                content=content,
                change_type="context",
            ))
            new_line_num += 1

    return ParsedDiff(filename=filename, hunks=hunks)
