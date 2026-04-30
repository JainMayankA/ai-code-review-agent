from agent.orchestrator import ReviewOrchestrator


class TestReviewableFileFilters:
    def setup_method(self):
        self.orchestrator = ReviewOrchestrator(github=None, agent=None)

    def test_skips_dependency_and_build_directories(self):
        skipped_files = [
            "node_modules/package/index.js",
            "frontend/dist/app.js",
            "frontend/build/app.js",
            "coverage/report.js",
            ".next/server/app.js",
            "vendor/library/file.php",
            "node_modules\\package\\index.js",
        ]

        for filename in skipped_files:
            assert self.orchestrator._should_review(filename) is False

    def test_still_reviews_normal_source_files(self):
        reviewed_files = [
            "src/app.js",
            "frontend/src/page.tsx",
            "api/server.py",
        ]

        for filename in reviewed_files:
            assert self.orchestrator._should_review(filename) is True
