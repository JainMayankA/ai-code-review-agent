"""
Tests for ReviewAgent core logic.

Covers _parse_tool_calls, _findings_to_comments, _build_prompt, and the
full review_pr pipeline with a mocked OpenAI client.
"""

from __future__ import annotations
import json
from unittest.mock import MagicMock

from agent.diff_parser import parse_diff
from agent.reviewer import ReviewAgent, ReviewFinding, REVIEW_TOOLS, SEVERITY_MAP
from github.client import PRFile, PullRequest


# SAMPLE_PATCH adds lines at new-file line numbers 41 and 42.
SAMPLE_PATCH = """\
@@ -40,6 +40,8 @@ def check_token(token):
     stored = get_stored_token()
-    if token == stored:
+    if not hmac.compare_digest(token, stored):
+        raise AuthError()
     return True
"""


def _tool_call(name: str, args: dict) -> MagicMock:
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


def _make_agent() -> ReviewAgent:
    return ReviewAgent(api_key="test-key", rules_file=None)


def _make_pr() -> PullRequest:
    return PullRequest(
        number=42,
        title="Fix auth timing attack",
        body="Replaces == with hmac.compare_digest",
        head_sha="abc123",
        base_sha="def456",
        head_branch="fix/timing",
        base_branch="main",
        repo="org/myrepo",
    )


def _make_file(filename: str = "src/auth.py", patch: str = SAMPLE_PATCH) -> PRFile:
    return PRFile(filename=filename, status="modified", patch=patch, additions=2, deletions=1, raw_url="")


def _make_finding(**kwargs) -> ReviewFinding:
    defaults = dict(
        tool="report_bug",
        filename="src/auth.py",
        line=41,
        description="Something is wrong",
        suggestion="Fix it",
        extra={},
    )
    defaults.update(kwargs)
    return ReviewFinding(**defaults)


def _mock_openai_response(tool_calls: list) -> MagicMock:
    resp = MagicMock()
    resp.choices[0].message.tool_calls = tool_calls
    resp.choices[0].message.content = None
    return resp


class TestParseToolCalls:
    def setup_method(self):
        self.agent = _make_agent()

    def test_single_bug_with_summary(self):
        calls = [
            _tool_call("report_bug", {"filename": "src/auth.py", "line": 42, "description": "Timing attack", "suggestion": "Use hmac.compare_digest"}),
            _tool_call("post_summary", {"summary": "Found a bug.", "verdict": "REQUEST_CHANGES"}),
        ]
        result = self.agent._parse_tool_calls(calls)

        assert result["verdict"] == "REQUEST_CHANGES"
        assert result["summary"] == "Found a bug."
        assert len(result["findings"]) == 1
        assert result["findings"][0]["tool"] == "report_bug"
        assert result["findings"][0]["filename"] == "src/auth.py"
        assert result["findings"][0]["line"] == 42

    def test_all_four_finding_types_are_collected(self):
        calls = [
            _tool_call("report_bug",         {"filename": "a.py", "line": 1, "description": "d", "suggestion": "s"}),
            _tool_call("report_security",    {"filename": "b.py", "line": 2, "vulnerability_type": "sqli", "description": "d", "suggestion": "s"}),
            _tool_call("report_performance", {"filename": "c.py", "line": 3, "description": "d", "suggestion": "s"}),
            _tool_call("report_style",       {"filename": "d.py", "line": 4, "description": "d"}),
            _tool_call("post_summary",       {"summary": "Four issues.", "verdict": "REQUEST_CHANGES"}),
        ]
        result = self.agent._parse_tool_calls(calls)

        assert len(result["findings"]) == 4
        tools = [f["tool"] for f in result["findings"]]
        assert set(tools) == {"report_bug", "report_security", "report_performance", "report_style"}

    def test_empty_tool_calls_returns_empty_dict(self):
        assert self.agent._parse_tool_calls([]) == {}

    def test_malformed_json_args_are_skipped_without_crash(self):
        bad = MagicMock()
        bad.function.name = "report_bug"
        bad.function.arguments = "NOT_VALID_JSON{"
        good = _tool_call("post_summary", {"summary": "ok", "verdict": "COMMENT"})

        result = self.agent._parse_tool_calls([bad, good])

        assert result["findings"] == []
        assert result["verdict"] == "COMMENT"

    def test_unknown_tool_name_excluded_from_findings(self):
        calls = [
            _tool_call("report_unicorn", {"filename": "x.py", "line": 1, "description": "d"}),
            _tool_call("post_summary",   {"summary": "ok", "verdict": "COMMENT"}),
        ]
        result = self.agent._parse_tool_calls(calls)
        assert result["findings"] == []

    def test_approve_verdict_when_summary_only(self):
        calls = [_tool_call("post_summary", {"summary": "LGTM!", "verdict": "APPROVE"})]
        result = self.agent._parse_tool_calls(calls)
        assert result["verdict"] == "APPROVE"
        assert result["findings"] == []

    def test_security_finding_preserves_vulnerability_type_in_args(self):
        calls = [
            _tool_call("report_security", {
                "filename": "api/views.py", "line": 10,
                "vulnerability_type": "SQL Injection",
                "description": "Raw query", "suggestion": "Use parameterized queries",
            }),
            _tool_call("post_summary", {"summary": "sqli", "verdict": "REQUEST_CHANGES"}),
        ]
        result = self.agent._parse_tool_calls(calls)
        assert result["findings"][0]["vulnerability_type"] == "SQL Injection"


class TestFindingsToComments:
    def setup_method(self):
        self.agent = _make_agent()
        self.diff = parse_diff("src/auth.py", SAMPLE_PATCH)
        assert {dl.line_number for dl in self.diff.added_lines} == {41, 42}

    def test_line_in_diff_produces_inline_comment(self):
        finding = _make_finding(filename="src/auth.py", line=41)
        comments = self.agent._findings_to_comments([finding], {"src/auth.py": self.diff})

        assert comments[0].path == "src/auth.py"
        assert comments[0].line == 41

    def test_line_not_in_diff_snaps_to_nearest_added_line(self):
        finding = _make_finding(filename="src/auth.py", line=200)
        comments = self.agent._findings_to_comments([finding], {"src/auth.py": self.diff})

        assert comments[0].line in {41, 42}
        assert comments[0].path == "src/auth.py"

    def test_line_before_diff_snaps_to_first_added_line(self):
        finding = _make_finding(filename="src/auth.py", line=1)
        comments = self.agent._findings_to_comments([finding], {"src/auth.py": self.diff})

        assert comments[0].line == 41

    def test_file_with_no_added_lines_becomes_top_level_comment(self):
        removal_patch = "@@ -10,3 +10,0 @@\n-line1\n-line2\n-line3\n"
        diff = parse_diff("src/old.py", removal_patch)
        assert diff.added_lines == []

        finding = _make_finding(filename="src/old.py", line=10)
        comments = self.agent._findings_to_comments([finding], {"src/old.py": diff})

        assert comments[0].path is None
        assert comments[0].line is None
        assert "src/old.py:10" in comments[0].body

    def test_file_not_in_parsed_diffs_becomes_top_level_comment(self):
        finding = _make_finding(filename="src/unknown.py", line=5)
        comments = self.agent._findings_to_comments([finding], {})

        assert comments[0].path is None
        assert comments[0].line is None

    def test_no_parsed_diffs_arg_produces_top_level_comment(self):
        finding = _make_finding()
        comments = self.agent._findings_to_comments([finding], None)

        assert comments[0].path is None
        assert comments[0].line is None

    def test_security_finding_includes_vulnerability_type_in_body(self):
        finding = _make_finding(tool="report_security", extra={"vulnerability_type": "Path Traversal"})
        comments = self.agent._findings_to_comments([finding], None)

        assert "Path Traversal" in comments[0].body

    def test_performance_finding_includes_impact_in_body(self):
        finding = _make_finding(tool="report_performance", extra={"impact": "1000× slowdown on large datasets"})
        comments = self.agent._findings_to_comments([finding], None)

        assert "1000×" in comments[0].body

    def test_suggestion_appears_in_comment_body(self):
        finding = _make_finding(suggestion="Use hmac.compare_digest instead")
        comments = self.agent._findings_to_comments([finding], None)

        assert "hmac.compare_digest" in comments[0].body

    def test_all_severity_mappings_are_applied(self):
        for tool, expected_severity in SEVERITY_MAP.items():
            finding = _make_finding(tool=tool)
            comments = self.agent._findings_to_comments([finding], None)
            assert comments[0].severity == expected_severity, f"wrong severity for {tool}"

    def test_multiple_findings_all_converted(self):
        findings = [
            _make_finding(tool="report_bug", line=41),
            _make_finding(tool="report_security", line=42),
        ]
        comments = self.agent._findings_to_comments(findings, {"src/auth.py": self.diff})
        assert len(comments) == 2


class TestBuildPrompt:
    def setup_method(self):
        self.agent = _make_agent()

    def test_prompt_contains_pr_title(self):
        pr = _make_pr()
        prompt = self.agent._build_prompt(pr, [_make_file()], {})
        assert "Fix auth timing attack" in prompt

    def test_prompt_contains_branch_info(self):
        pr = _make_pr()
        prompt = self.agent._build_prompt(pr, [_make_file()], {})
        assert "fix/timing" in prompt
        assert "main" in prompt

    def test_prompt_contains_diff_content(self):
        pr = _make_pr()
        file = _make_file(patch="@@ -1 +1 @@\n+unique_sentinel_string\n")
        prompt = self.agent._build_prompt(pr, [file], {})
        assert "unique_sentinel_string" in prompt

    def test_prompt_includes_full_file_content_when_provided(self):
        pr = _make_pr()
        content = "def check_token():\n    pass\n"
        prompt = self.agent._build_prompt(pr, [_make_file()], {"src/auth.py": content})
        assert "check_token" in prompt

    def test_long_file_content_is_truncated_at_3000_chars(self):
        pr = _make_pr()
        long_content = "x" * 5000
        prompt = self.agent._build_prompt(pr, [_make_file()], {"src/auth.py": long_content})
        assert "x" * 3001 not in prompt
        assert "x" * 3000 in prompt

    def test_pr_body_included_in_prompt(self):
        pr = _make_pr()
        prompt = self.agent._build_prompt(pr, [_make_file()], {})
        assert "hmac.compare_digest" in prompt  # from pr.body


class TestReviewPr:
    def setup_method(self):
        self.agent = _make_agent()
        self.agent.client = MagicMock()

    def _set_response(self, tool_calls: list):
        self.agent.client.chat.completions.create.return_value = _mock_openai_response(tool_calls)

    def test_bug_finding_returns_request_changes(self):
        self._set_response([
            _tool_call("report_bug", {"filename": "src/auth.py", "line": 41, "description": "Timing attack", "suggestion": "Use hmac.compare_digest"}),
            _tool_call("post_summary", {"summary": "Critical bug found.", "verdict": "REQUEST_CHANGES"}),
        ])

        result = self.agent.review_pr(_make_pr(), [_make_file()], {})

        assert result.verdict == "REQUEST_CHANGES"
        assert len(result.findings) == 1
        assert result.findings[0].tool == "report_bug"
        assert result.summary == "Critical bug found."

    def test_no_findings_produces_approve_with_no_comments(self):
        self._set_response([
            _tool_call("post_summary", {"summary": "LGTM, no issues.", "verdict": "APPROVE"}),
        ])

        result = self.agent.review_pr(_make_pr(), [_make_file()], {})

        assert result.verdict == "APPROVE"
        assert result.findings == []
        assert result.comments == []

    def test_security_finding_severity_is_error(self):
        self._set_response([
            _tool_call("report_security", {
                "filename": "src/auth.py", "line": 41,
                "vulnerability_type": "Timing Attack",
                "description": "Token compared with ==",
                "suggestion": "Use hmac.compare_digest",
            }),
            _tool_call("post_summary", {"summary": "Security issue.", "verdict": "REQUEST_CHANGES"}),
        ])

        result = self.agent.review_pr(_make_pr(), [_make_file()], {})

        assert result.findings[0].severity == "error"

    def test_security_finding_includes_vuln_type_in_comment(self):
        self._set_response([
            _tool_call("report_security", {
                "filename": "src/auth.py", "line": 41,
                "vulnerability_type": "Timing Attack",
                "description": "Vulnerable comparison", "suggestion": "Fix it",
            }),
            _tool_call("post_summary", {"summary": "Issues.", "verdict": "REQUEST_CHANGES"}),
        ])

        result = self.agent.review_pr(_make_pr(), [_make_file()], {})

        assert any("Timing Attack" in c.body for c in result.comments)

    def test_openai_called_with_tool_choice_required(self):
        self._set_response([
            _tool_call("post_summary", {"summary": "ok", "verdict": "APPROVE"}),
        ])
        self.agent.review_pr(_make_pr(), [_make_file()], {})

        call_kwargs = self.agent.client.chat.completions.create.call_args.kwargs
        assert call_kwargs["tool_choice"] == "required"
        assert len(call_kwargs["tools"]) == len(REVIEW_TOOLS)

    def test_finding_line_snapped_to_valid_diff_line(self):
        self._set_response([
            _tool_call("report_bug", {"filename": "src/auth.py", "line": 999, "description": "Bug", "suggestion": "Fix"}),
            _tool_call("post_summary", {"summary": "Bug.", "verdict": "REQUEST_CHANGES"}),
        ])

        result = self.agent.review_pr(_make_pr(), [_make_file()], {})

        inline = [c for c in result.comments if c.path is not None]
        assert len(inline) == 1
        assert inline[0].line in {41, 42}

    def test_multiple_findings_all_produce_comments(self):
        self._set_response([
            _tool_call("report_bug",         {"filename": "src/auth.py", "line": 41, "description": "d1", "suggestion": "s1"}),
            _tool_call("report_performance", {"filename": "src/auth.py", "line": 42, "description": "d2", "suggestion": "s2"}),
            _tool_call("post_summary",       {"summary": "Two issues.", "verdict": "REQUEST_CHANGES"}),
        ])

        result = self.agent.review_pr(_make_pr(), [_make_file()], {})

        assert len(result.findings) == 2
        assert len(result.comments) == 2
