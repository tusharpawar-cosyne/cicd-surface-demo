from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / ".demo" / "out"
DIST_DIR = OUT_DIR / "dist"
RUNTIME_DIR = ROOT / ".demo" / "runtime"
RUNTIME_LOG_PATH = RUNTIME_DIR / "emitted.log"
REQUEST_PATH = OUT_DIR / "qstore-ingest-request.json"
EVIDENCE_PATH = OUT_DIR / "release-evidence.json"
REPORT_PATH = OUT_DIR / "run-report.md"
LOG_PATH = OUT_DIR / "logs.txt"
DEPLOY_RECEIPT_PATH = OUT_DIR / "deploy-receipt.json"
SCENARIO_PATTERN = re.compile(r"\[cicd:([a-z0-9-]+)\]")


@dataclass(frozen=True)
class Scenario:
    title: str
    summary: str
    service: str
    team: str
    risk_level: str
    environment_slug: str
    environment_label: str
    should_deploy: bool
    should_fail: bool
    run_conclusion: str
    job_conclusion: str
    step_conclusion: str
    artifact_name: str
    log_lines: tuple[str, ...]
    change_summary: str
    change_scope: str
    job_name: str
    step_name: str


VISIBLE_SCENARIOS = ("safe-release", "migration-repeat", "evidence-gap")
DEFAULT_SCENARIO = "safe-release"

SCENARIOS: dict[str, Scenario] = {
    "safe-release": Scenario(
        title="Revenue release moves forward",
        summary="Healthy checkout evidence clears the gate without a manual tool chase.",
        service="checkout-service",
        team="Commerce Platform",
        risk_level="LOW",
        environment_slug="prod-blue",
        environment_label="Prod Blue",
        should_deploy=True,
        should_fail=False,
        run_conclusion="success",
        job_conclusion="success",
        step_conclusion="success",
        artifact_name="checkout-bundle",
        log_lines=(
            "checkout smoke passed",
            "latency budget healthy",
            "artifact signature verified",
        ),
        change_summary="Cart latency fix, promo-code cache refresh, and smoke cleanup.",
        change_scope="3 commits, no schema changes, 1 artifact",
        job_name="checkout smoke",
        step_name="smoke tests",
    ),
    "migration-repeat": Scenario(
        title="Migration failure repeats a known outage",
        summary="A failing migration reproduces a dangerous historical pattern.",
        service="billing-service",
        team="Revenue Systems",
        risk_level="HIGH",
        environment_slug="prod-blue",
        environment_label="Prod Blue",
        should_deploy=False,
        should_fail=True,
        run_conclusion="failure",
        job_conclusion="failure",
        step_conclusion="failure",
        artifact_name="migration-report",
        log_lines=(
            "preflight ok",
            "migration FAILED: column already exists",
            "rollback requested",
        ),
        change_summary="Schema migration for recurring invoices.",
        change_scope="1 commit, 1 schema migration, rollback required",
        job_name="database migration",
        step_name="apply migration",
    ),
    "evidence-gap": Scenario(
        title="Green pipeline still lacks governance coverage",
        summary="The pipeline is green, but the target environment lacks approved release policy.",
        service="fulfillment-service",
        team="Operations Core",
        risk_level="MEDIUM",
        environment_slug="prod-gold",
        environment_label="Prod Gold",
        should_deploy=True,
        should_fail=False,
        run_conclusion="success",
        job_conclusion="success",
        step_conclusion="success",
        artifact_name="fulfillment-release",
        log_lines=(
            "build passed",
            "integration tests passed",
            "release evidence missing approved gate policy for target environment",
        ),
        change_summary="Warehouse routing refresh with no failing CI evidence.",
        change_scope="4 commits, environment change, 1 artifact",
        job_name="fulfillment verification",
        step_name="governance precheck",
    ),
    "night-parade": Scenario(
        title="Internal scenario",
        summary="Internal scenario used for additional release-surface coverage.",
        service="inventory-service",
        team="Supply Graph",
        risk_level="MEDIUM",
        environment_slug="staging-emerald",
        environment_label="Staging Emerald",
        should_deploy=True,
        should_fail=False,
        run_conclusion="success",
        job_conclusion="success",
        step_conclusion="success",
        artifact_name="inventory-preview",
        log_lines=(
            "inventory snapshot refreshed",
            "dependency audit passed",
            "promotion candidate assembled",
        ),
        change_summary="Internal release path for broader surface validation.",
        change_scope="2 commits, no schema changes, 2 artifacts",
        job_name="inventory preview",
        step_name="preview validation",
    ),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _required(name: str) -> str:
    value = _env(name)
    if value:
        return value
    raise SystemExit(f"missing required environment variable: {name}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _repo_parts(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


def _ensure_dirs() -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _append_summary(text: str) -> None:
    summary_path = _env("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")


def _write_output(name: str, value: str) -> None:
    output_path = _env("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _event_payload() -> dict[str, Any]:
    event_path = _env("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    path = Path(event_path)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _scenario_key() -> str:
    explicit = _env("INPUT_SCENARIO")
    if explicit:
        if explicit in SCENARIOS:
            return explicit
        visible = ", ".join(VISIBLE_SCENARIOS)
        raise SystemExit(
            f"unsupported scenario '{explicit}'. Visible scenarios: {visible}"
        )

    payload = _event_payload()
    candidates: list[str] = []
    head_commit = payload.get("head_commit")
    if isinstance(head_commit, dict):
        message = head_commit.get("message")
        if isinstance(message, str):
            candidates.append(message)
    commits = payload.get("commits")
    if isinstance(commits, list):
        for commit in commits:
            if isinstance(commit, dict):
                message = commit.get("message")
                if isinstance(message, str):
                    candidates.append(message)
    for message in candidates:
        match = SCENARIO_PATTERN.search(message)
        if not match:
            continue
        key = match.group(1)
        if key in SCENARIOS:
            return key
    return DEFAULT_SCENARIO


def _scenario() -> tuple[str, Scenario]:
    key = _scenario_key()
    return key, SCENARIOS[key]


def _artifact_size_bytes() -> int:
    total = 0
    for path in DIST_DIR.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _base_context() -> dict[str, Any]:
    repository = _required("GITHUB_REPOSITORY")
    owner, repo_name = _repo_parts(repository)
    run_id = _required("GITHUB_RUN_ID")
    run_attempt = int(_env("GITHUB_RUN_ATTEMPT", "1"))
    workflow = _required("GITHUB_WORKFLOW")
    sha = _required("GITHUB_SHA")
    ref_name = _required("GITHUB_REF_NAME")
    actor = _required("GITHUB_ACTOR")
    event_name = _required("GITHUB_EVENT_NAME")
    server_url = _required("GITHUB_SERVER_URL")
    release_id = _env("INPUT_RELEASE_ID") or f"rel-{run_id}-{run_attempt}"
    return {
        "repository": repository,
        "owner": owner,
        "repo_name": repo_name,
        "repository_id": f"gh:{repository}",
        "run_id": run_id,
        "run_attempt": run_attempt,
        "workflow": workflow,
        "sha": sha,
        "ref_name": ref_name,
        "actor": actor,
        "event_name": event_name,
        "server_url": server_url,
        "run_url": f"{server_url}/{repository}/actions/runs/{run_id}",
        "release_id": release_id,
        "notes": _env("INPUT_NOTES"),
    }


def _log_text_for(scenario: Scenario) -> str:
    if RUNTIME_LOG_PATH.is_file():
        return RUNTIME_LOG_PATH.read_text(encoding="utf-8")
    return "\n".join(scenario.log_lines) + "\n"


def _build_payload(scenario_key: str, scenario: Scenario, context: dict[str, Any]) -> dict[str, Any]:
    log_text = _log_text_for(scenario)
    build_job_id = int(context["run_id"]) * 10 + 1
    deployment_id = f"deploy-{context['run_id']}-{scenario.environment_slug}"
    payload = {
        "source": "webhook",
        "delivery_id": f"delivery-{context['run_id']}-{context['run_attempt']}-{scenario_key}",
        "repository": {
            "owner": context["owner"],
            "name": context["repo_name"],
        },
        "workflow": {
            "id": int(context["run_id"]),
            "name": context["workflow"],
        },
        "run": {
            "id": context["run_id"],
            "attempt": context["run_attempt"],
            "status": "Completed",
            "conclusion": scenario.run_conclusion.title(),
            "updated_at": _now_iso(),
            "head_sha": context["sha"],
            "environment": scenario.environment_label,
        },
        "jobs": [
            {
                "id": build_job_id,
                "name": scenario.job_name,
                "status": "completed",
                "conclusion": scenario.job_conclusion,
                "steps": [
                    {
                        "name": "checkout",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "name": scenario.step_name,
                        "status": "completed",
                        "conclusion": scenario.step_conclusion,
                    },
                ],
            }
        ],
        "artifacts": [
            {
                "id": int(context["run_id"]),
                "name": scenario.artifact_name,
                "size_in_bytes": 0,
            }
        ],
        "deployments": [],
        "logs": {"raw_text": log_text},
    }
    if scenario.should_deploy:
        payload["deployments"].append(
            {"id": deployment_id, "environment": scenario.environment_label}
        )
    return payload


def _build_request(scenario_key: str, scenario: Scenario, context: dict[str, Any]) -> dict[str, Any]:
    payload = _build_payload(scenario_key, scenario, context)
    return {
        "vendor": "github_actions",
        "repository_id": context["repository_id"],
        "run_id": context["run_id"],
        "event_id": payload["delivery_id"],
        "payload": payload,
    }


def _write_demo_files(scenario_key: str, scenario: Scenario, context: dict[str, Any]) -> dict[str, Any]:
    notes = context["notes"]
    (DIST_DIR / "release-notes.txt").write_text(
        "\n".join(
            [
                f"Release ID: {context['release_id']}",
                f"Scenario: {scenario_key}",
                f"Service: {scenario.service}",
                f"Change summary: {scenario.change_summary}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        DIST_DIR / "service-manifest.json",
        {
            "release_id": context["release_id"],
            "service": scenario.service,
            "team": scenario.team,
            "risk_level": scenario.risk_level,
            "environment": scenario.environment_slug,
            "ref_name": context["ref_name"],
            "head_sha": context["sha"],
        },
    )
    LOG_PATH.write_text(_log_text_for(scenario), encoding="utf-8")

    request = _build_request(scenario_key, scenario, context)
    request["payload"]["artifacts"][0]["size_in_bytes"] = _artifact_size_bytes()
    _write_json(REQUEST_PATH, request)

    evidence = {
        "scenario": scenario_key,
        "title": scenario.title,
        "summary": scenario.summary,
        "release_id": context["release_id"],
        "service": scenario.service,
        "team": scenario.team,
        "risk_level": scenario.risk_level,
        "environment": scenario.environment_slug,
        "run_url": context["run_url"],
        "change_summary": scenario.change_summary,
        "change_scope": scenario.change_scope,
        "logs_preview": _log_text_for(scenario).strip().splitlines(),
    }
    if notes:
        evidence["operator_notes"] = notes
    _write_json(EVIDENCE_PATH, evidence)

    report = "\n".join(
        [
            f"# {scenario.title}",
            "",
            scenario.summary,
            "",
            f"- Release ID: {context['release_id']}",
            f"- Service: {scenario.service}",
            f"- Team: {scenario.team}",
            f"- Risk: {scenario.risk_level}",
            f"- Environment: {scenario.environment_slug}",
            f"- Run URL: {context['run_url']}",
        ]
    )
    REPORT_PATH.write_text(report + "\n", encoding="utf-8")
    return request


def select_scenario() -> int:
    scenario_key, scenario = _scenario()
    _write_output("scenario", scenario_key)
    _write_output("environment", scenario.environment_slug)
    print(f"Selected scenario: {scenario_key}")
    return 0


def emit_log() -> int:
    _, scenario = _scenario()
    _ensure_dirs()
    log_text = "\n".join(scenario.log_lines) + "\n"
    RUNTIME_LOG_PATH.write_text(log_text, encoding="utf-8")
    sys.stdout.write(log_text)
    return 0


def prepare() -> int:
    scenario_key, scenario = _scenario()
    context = _base_context()
    _ensure_dirs()
    request = _write_demo_files(scenario_key, scenario, context)
    deployment_id = ""
    if request["payload"]["deployments"]:
        deployment_id = str(request["payload"]["deployments"][0]["id"])

    _write_output("scenario", scenario_key)
    _write_output("environment", scenario.environment_slug)
    _write_output("should_deploy", str(scenario.should_deploy).lower())
    _write_output("should_fail", str(scenario.should_fail).lower())
    _write_output("deployment_id", deployment_id)
    _write_output("canonical_run_id", f"{context['repository_id']}:run:{context['run_id']}")

    _append_summary(
        "\n".join(
            [
                f"## Prepared {scenario_key}",
                f"- Title: {scenario.title}",
                f"- Service: {scenario.service}",
                f"- Environment: {scenario.environment_slug}",
                f"- Artifact: qstore-evidence-{context['run_id']}",
                "",
            ]
        )
    )
    return 0


def finalize_deploy() -> int:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    deploy_environment = _required("DEPLOY_ENVIRONMENT")
    deployment_id = _required("DEPLOYMENT_ID")
    run_id = _required("GITHUB_RUN_ID")
    deploy_job = {
        "id": int(run_id) * 10 + 2,
        "name": f"deploy {deploy_environment}",
        "status": "completed",
        "conclusion": "success",
        "steps": [
            {"name": "review gate", "status": "completed", "conclusion": "success"},
            {"name": "promote candidate", "status": "completed", "conclusion": "success"},
        ],
    }
    request["payload"].setdefault("jobs", []).append(deploy_job)
    request["payload"]["logs"]["raw_text"] += (
        f"deployment promoted to {deploy_environment}\n"
    )
    _write_json(REQUEST_PATH, request)

    receipt = {
        "deployment_id": deployment_id,
        "environment": deploy_environment,
        "run_url": f"{_required('GITHUB_SERVER_URL')}/{_required('GITHUB_REPOSITORY')}/actions/runs/{run_id}",
        "status": "proposed",
    }
    _write_json(DEPLOY_RECEIPT_PATH, receipt)

    _append_summary(
        "\n".join(
            [
                "## Deploy job reached environment gate",
                f"- Environment: {deploy_environment}",
                f"- Deployment ID: {deployment_id}",
                "",
            ]
        )
    )
    return 0


def summarize() -> int:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    prepare_result = _env("PREPARE_JOB_RESULT", "unknown")
    deploy_result = _env("DEPLOY_JOB_RESULT", "unknown")
    scenario_key, _ = _scenario()
    summary_lines = [
        "## Workflow summary",
        f"- Scenario input: {scenario_key}",
        f"- Prepare job: {prepare_result}",
        f"- Deploy job: {deploy_result}",
        f"- Canonical run ID: {request['repository_id']}:run:{request['run_id']}",
        f"- Evidence artifact: qstore-evidence-{request['run_id']}",
        "",
        "Use `qstore-ingest-request.json` as the body for `cicd.run.ingest`.",
        "",
    ]
    _append_summary("\n".join(summary_lines))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python scripts/release_demo.py <select-scenario|emit-log|prepare|finalize-deploy|summarize>",
            file=sys.stderr,
        )
        return 2
    command = argv[1]
    if command == "select-scenario":
        return select_scenario()
    if command == "emit-log":
        return emit_log()
    if command == "prepare":
        return prepare()
    if command == "finalize-deploy":
        return finalize_deploy()
    if command == "summarize":
        return summarize()
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
