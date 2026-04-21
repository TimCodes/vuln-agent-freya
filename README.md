# VulnFix POC — Agent + Tools API

A proof-of-concept vulnerability remediation system built as **two independent FastAPI services** with a strict separation of concerns:

- **`tools-api/`** — the only component allowed to perform *mutations* (git, npm, filesystem). No LLM, no agent logic.
- **`agent-api/`** — a LangGraph workflow that decides what to fix and delegates every mutation to the Tools API over HTTP.

The services are intended to run in separate containers and/or networks, with only the Agent API able to call the Tools API. Docker packaging is out of scope for this POC.

## Security boundaries

1. The agent never imports `subprocess`, never touches the filesystem, and has no git/npm code. Every change to a repo is an HTTP call to the Tools API.
2. The Tools API exposes a small, fixed set of endpoints and does **not** accept arbitrary commands. Package names and version specifiers are validated with strict regexes at the model layer.
3. All subprocess calls in the Tools API use `shell=False` with an explicit argv list, so no user-supplied value ever reaches a shell.
4. Workspaces are isolated to one directory per UUID under `TOOLS_API_WORKSPACE_ROOT`. The workspace manager resolves every path and rejects anything outside the root (blocking `..` and symlink escapes).
5. The Tools API has no auth in this POC — it’s assumed to run on a trusted network reachable only by the Agent API. Production should restore a shared secret or switch to mTLS.
6. `npm audit fix --force` is **not** exposed. Force can introduce breaking changes and is not appropriate for an automated agent.
7. GitHub tokens used for pushing branches and opening PRs live only in the Tools API environment. They are embedded into push URLs at runtime and scrubbed from any error message surfaced to the agent.

## Architecture

```text
┌─────────────────────┐                  HTTP                   ┌─────────────────────┐
│    Agent API        │ ─────────────────────────────────────► │    Tools API        │
│  (LangGraph)        │                                         │                     │
│                     │                                         │  git clone / push   │
│  - Planner (LLM)    │                                         │  npm audit          │
│  - Executor         │                                         │  npm audit fix      │
│  - Summarizer (LLM) │                                         │  npm install <pkg>  │
│                     │                                         │  git commit         │
│  NO subprocess      │                                         │  GitHub PR API      │
│  NO filesystem      │                                         │                     │
└─────────────────────┘                                         │  NO LLM             │
         ▲                                                      └─────────────────────┘
         │                                                               │      │
   POST /remediate                                       Isolated workspaces    │
   (from scanner/ticket                                  under workspace_root   │
    system)                                                                     ▼
                                                                         github.com
                                                                        (push + PR)
```

## LangGraph workflow

```text
START
  │
  ▼
create_workspace ──(no workspace)──┐
  │                                │
  ▼                                │
initial_audit                      │
  │                                │
  ▼                                │
plan (LLM)                         │
  │                                │
  ▼                                │
execute_plan                       │
  │                                │
  ▼                                │
final_audit                        │
  │                                │
  ▼                                │
commit                             │
  │                                │
  ▼                                │
open_pr                            │
  │                                │
  ▼                                ▼
summarize (LLM) ◄──────────────────┘
  │
  ▼
 END
```

Workspace creation failures short-circuit to the summarizer so the caller always gets a report. LLM failures in the planner and summarizer fall back to deterministic heuristics, so the pipeline still works without an LLM key. `open_pr` skips cleanly if nothing was committed or if no `TOOLS_API_GITHUB_TOKEN` is configured; the failure is recorded in `errors` and the commit still lives locally in the (ephemeral) workspace.

## Running locally

### Prerequisites

- Python 3.11+
- `git` and `npm` on `PATH` (Tools API host only)
- An LLM API key for real planning/summarization (otherwise heuristic fallbacks are used):
  - **OpenAI** (default): `OPENAI_API_KEY` — set `AGENT_API_LLM_PROVIDER=openai` (or leave unset)
  - **Anthropic**: `ANTHROPIC_API_KEY` — set `AGENT_API_LLM_PROVIDER=anthropic`
  - **GitHub Models**: `GITHUB_TOKEN` (or `AGENT_API_GITHUB_MODELS_TOKEN`) — set `AGENT_API_LLM_PROVIDER=github` and `AGENT_API_LLM_MODEL` to a model from the GitHub Models catalog
- A GitHub token to open pull requests (optional — without it, commits stay local and `open_pr` is skipped).
  - Classic token: needs `repo` scope
  - Fine-grained token: needs `contents: write` + `pull_requests: write` on the target repo
  - Set as `TOOLS_API_GITHUB_TOKEN` on the Tools API host

### Tools API

**Mac/Linux**

```bash
cd tools-api
python -m venv .venv && source .venv/bin/activate
pip install -e .

export TOOLS_API_WORKSPACE_ROOT="/tmp/vulnfix-workspaces"
# Optional: enables the /pull-request endpoint (and the agent's open_pr node).
export TOOLS_API_GITHUB_TOKEN="ghp_..."

uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

**Windows (PowerShell)**

```powershell
cd tools-api
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e .

$env:TOOLS_API_WORKSPACE_ROOT="C:\tmp\vulnfix-workspaces"
# Optional: enables the /pull-request endpoint (and the agent's open_pr node).
$env:TOOLS_API_GITHUB_TOKEN="ghp_..."

uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

Health check:

```bash
curl http://localhost:8001/health
```

### Agent API

**Mac/Linux**

```bash
cd agent-api
python -m venv .venv && source .venv/bin/activate
pip install -e .

export AGENT_API_TOOLS_API_BASE_URL="http://localhost:8001"

# Optional: real LLM calls. Without this the heuristic fallbacks are used.
# OpenAI (default provider):
export OPENAI_API_KEY="sk-proj-..."
# Or switch to Anthropic:
# export AGENT_API_LLM_PROVIDER=anthropic
# export ANTHROPIC_API_KEY="sk-ant-..."
# Or use GitHub Models:
# export AGENT_API_LLM_PROVIDER=github
# export AGENT_API_LLM_MODEL="openai/gpt-5"
# export GITHUB_TOKEN="ghp_..."

uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

**Windows (PowerShell)**

```powershell
cd agent-api
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e .

$env:AGENT_API_TOOLS_API_BASE_URL="http://localhost:8001"

# Optional: real LLM calls. Without this the heuristic fallbacks are used.
# OpenAI (default provider):
$env:OPENAI_API_KEY="sk-proj-..."
# Or switch to Anthropic:
# $env:AGENT_API_LLM_PROVIDER="anthropic"
# $env:ANTHROPIC_API_KEY="sk-ant-..."
# Or use GitHub Models:
# $env:AGENT_API_LLM_PROVIDER="github"
# $env:AGENT_API_LLM_MODEL="openai/gpt-5"
# $env:GITHUB_TOKEN="ghp_..."

uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

## Example request — single repo (`POST /remediate`)

**Mac/Linux**

```bash
curl -X POST http://localhost:8002/remediate \
  -H 'Content-Type: application/json' \
  -d '{
    "repo_url": "https://github.com/TimCodes/vuln-test.git",
    "branch": "main",
    "vulnerabilities": [
      {
        "id": "GHSA-jf85-cpcp-j695",
        "package": "lodash",
        "current_version": "4.17.15",
        "fixed_version": "4.17.21",
        "severity": "high",
        "description": "Prototype pollution in lodash"
      }
    ]
  }'
```

**Windows (PowerShell)**

```powershell
$body = @{
  repo_url = "https://github.com/TimCodes/vuln-test"
  branch = "main"
  vulnerabilities = @(
    @{
      id = "none"
      package = "handlebars"
      current_version = "4.7.8"
      fixed_version = "4.7.9"
      severity = "high"
      description = "Prototype pollution in lodash"
    }
  )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri http://localhost:8002/remediate `
  -ContentType "application/json" `
  -Body $body
```

## Example request — CSV batch (`POST /remediate/batch`)

Upload a CSV listing vulnerabilities across many repos; the agent groups rows by repo and runs one remediation per repo in parallel (bounded by `AGENT_API_BATCH_REMEDIATION_CONCURRENCY`, default `3`).

Expected columns (case-insensitive; extras ignored):

| Column          | Required | Notes |
|-----------------|----------|-------|
| `Name/Package`  | yes      | Package name; any `(Vulnerable versions: ...)` suffix is stripped. |
| `Location`      | yes      | `<owner>/<repo>` slug. `(Vulnerable manifest path: ...)` stripped. |
| `ID`            | no       | CVE id. `N/A` falls back to `Unique ID`. |
| `Severity`      | no       | `Critical` / `High` / `Moderate` / `Low` / `Info`. |
| `Description`   | no       | Truncated to `AGENT_API_CSV_DESCRIPTION_MAX_CHARS` (default 500). |
| `Unique ID`     | no       | Fallback identifier when `ID` is `N/A`. |
| `Fixed Version` | no       | If present and not empty/`N/A`/`none`, bypasses the agent's fix inference. |

Slug → git URL uses `AGENT_API_GITHUB_HOST` (default `https://github.com/`) — `acme-corp/foo` becomes `https://github.com/acme-corp/foo.git`. All batch runs target branch `main`.

**Mac/Linux**

```bash
curl -X POST http://localhost:8002/remediate/batch \
  -F "file=@/path/to/vulnerabilities.csv"
```

**Windows (PowerShell)**

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8002/remediate/batch `
  -Form @{ file = Get-Item "C:\Users\you\Downloads\vulnerabilities.csv" }
```

Sample batch response (abridged):

```json
{
  "total_rows": 13,
  "rows_skipped": 0,
  "repos_processed": 4,
  "repos_succeeded": 3,
  "repos_failed": 1,
  "results": [
    {
      "repo_slug": "acme-corp/test-tests-missing-claims-report",
      "repo_url": "https://github.com/acme-corp/test-tests-missing-claims-report.git",
      "vulnerabilities_reported": 5,
      "result": { "...": "same shape as /remediate response" },
      "error": null
    },
    {
      "repo_slug": "acme-corp/test-tests-claim-writer-api",
      "repo_url": "https://github.com/acme-corp/test-tests-claim-writer-api.git",
      "vulnerabilities_reported": 1,
      "result": null,
      "error": "remediation failed: ..."
    }
  ],
  "parse_warnings": []
}
```

Failures are per-repo: one blown-up clone or git push does not abort the rest of the batch. `parse_warnings` lists any rows dropped during CSV parsing (missing slug/package, validation errors, etc.).

### Concurrency and batching

The agent processes repos with a semaphore bounded by `AGENT_API_BATCH_REMEDIATION_CONCURRENCY` (default `3`). If the CSV contains more repos than the cap, all repos are still processed — extras queue and start as slots free.

```text
Time →   (example: 5 repos, cap = 3)

Repo 1   [======== running ========]
Repo 2   [======== running ========]
Repo 3   [============ running ============]
Repo 4                             [=== queued → starts when slot frees ===]
Repo 5                             [=== queued → starts when slot frees ===]
```

The HTTP response returns only once **every** repo has finished, so a large CSV with slow clones will block for a while. The cap is a throttle to avoid overwhelming the Tools API with simultaneous workspace creation.

The only hard ceiling is `TOOLS_API_MAX_WORKSPACES` on the Tools API side: each in-flight repo holds one workspace slot, so `AGENT_API_BATCH_REMEDIATION_CONCURRENCY` should be set no higher than that value.

Sample single-repo response (abridged):

```json
{
  "repo_url": "...",
  "branch": "main",
  "workspace_id": "a1b2c3...",
  "applied_fixes": [],
  "pr_url": "https://github.com/owner/repo/pull/42",
  "summary": "Fixed 3 vulnerabilities...",
  "errors": []
}
```

## Project layout

```text
vulnfix-poc/
├── README.md
├── tools-api/
│   ├── pyproject.toml
│   └── app/
│       ├── main.py                     # FastAPI app + exception handler
│       ├── api/                        # HTTP routing layer
│       │   ├── workspaces_router.py    # create/get/delete workspace
│       │   ├── npm_router.py           # audit, audit-fix
│       │   ├── packages_router.py      # package.json read, package update, commit
│       │   └── pull_requests_router.py # open pull request
│       ├── workflows/                  # multi-step use-case orchestration
│       │   ├── workspace_provisioning_workflow.py  # reserve + clone + read HEAD
│       │   ├── audit_fix_workflow.py               # audit-fix then re-audit
│       │   ├── package_update_workflow.py          # install + verify installed version
│       │   ├── commit_changes_workflow.py          # changed-files guard + commit
│       │   └── pull_request_workflow.py            # push + GitHub PR create
│       ├── services/                   # domain rules over low-level clients
│       │   ├── workspace_service.py    # UUID-keyed workspaces + path safety
│       │   ├── git_service.py          # clone, commit, push, status
│       │   ├── npm_service.py          # audit, audit-fix, install, ls
│       │   └── github_service.py       # URL parsing, push URL, create PR
│       ├── core/
│       │   ├── config.py               # env-driven settings
│       │   ├── logger.py               # centralized logging setup
│       │   └── clients/                # low-level transport only
│       │       ├── git_client.py       # subprocess runner for `git ...`
│       │       ├── npm_client.py       # subprocess runner for `npm ...`
│       │       └── github_api_client.py # httpx wrapper around api.github.com
│       └── models/                     # request/response DTOs
│           ├── workspace_models.py
│           ├── npm_models.py
│           ├── package_models.py
│           └── pull_request_models.py
└── agent-api/
    ├── pyproject.toml
    └── app/
        ├── main.py                          # FastAPI app + exception handler
        ├── api/                             # HTTP routing layer
        │   ├── remediate_router.py          # POST /remediate
        │   └── batch_router.py              # POST /remediate/batch (CSV upload)
        ├── services/                        # orchestration between API, graph, clients
        │   ├── remediation_service.py       # single-repo: runs graph + cleanup + response
        │   └── batch_remediation_service.py # CSV → fan-out per repo (Semaphore-bounded)
        ├── agents/                          # LLM-backed reasoning
        │   ├── planner_agent.py             # reported vulns + audit → FixAction[]
        │   └── summarizer_agent.py          # final state → human-readable report
        ├── graph/                           # LangGraph workflow
        │   ├── state.py                     # AgentState TypedDict
        │   ├── nodes.py                     # thin delegators → agents + Tools API
        │   └── builder.py                   # wires the graph
        ├── clients/                         # external HTTP clients
        │   └── tools_api_client.py          # the ONLY mutation path — httpx client
        ├── core/                            # config, logging, LLM factory
        │   ├── config.py                    # env-driven settings
        │   ├── logging_config.py            # centralized logging setup
        │   └── llm_factory.py               # Anthropic / OpenAI / GitHub Models chat model builder
        ├── schemas/                         # request/response + domain DTOs
        │   └── models.py                    # VulnerabilityReport, FixAction, RemediationResult, BatchRemediationResult
        └── utils/                           # pure helpers
            ├── audit_summary.py             # npm audit → compact summary dict
            ├── csv_parser.py                # batch upload → grouped VulnerabilityReports
            └── presentation.py              # commit message + PR body builders
```

## What the POC intentionally does not do

A production version would need these, but they’re out of scope here:

- **Docker**: containerization and network isolation to enforce the agent→tools boundary at the infra layer.
- **Push / PR creation**: push and PR creation should live in a separate endpoint with credential handling and a branching strategy.
- **Persistence**: workspaces and graph state are in-memory; a real system would use a durable LangGraph checkpointer and a workspace database.
- **Human approval gate**: every successful plan is applied; production needs a hold-for-review step before commit/push.
- **Authentication**: production should use mTLS between services and real auth on the public Agent API endpoint.
- **Concurrency**: there is no cross-workspace rate limiting or queueing.
- **Observability**: stdout logging only; production needs structured logs, tracing, and metrics.

## Extending the POC

A few natural next steps:

- Add a `POST /workspaces/{id}/git/push` endpoint and a `create_pr` node.
- Add a human-approval interrupt between `plan` and `execute_plan` using LangGraph’s interrupt mechanism.
- Store successful fix plans in a RAG index keyed by `(package, fixed_version)` so the planner can short-circuit when it’s seen a fix before.
- Replace the shared-secret auth with mTLS or OAuth client credentials.
