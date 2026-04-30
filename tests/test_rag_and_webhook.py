from agent.rag_context import RepoIndex, CodeChunk
from github.webhook import verify_signature, parse_pr_event
import hashlib
import hmac


class TestRepoIndex:
    def setup_method(self):
        self.index = RepoIndex()

    def _add_sample_files(self):
        self.index.add_file("auth.py", "\n".join([
            "def authenticate(token):",
            "    user = db.query(User).filter_by(token=token).first()",
            "    if not user:",
            "        raise AuthError('invalid token')",
            "    return user",
        ] * 15))
        self.index.add_file("models.py", "\n".join([
            "class User(Base):",
            "    id = Column(Integer, primary_key=True)",
            "    token = Column(String, unique=True)",
            "    email = Column(String)",
        ] * 15))
        self.index.add_file("routes.py", "\n".join([
            "from flask import request, jsonify",
            "def create_user():",
            "    data = request.get_json()",
            "    user = User(**data)",
            "    db.session.add(user)",
        ] * 15))

    def test_add_file_creates_chunks(self):
        self._add_sample_files()
        assert self.index.chunk_count > 0

    def test_query_returns_relevant_chunks(self):
        self._add_sample_files()
        self.index.build()
        results = self.index.query("authenticate token user", top_k=3)
        assert len(results) <= 3
        assert all(isinstance(r, CodeChunk) for r in results)

    def test_query_excludes_specified_files(self):
        self._add_sample_files()
        self.index.build()
        results = self.index.query("user token auth", top_k=5,
                                   exclude_files={"auth.py"})
        assert all(r.filename != "auth.py" for r in results)

    def test_chunk_header_format(self):
        self.index.add_file("utils.py", "def helper():\n    pass\n" * 5)
        self.index.build()
        results = self.index.query("helper", top_k=1)
        if results:
            assert "utils.py" in results[0].header

    def test_empty_index_query_returns_empty(self):
        self.index.build()
        results = self.index.query("anything")
        assert results == []

    def test_chunk_size_respected(self):
        long_content = "\n".join(f"line_{i} = {i}" for i in range(200))
        self.index.add_file("big.py", long_content)
        # 200 lines / 50 per chunk = 4 chunks
        assert self.index.chunk_count == 4


class TestWebhookHandler:
    SECRET = "test-secret-abc123"

    def _make_signature(self, payload: bytes) -> str:
        return "sha256=" + hmac.new(
            self.SECRET.encode(), payload, hashlib.sha256
        ).hexdigest()

    def test_valid_signature_passes(self):
        payload = b'{"action": "opened"}'
        sig = self._make_signature(payload)
        assert verify_signature(payload, sig, self.SECRET) is True

    def test_tampered_payload_fails(self):
        payload = b'{"action": "opened"}'
        sig = self._make_signature(payload)
        assert verify_signature(b'{"action": "closed"}', sig, self.SECRET) is False

    def test_missing_signature_fails(self):
        assert verify_signature(b"payload", "", self.SECRET) is False

    def test_wrong_prefix_fails(self):
        assert verify_signature(b"payload", "sha1=abc", self.SECRET) is False

    def test_parse_pr_opened_event(self):
        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "head": {"sha": "abc123", "ref": "feature-x"},
                "base": {"sha": "def456", "ref": "main"},
            },
            "repository": {
                "name": "my-repo",
                "owner": {"login": "myorg"},
            },
            "sender": {"login": "mayank"},
        }
        event = parse_pr_event(payload)
        assert event is not None
        assert event.pr_number == 42
        assert event.owner == "myorg"
        assert event.repo == "my-repo"
        assert event.action == "opened"

    def test_non_pr_action_returns_none(self):
        payload = {"action": "labeled", "pull_request": {}, "repository": {}}
        assert parse_pr_event(payload) is None

    def test_synchronize_action_parsed(self):
        payload = {
            "action": "synchronize",
            "pull_request": {
                "number": 7,
                "head": {"sha": "xyz", "ref": "fix"},
                "base": {"sha": "abc", "ref": "main"},
            },
            "repository": {"name": "repo", "owner": {"login": "org"}},
            "sender": {"login": "dev"},
        }
        event = parse_pr_event(payload)
        assert event is not None
        assert event.action == "synchronize"
