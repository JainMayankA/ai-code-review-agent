# ai-code-review-agent

![CI](https://github.com/JainMayankA/ai-code-review-agent/actions/workflows/ci.yml/badge.svg)

A small GitHub webhook service that reviews pull requests with OpenAI and posts
inline comments for bugs, security issues, and performance problems. Aimed at
small to medium PRs.

## How it works

1. GitHub sends a `pull_request` webhook (opened / synchronize / reopened).
2. The signature is verified (HMAC-SHA256) and the event is parsed.
3. In a background task, the orchestrator fetches the PR diff and the changed
   files' contents from the GitHub REST API.
4. The review agent sends them to OpenAI using tool calls, so findings come back
   as typed `{filename, line, description, suggestion}` objects instead of free
   text that would need regex parsing.
5. Findings are mapped to diff line numbers and posted as a PR review with inline
   comments. A finding on a line that isn't in the diff falls back to a
   top-level comment.

With `REVIEW_REPO_CONTEXT=true` it also builds a small in-process TF-IDF index
over the fetched files and adds the most relevant chunks to the prompt, which
helps with cross-file issues. No embedding API is used.

## Run locally

```bash
pip install -r requirements.txt

export GITHUB_TOKEN=ghp_...
export GITHUB_WEBHOOK_SECRET=your-secret
export OPENAI_API_KEY=sk-...

uvicorn api.server:app --host 0.0.0.0 --port 8000
```

Or with Docker:

```bash
docker-compose up
```

To test against real GitHub deliveries, expose the port with ngrok and set the
webhook URL to `https://<ngrok-host>/webhook/github`.

You can also trigger a review without a webhook:

```bash
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"owner": "myorg", "repo": "myrepo", "pr_number": 42, "dry_run": true}'
```

## Configuration

| Env var | Description |
|---------|-------------|
| `GITHUB_TOKEN` | GitHub PAT or App installation token |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret for webhook signature verification |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_MODEL` | Model to use, defaults to `gpt-4o` |
| `DRY_RUN` | `true` = run the review but don't post comments |
| `REVIEW_RULES_FILE` | Markdown file with extra review rules, defaults to `review_rules.md` |
| `REVIEW_REPO_CONTEXT` | `true` = fetch related repo files for extra context; default `false` reviews only the PR's changed files |

Copy `.env.example` to `.env` to fill these in for `docker-compose`.

## Tests

```bash
pip install -r requirements.txt
pytest tests/
```

Tested locally with sample patches and webhook payloads. The suite covers diff
parsing, the review agent (tool-call parsing, finding-to-comment mapping, and the
review pipeline with a mocked OpenAI client), review-rules loading, the TF-IDF
index, webhook signature verification, and the file filters.

## Project layout

```
agent/
  reviewer.py       # OpenAI review agent and the review tools
  orchestrator.py   # fetch -> (optional RAG) -> review -> post
  rag_context.py    # TF-IDF index for cross-file context
  diff_parser.py    # unified diff -> new-file line numbers
github/
  client.py         # GitHub REST: fetch PR, post review
  webhook.py        # signature verification + event parsing
api/
  server.py         # FastAPI app: webhook handler + manual trigger
tests/
  test_diff_parser.py
  test_reviewer.py
  test_reviewer_rules.py
  test_rag_and_webhook.py
  test_orchestrator_filters.py
```

## Deployment notes

It's a stateless container that needs a stable HTTPS endpoint for the webhook, so
anything that runs a container works (ECS/Fargate, Cloud Run, a VM behind a
reverse proxy, etc.). Keep the tokens in your platform's secret store instead of
the image, point the GitHub webhook at `/webhook/github`, and use `/health` for
health checks. Start with `DRY_RUN=true` to confirm deliveries before posting
real comments.
```