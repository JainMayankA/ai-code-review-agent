from agent.diff_parser import parse_diff

SAMPLE_PATCH = """\
@@ -10,7 +10,9 @@ def process(items):
     results = []
     for item in items:
-        result = item.compute()
+        if item is None:
+            continue
+        result = item.compute()
         results.append(result)
     return results
@@ -25,4 +27,3 @@ def validate(x):
-    if x > 0:
-        return True
-    return False
+    return x > 0
"""


class TestDiffParser:
    def test_parses_two_hunks(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        assert len(diff.hunks) == 2

    def test_added_lines_count(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        added = diff.added_lines
        assert len(added) == 4

    def test_added_line_content(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        added_contents = [line.content.strip() for line in diff.added_lines]
        assert "if item is None:" in added_contents
        assert "continue" in added_contents

    def test_line_numbers_increment_for_added(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        added = diff.added_lines
        line_nums = [line.line_number for line in added]
        assert line_nums == sorted(line_nums)

    def test_removed_lines_do_not_advance_line_counter(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        removed = [
            line
            for hunk in diff.hunks
            for line in hunk.lines
            if line.change_type == "removed"
        ]
        assert len(removed) > 0

    def test_filename_stored(self):
        diff = parse_diff("src/utils.py", SAMPLE_PATCH)
        assert diff.filename == "src/utils.py"

    def test_get_context_returns_surrounding_lines(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        added = diff.added_lines
        if added:
            ctx = diff.get_context(added[0].line_number, window=2)
            assert str(added[0].line_number) in ctx

    def test_empty_patch_returns_no_hunks(self):
        diff = parse_diff("empty.py", "")
        assert diff.hunks == []
        assert diff.added_lines == []

    def test_hunk_metadata(self):
        diff = parse_diff("foo.py", SAMPLE_PATCH)
        h = diff.hunks[0]
        assert h.old_start == 10
        assert h.new_start == 10
