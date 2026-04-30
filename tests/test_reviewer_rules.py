from pathlib import Path
from types import SimpleNamespace

from agent.diff_parser import parse_diff
from agent.reviewer import ReviewAgent, ReviewFinding


def test_system_prompt_includes_personal_rules():
    rules_file = Path("test_review_rules.md")
    rules_file.write_text("- Flag missing authorization checks.\n", encoding="utf-8")

    try:
        agent = ReviewAgent(api_key="test-key", rules_file=str(rules_file))

        prompt = agent._build_system_prompt()
        assert "Follow these personal review rules" in prompt
        assert "Flag missing authorization checks" in prompt
    finally:
        rules_file.unlink(missing_ok=True)


def test_missing_personal_rules_file_is_ignored():
    missing_file = Path("missing_test_review_rules.md")

    agent = ReviewAgent(api_key="test-key", rules_file=str(missing_file))

    prompt = agent._build_system_prompt()
    assert "Follow these personal review rules" not in prompt


def test_tool_call_arguments_are_parsed_into_findings():
    agent = ReviewAgent(api_key="test-key", rules_file="")
    tool_calls = [
        SimpleNamespace(
            function=SimpleNamespace(
                name="report_bug",
                arguments=(
                    '{"filename": "demo.py", "line": 3, '
                    '"description": "Bug found.", "suggestion": "Fix it."}'
                ),
            )
        ),
        SimpleNamespace(
            function=SimpleNamespace(
                name="post_summary",
                arguments='{"summary": "One issue.", "verdict": "REQUEST_CHANGES"}',
            )
        ),
    ]

    parsed = agent._parse_tool_calls(tool_calls)

    assert parsed["summary"] == "One issue."
    assert parsed["verdict"] == "REQUEST_CHANGES"
    assert parsed["findings"] == [
        {
            "tool": "report_bug",
            "filename": "demo.py",
            "line": 3,
            "description": "Bug found.",
            "suggestion": "Fix it.",
        }
    ]


def test_findings_snap_to_added_diff_lines():
    patch = """\
@@ -1,3 +1,4 @@
 def demo():
+    value = None
     return value
"""
    parsed = {"demo.py": parse_diff("demo.py", patch)}
    finding = ReviewFinding(
        tool="report_bug",
        filename="demo.py",
        line=3,
        description="This can return None unexpectedly.",
    )
    agent = ReviewAgent(api_key="test-key", rules_file="")

    comments = agent._findings_to_comments([finding], parsed)

    assert len(comments) == 1
    assert comments[0].path == "demo.py"
    assert comments[0].line == 2


def test_findings_without_added_lines_become_top_level_comments():
    patch = """\
@@ -1,2 +1,2 @@
 def demo():
     return value
"""
    parsed = {"demo.py": parse_diff("demo.py", patch)}
    finding = ReviewFinding(
        tool="report_bug",
        filename="demo.py",
        line=2,
        description="This can return an undefined value.",
    )
    agent = ReviewAgent(api_key="test-key", rules_file="")

    comments = agent._findings_to_comments([finding], parsed)

    assert len(comments) == 1
    assert comments[0].path is None
    assert comments[0].line is None
    assert "demo.py:2" in comments[0].body
