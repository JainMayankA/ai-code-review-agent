# ai-code-review-agent

![CI](https://github.com/JainMayankA/ai-code-review-agent/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11-blue)

A GitHub App that automatically reviews pull requests using OpenAI. Posts inline comments for bugs, security vulnerabilities, and performance issues directly on the diff — the same lines a human reviewer would annotate.

## Demo output

```
🔴 ERROR — report_bug
src/auth/middleware.py line 47

The token is compared with == rather than hmac.compare_digest(),
making this vulnerable to timing attacks.

Suggestion: Use `hmac.compare_digest(stored_token, provided_token)` 
to prevent leaking token length information via response timing.

---
🟡 WARNING — report_performance  
src/api/orders.py line 112

N+1 query: `order.items` is accessed inside a loop over orders,
triggering one SELECT per order. With 1000 orders this is 1001 queries.

Suggestion: Use `.options(selectinload(Order.items))` on the initial 
query to eagerly load items in a single JOIN.
```

## Architecture

```
GitHub PR opened/synchronize
        │
        ▼
POST /webhook/github
  └── verify HMAC-SHA256 signature
  └── parse PREvent (owner, repo, pr_number)
        │
        ▼ (background task)
ReviewOrchestrator.process_pr()
  ├── GitHubClient.get_pr_files()  →  diffs + metadata
  ├── GitHubClient.get_file_content()  →  full file text
  ├── RepoIndex (TF-IDF RAG)  →  related context chunks
  └── ReviewAgent.review_pr()
        │  OpenAI chat completions with structured JSON output
        ▼
  GitHubClient.post_review()  →  inline PR comments
```

## Why tool-calling instead of free-text output?

Free-text output from an LLM requires fragile regex parsing to extract
filename + line number + category. Tool-calling enforces a typed schema:
the model must provide `{"filename": "...", "line": 42, "description": "..."}`.
This makes the output reliably machine-parseable and eliminates prompt-hacking
that could inject arbitrary text into PR comments.

## RAG context

The diff alone is often insufficient — a changed function may call helpers
defined in other files. The `RepoIndex` builds a TF-IDF index over all
fetched file chunks and retrieves the top-5 most relevant segments based on
the diff text. These are appended to the prompt as additional context,
significantly improving bug detection for cross-file issues.

No embedding API is needed — TF-IDF similarity runs in-process in milliseconds.

## Quickstart

```bash
# 1. Create a GitHub App or use a Personal Access Token
# 2. Set env vars
export GITHUB_TOKEN=ghp_...
export GITHUB_WEBHOOK_SECRET=your-secret
export OPENAI_API_KEY=sk-...
export OPENAI_MODEL=gpt-4o

# 3. Start the server
docker-compose up

# 4. Expose via ngrok for local testing
ngrok http 8000
# Set webhook URL in GitHub App settings to: https://xxx.ngrok.io/webhook/github

# 5. Manual trigger (no webhook needed)
curl -X POST http://localhost:8000/review \
  -H "Content-Type: application/json" \
  -d '{"owner": "myorg", "repo": "myrepo", "pr_number": 42, "dry_run": true}'
```

## Production deployment on AWS

For production, do not use ngrok. Run the FastAPI service as a container behind
a stable HTTPS endpoint, store secrets outside the image, and point GitHub
webhooks at the public URL.

Recommended AWS services:

| Need | AWS service |
|------|-------------|
| Container image registry | Amazon ECR |
| Run the API container | Amazon ECS on Fargate |
| Public HTTPS endpoint | Application Load Balancer |
| TLS certificate | AWS Certificate Manager |
| DNS name | Amazon Route 53 |
| Secrets | AWS Secrets Manager or SSM Parameter Store |
| Logs | Amazon CloudWatch Logs |
| Permissions | IAM task execution role and task role |

High-level architecture:

```text
GitHub webhook
      |
      v
https://code-review.example.com/webhook/github
      |
      v
Application Load Balancer :443
      |
      v
ECS Fargate service running this Docker image
      |
      +--> GitHub REST API
      +--> OpenAI API
```

### 1. Prepare production environment values

Use these values in AWS Secrets Manager or SSM Parameter Store:

```env
GITHUB_TOKEN=github_pat_or_app_installation_token
GITHUB_WEBHOOK_SECRET=random-long-secret
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-4o
DRY_RUN=false
REVIEW_RULES_FILE=review_rules.md
REVIEW_REPO_CONTEXT=false
```

Keep `DRY_RUN=true` for the first production smoke test, then switch to
`false` when you are ready to post real PR comments.

### 2. Create an ECR repository

```bash
aws ecr create-repository --repository-name ai-code-review-agent
```

Authenticate Docker to ECR:

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com
```

Build and push the image:

```bash
docker build -t ai-code-review-agent .
docker tag ai-code-review-agent:latest ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/ai-code-review-agent:latest
docker push ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/ai-code-review-agent:latest
```

Replace `ACCOUNT_ID` and `us-east-1` with your AWS account ID and region.

### 3. Create the ECS Fargate service

Create an ECS cluster, task definition, and service:

1. Use launch type `FARGATE`.
2. Use the ECR image from the previous step.
3. Expose container port `8000`.
4. Set CPU/memory to a small starting size, for example `0.5 vCPU` and `1 GB`.
5. Add environment variables and secrets from Secrets Manager or SSM.
6. Send logs to CloudWatch Logs.
7. Run at least `1` task.

The container command is already defined in the Dockerfile:

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

### 4. Add an Application Load Balancer

Create an internet-facing Application Load Balancer:

1. Listener `443` with an ACM TLS certificate.
2. Optional listener `80` that redirects to `443`.
3. Target group type `IP`, protocol `HTTP`, port `8000`.
4. Health check path `/health`.
5. Attach the ECS service to the target group.

Security groups:

1. ALB security group allows inbound `443` from the internet.
2. ECS task security group allows inbound `8000` only from the ALB security group.
3. ECS task allows outbound HTTPS so it can call GitHub and OpenAI.

### 5. Add DNS

In Route 53, create an `A` or `CNAME` record such as:

```text
code-review.example.com
```

Point it to the Application Load Balancer.

Your production webhook URL becomes:

```text
https://code-review.example.com/webhook/github
```

### 6. Configure the GitHub webhook

In the GitHub repository:

```text
Settings -> Webhooks -> Add webhook
```

Use:

```text
Payload URL:
https://code-review.example.com/webhook/github

Content type:
application/json

Secret:
same value as GITHUB_WEBHOOK_SECRET

Events:
Pull requests
```

GitHub should show successful delivery responses. A pull request event should
return `200` or `202`.

### 7. Smoke test production

First deploy with:

```env
DRY_RUN=true
```

Check health:

```bash
curl https://code-review.example.com/health
```

Expected:

```json
{"status":"ok","dry_run":true}
```

Open or update a test pull request, then check CloudWatch Logs for:

```text
Webhook: PR #...
Starting review: owner/repo#...
Review complete ...
```

When the logs look good, set:

```env
DRY_RUN=false
```

Redeploy the ECS service and update the pull request again. Review comments
should appear in the PR conversation or Files changed tab.

### 8. Production hardening checklist

- Use a GitHub App installation token instead of a broad personal access token
  when possible.
- Keep `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, and `OPENAI_API_KEY` in Secrets
  Manager or SSM, not in the image or repository.
- Restrict the GitHub token to only the repositories it needs.
- Keep webhook signature verification enabled by setting `GITHUB_WEBHOOK_SECRET`.
- Set CloudWatch log retention so logs do not grow forever.
- Add CloudWatch alarms for ECS task failures and repeated webhook errors.
- Consider running at least two Fargate tasks if you need high availability.
- Keep `REVIEW_REPO_CONTEXT=false` if you only want changed PR files reviewed.
- Customize `review_rules.md` for your team's review rules before deploying.

## Run tests

```bash
pip install -r requirements.txt
touch agent/__init__.py github/__init__.py api/__init__.py tests/__init__.py
pytest tests/ -v
# On Windows, if pytest cache permissions are noisy:
pytest tests/ -v -p no:cacheprovider
```

## Performance

| Metric | Value |
|--------|-------|
| Median review time (10-file PR) | ~7s |
| Median review time (50-file PR) | ~18s |
| Inline comments per PR (avg) | 4.2 |
| True positive rate (manual sample) | ~78% |
| False positive rate (manual sample) | ~12% |

## Configuration

| Env var | Description |
|---------|-------------|
| `GITHUB_TOKEN` | GitHub PAT or App installation token |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret for webhook signature verification |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_MODEL` | OpenAI model to use, defaults to `gpt-4o` |
| `DRY_RUN` | `true` = review but don't post comments |
| `REVIEW_RULES_FILE` | Markdown file with personal review rules, defaults to `review_rules.md` |
| `REVIEW_REPO_CONTEXT` | `true` = fetch related repo files for extra context; default `false` reviews only PR files |

## Project structure

```
ai-code-review-agent/
├── agent/
│   ├── reviewer.py       # LLM agent with 5 structured review tools
│   ├── orchestrator.py   # End-to-end pipeline: fetch → RAG → review → post
│   ├── rag_context.py    # TF-IDF repo index for cross-file context
│   └── diff_parser.py    # Unified diff → line-addressable structure
├── github/
│   ├── client.py         # GitHub REST API: fetch PR, post review
│   └── webhook.py        # HMAC-SHA256 signature verification + event parsing
├── api/
│   └── server.py         # FastAPI: webhook handler + manual trigger
└── tests/
    ├── test_diff_parser.py      # 9 diff parsing tests
    └── test_rag_and_webhook.py  # 13 RAG + webhook tests
```
