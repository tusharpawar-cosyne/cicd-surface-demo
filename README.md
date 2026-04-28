# CI/CD Surface Demo Repo

Public GitHub Actions source repo for exercising the QStore CI/CD surface with
real workflow runs, live GitHub Actions outputs, governed ingestion, and
automated MCP-based analysis against a local external host.

## What happens on push

After the one-time local setup, a single push drives the full flow:

1. GitHub Actions runs in GitHub-hosted CI.
2. The workflow emits logs and uploads `qstore-evidence-<run_id>`.
3. A local watcher process polls GitHub for newly completed runs.
4. The watcher downloads the live run metadata, job list, and log archive.
5. The watcher rebuilds the ingest request from those live outputs.
6. The watcher calls the local `cicd` MCP server in `external` mode.
7. QStore ingests the run, evaluates it, and writes a governed summary under `.demo/watcher/runs/<run_id>/analysis`.

The per-run manual action is only pushing code.

## Visible demo scenarios

The workflow supports these visible cases:

- `safe-release`
- `migration-repeat`
- `evidence-gap`

For `workflow_dispatch`, you can pass one explicitly.

For `push`, the workflow chooses automatically:

- default: `safe-release`
- commit message contains `[cicd:migration-repeat]`: uses `migration-repeat`
- commit message contains `[cicd:evidence-gap]`: uses `evidence-gap`

## One-time local setup

1. Start the local CI/CD stack and keep it running:

   ```bash
   bash /Users/tushar/Project/cosyne/QStore/demo/cicd/run_demo.sh
   ```

2. Export a GitHub token that can read Actions runs and artifacts for this repo:

   ```bash
   export GITHUB_TOKEN=YOUR_TOKEN
   ```

   Minimum practical permissions for a PAT are repository read access plus Actions read access.

3. Start the local watcher from this repo:

   ```bash
   /Users/tushar/miniconda3/envs/a1/bin/python scripts/watch_github_runs.py
   ```

   Add `--replay-existing` if you want the watcher to also analyze completed runs that already existed before the watcher started:

   ```bash
   /Users/tushar/miniconda3/envs/a1/bin/python scripts/watch_github_runs.py --replay-existing
   ```

Optional overrides:

- `WATCH_REPOSITORY=owner/repo`
- `WATCH_POLL_INTERVAL_SECONDS=30`
- `QSTORE_REPO_ROOT=/Users/tushar/Project/cosyne/QStore`
- `QSTORE_PYTHON=/Users/tushar/miniconda3/envs/a1/bin/python`
- `QALQI_CICD_MCP_MODE=external`
- `QALQI_CICD_MCP_QARDINAL_BASE_URL=http://127.0.0.1:18080`
- `QALQI_CICD_MCP_API_KEY=local-dev`
- `QALQI_CICD_MCP_STARTUP_NAMESPACE_ID=gh:octo/widgets`
- `QALQI_CICD_MCP_DEFAULT_NAMESPACE_ID=gh:octo/widgets`

The watcher reads `/tmp/cicd-demo/logs/current-run.env` from `run_demo.sh` so it can
bind the repo namespace on the running host and seed the primary `prod-blue` gate policy
before MCP analysis.

## Workflow outputs

GitHub uploads these artifacts per run:

- `qstore-evidence-<run_id>`
  - `qstore-ingest-request.json`
  - `release-evidence.json`
  - `run-report.md`
  - `logs.txt`
  - `dist/`
- `deployment-receipt-<run_id>` for deploying cases

The local watcher then writes these per-run files under `.demo/watcher/runs/<run_id>`:

- `github/workflow-run.json`
- `github/jobs.json`
- `github/artifacts.json`
- `logs-live.txt`
- `qstore-ingest-request-live.json`
- `release-evidence.json`
- `analysis/analysis-results.json`
- `analysis/analysis-summary.md`

## How the local MCP analysis works

`scripts/analyze_with_qstore.py` now supports the external-host path directly:

- it reads the live ingest request and release evidence,
- ensures the repo namespace is bound on the running CI/CD host,
- seeds the `prod-blue` gate policy into that namespace,
- starts the local `cicd` MCP server in `external` mode,
- runs `cicd.run.ingest`, `cicd.run.search`, `cicd.failure.explain`, `cicd.release.evaluate`, `cicd.deploy.propose`, and `cicd.rollback.propose`.

That keeps GitHub-hosted CI and local governed analysis cleanly separated.

## Repo layout

- `.github/workflows/release-demo.yml`: GitHub-hosted workflow that produces live run outputs and evidence artifacts
- `scripts/release_demo.py`: scenario selection, log emission, and evidence generation
- `scripts/watch_github_runs.py`: local poller that turns completed GitHub runs into local governed analyses
- `scripts/analyze_with_qstore.py`: local MCP ingestion and analysis client for the downloaded run payload

test4