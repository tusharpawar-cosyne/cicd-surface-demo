from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib import error, parse, request
import zipfile

from analyze_with_qstore import run_analysis

ROOT = Path(__file__).resolve().parents[1]
WATCH_ROOT = ROOT / ".demo" / "watcher"
RUNS_ROOT = WATCH_ROOT / "runs"
STATE_PATH = WATCH_ROOT / "state.json"
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_USER_AGENT = "cicd-surface-demo-watcher/1.0"
TARGET_WORKFLOW_NAME = "release-decision-demo"
EVIDENCE_ARTIFACT_PREFIX = "qstore-evidence-"
DEPLOYMENT_ARTIFACT_PREFIX = "deployment-receipt-"
MAX_INLINE_LOG_BYTES = 900_000


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require_token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        value = _env(name)
        if value:
            return value
    raise SystemExit("Set GITHUB_TOKEN, GH_TOKEN, or GITHUB_PAT before starting the watcher.")


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {"processed_runs": {}}
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed_runs": {}}
    if not isinstance(payload, dict):
        return {"processed_runs": {}}
    payload.setdefault("processed_runs", {})
    return payload


def _save_state(state: dict[str, Any]) -> None:
    WATCH_ROOT.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _repo_slug() -> str:
    explicit = _env("WATCH_REPOSITORY")
    if explicit:
        return explicit
    command = ["git", "-C", str(ROOT), "remote", "get-url", "origin"]
    try:
        remote = subprocess.check_output(command, text=True).strip()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Unable to determine repo remote. Set WATCH_REPOSITORY. ({exc})") from exc
    if remote.startswith("git@github.com:"):
        slug = remote.split(":", 1)[1]
    elif remote.startswith("https://github.com/"):
        slug = remote.split("https://github.com/", 1)[1]
    else:
        raise SystemExit(f"Unsupported origin remote for watcher: {remote}")
    if slug.endswith(".git"):
        slug = slug[:-4]
    return slug


class GitHubClient:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self._token = token
        self._base = f"https://api.github.com/repos/{repo}"

    def _request(self, url: str, *, accept: str = "application/vnd.github+json") -> bytes:
        req = request.Request(
            url,
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": _env("WATCH_USER_AGENT", DEFAULT_USER_AGENT),
            },
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                return response.read()
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {exc.code} for {url}: {body}") from exc

    def json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = f"?{parse.urlencode(params)}" if params else ""
        payload = self._request(f"{self._base}{path}{query}")
        return json.loads(payload.decode("utf-8"))

    def bytes(self, url: str, *, accept: str = "application/octet-stream") -> bytes:
        return self._request(url, accept=accept)


def _workflow_runs(client: GitHubClient) -> list[dict[str, Any]]:
    payload = client.json(
        "/actions/runs",
        params={"status": "completed", "per_page": 20},
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return []
    filtered: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "")
        path = str(run.get("path") or "")
        if name == TARGET_WORKFLOW_NAME or ".github/workflows/release-demo.yml" in path:
            filtered.append(run)
    filtered.sort(key=lambda item: int(item.get("run_number") or 0))
    return filtered


def _artifacts_for_run(client: GitHubClient, run_id: str) -> list[dict[str, Any]]:
    payload = client.json(f"/actions/runs/{run_id}/artifacts", params={"per_page": 100})
    artifacts = payload.get("artifacts")
    return [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []


def _jobs_for_run(client: GitHubClient, run_id: str) -> list[dict[str, Any]]:
    payload = client.json(f"/actions/runs/{run_id}/jobs", params={"per_page": 100})
    jobs = payload.get("jobs")
    return [item for item in jobs if isinstance(item, dict)] if isinstance(jobs, list) else []


def _download_and_extract_artifact(client: GitHubClient, artifact: dict[str, Any], destination: Path) -> None:
    archive_url = str(artifact.get("archive_download_url") or "")
    if not archive_url:
        raise RuntimeError(f"Artifact {artifact.get('name')} has no archive_download_url")
    destination.mkdir(parents=True, exist_ok=True)
    payload = client.bytes(archive_url)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(destination)


def _find_file(root: Path, filename: str) -> Path | None:
    direct = root / filename
    if direct.is_file():
        return direct
    matches = sorted(root.rglob(filename))
    return matches[0] if matches else None


def _download_logs_text(client: GitHubClient, run_id: str, fallback_path: Path | None) -> str:
    api_url = f"https://api.github.com/repos/{client.repo}/actions/runs/{run_id}/logs"
    try:
        payload = client.bytes(api_url)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            pieces: list[str] = []
            for name in sorted(zf.namelist()):
                if name.endswith("/"):
                    continue
                text = zf.read(name).decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                pieces.append(f"===== {name} =====\n{text}")
        if pieces:
            return "\n\n".join(pieces) + "\n"
    except Exception as exc:  # noqa: BLE001
        print(f"[watcher] log download fallback for run {run_id}: {exc}", file=sys.stderr)
    if fallback_path is not None and fallback_path.is_file():
        return fallback_path.read_text(encoding="utf-8")
    raise RuntimeError(f"Unable to load logs for run {run_id}")


def _trim_logs(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= MAX_INLINE_LOG_BYTES:
        return text
    head_bytes = data[: MAX_INLINE_LOG_BYTES // 2]
    tail_bytes = data[-MAX_INLINE_LOG_BYTES // 2 :]
    head = head_bytes.decode("utf-8", errors="ignore")
    tail = tail_bytes.decode("utf-8", errors="ignore")
    return head + "\n\n...[truncated for ingest budget]...\n\n" + tail


def _normalize_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for job in jobs:
        steps_payload = job.get("steps")
        steps: list[dict[str, Any]] = []
        if isinstance(steps_payload, list):
            for step in steps_payload:
                if not isinstance(step, dict):
                    continue
                steps.append(
                    {
                        "name": str(step.get("name") or "step"),
                        "status": str(step.get("status") or "completed"),
                        "conclusion": str(step.get("conclusion") or "success"),
                    }
                )
        normalized.append(
            {
                "id": int(job.get("id") or 0),
                "name": str(job.get("name") or "job"),
                "status": str(job.get("status") or "completed"),
                "conclusion": str(job.get("conclusion") or "success"),
                "steps": steps,
            }
        )
    return normalized


def _normalize_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for artifact in artifacts:
        normalized.append(
            {
                "id": int(artifact.get("id") or 0),
                "name": str(artifact.get("name") or "artifact"),
                "size_in_bytes": int(artifact.get("size_in_bytes") or 0),
            }
        )
    return normalized


def _capitalize(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    return value[:1].upper() + value[1:]


def _build_live_request(
    seed_request: dict[str, Any],
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    logs_text: str,
    deploy_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    request_payload = json.loads(json.dumps(seed_request))
    payload = request_payload.setdefault("payload", {})
    workflow = payload.setdefault("workflow", {})
    workflow["id"] = int(run.get("workflow_id") or workflow.get("id") or run.get("id") or 0)
    workflow["name"] = str(run.get("name") or workflow.get("name") or TARGET_WORKFLOW_NAME)

    run_payload = payload.setdefault("run", {})
    run_payload["id"] = str(run.get("id") or request_payload.get("run_id") or run_payload.get("id") or "")
    run_payload["attempt"] = int(run.get("run_attempt") or run_payload.get("attempt") or 1)
    run_payload["status"] = _capitalize(str(run.get("status") or run_payload.get("status") or "completed"))
    run_payload["conclusion"] = _capitalize(
        str(run.get("conclusion") or run_payload.get("conclusion") or "success")
    )
    run_payload["updated_at"] = str(run.get("updated_at") or run_payload.get("updated_at") or "")
    run_payload["head_sha"] = str(run.get("head_sha") or run_payload.get("head_sha") or "")

    payload["jobs"] = _normalize_jobs(jobs) or payload.get("jobs") or []
    payload["artifacts"] = _normalize_artifacts(artifacts) or payload.get("artifacts") or []
    payload.setdefault("logs", {})["raw_text"] = _trim_logs(logs_text)

    if deploy_receipt is not None:
        deployment_id = deploy_receipt.get("deployment_id")
        environment = deploy_receipt.get("environment")
        if deployment_id and environment:
            payload["deployments"] = [{"id": str(deployment_id), "environment": str(environment)}]

    request_payload["run_id"] = run_payload["id"]
    request_payload["event_id"] = str(payload.get("delivery_id") or request_payload.get("event_id") or "")
    return request_payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _process_run(client: GitHubClient, run: dict[str, Any], state: dict[str, Any]) -> None:
    run_id = str(run["id"])
    run_key = f"{run_id}:{run.get('updated_at', '')}"
    processed = state.setdefault("processed_runs", {})
    if processed.get(run_id) == run_key:
        return

    run_root = RUNS_ROOT / run_id
    downloaded_root = run_root / "downloaded"
    analysis_root = run_root / "analysis"
    api_root = run_root / "github"
    run_root.mkdir(parents=True, exist_ok=True)
    downloaded_root.mkdir(parents=True, exist_ok=True)
    api_root.mkdir(parents=True, exist_ok=True)

    artifacts = _artifacts_for_run(client, run_id)
    jobs = _jobs_for_run(client, run_id)
    _write_json(api_root / "workflow-run.json", run)
    _write_json(api_root / "jobs.json", jobs)
    _write_json(api_root / "artifacts.json", artifacts)

    evidence_artifact = None
    deployment_artifact = None
    for artifact in artifacts:
        name = str(artifact.get("name") or "")
        if name == f"{EVIDENCE_ARTIFACT_PREFIX}{run_id}":
            evidence_artifact = artifact
        elif name == f"{DEPLOYMENT_ARTIFACT_PREFIX}{run_id}":
            deployment_artifact = artifact
    if evidence_artifact is None:
        print(f"[watcher] skip run {run_id}: evidence artifact not available yet", file=sys.stderr)
        return

    _download_and_extract_artifact(client, evidence_artifact, downloaded_root)
    if deployment_artifact is not None:
        _download_and_extract_artifact(client, deployment_artifact, downloaded_root / "deployment-receipt")

    request_path = _find_file(downloaded_root, "qstore-ingest-request.json")
    evidence_path = _find_file(downloaded_root, "release-evidence.json")
    logs_path = _find_file(downloaded_root, "logs.txt")
    deploy_receipt_path = _find_file(downloaded_root, "deploy-receipt.json")
    if request_path is None or evidence_path is None:
        raise RuntimeError(f"Run {run_id} is missing required evidence files")

    seed_request = json.loads(request_path.read_text(encoding="utf-8"))
    deploy_receipt = None
    if deploy_receipt_path is not None:
        deploy_receipt = json.loads(deploy_receipt_path.read_text(encoding="utf-8"))

    logs_text = _download_logs_text(client, run_id, logs_path)
    live_request = _build_live_request(seed_request, run, jobs, artifacts, logs_text, deploy_receipt)
    live_request_path = run_root / "qstore-ingest-request-live.json"
    _write_json(live_request_path, live_request)

    copied_evidence_path = run_root / "release-evidence.json"
    copied_evidence_path.write_text(evidence_path.read_text(encoding="utf-8"), encoding="utf-8")
    (run_root / "logs-live.txt").write_text(logs_text, encoding="utf-8")

    run_analysis(
        request_path=live_request_path,
        evidence_path=copied_evidence_path,
        output_dir=analysis_root,
    )

    processed[run_id] = run_key
    _save_state(state)
    summary_path = analysis_root / "analysis-summary.md"
    print(f"[watcher] analyzed run {run_id}: {summary_path}")


def _poll_once() -> None:
    repo = _repo_slug()
    client = GitHubClient(repo=repo, token=_require_token())
    state = _load_state()
    for run in _workflow_runs(client):
        _process_run(client, run, state)


def main() -> int:
    interval = int(_env("WATCH_POLL_INTERVAL_SECONDS", str(DEFAULT_POLL_INTERVAL_SECONDS)))
    once = len(sys.argv) > 1 and sys.argv[1] == "once"
    while True:
        try:
            _poll_once()
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] {exc}", file=sys.stderr)
            if once:
                return 1
        if once:
            return 0
        time.sleep(max(interval, 5))


if __name__ == "__main__":
    raise SystemExit(main())
