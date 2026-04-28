from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DEFAULT_QSTORE_ROOT = "/Users/tushar/Project/cosyne/QStore"
DEFAULT_QSTORE_PYTHON = "/Users/tushar/miniconda3/envs/a1/bin/python"
DEFAULT_RUN_INFO_PATH = "/tmp/cicd-demo/logs/current-run.env"
DEFAULT_EXTERNAL_BASE_URL = "http://127.0.0.1:18080"
DEFAULT_EXTERNAL_NAMESPACE_ID = "gh:octo/widgets"
PRIMARY_ENVIRONMENT = "prod-blue"
SHOWCASE_GATE_POLICY = {
    "schema_version": "cicd_gate_policy_v1",
    "allowed_run_conclusions": ["success"],
    "allowed_environments": [PRIMARY_ENVIRONMENT],
    "max_failed_jobs": 0,
    "max_artifact_age_seconds": 1800,
    "missing_evidence_disposition": "HOLD",
    "require_deployment_history": True,
}

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_DIR = ROOT / ".demo" / "downloaded"
ANALYSIS_DIR = ROOT / ".demo" / "analysis"
REQUEST_PATH = ARTIFACT_DIR / "qstore-ingest-request.json"
EVIDENCE_PATH = ARTIFACT_DIR / "release-evidence.json"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _append_summary(text: str) -> None:
    summary_path = _env("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")


def _structured(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool call failed: {result}")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return dict(structured)
    raise RuntimeError(f"MCP tool returned no structuredContent: {result}")


def _normalize_environment(value: str) -> str:
    return value.strip().lower().replace(" ", "-")


def _load_run_info() -> dict[str, str]:
    run_info_path = Path(_env("CICD_DEMO_RUN_INFO_PATH", DEFAULT_RUN_INFO_PATH))
    if not run_info_path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in run_info_path.read_text(encoding="utf-8").splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _analysis_paths(
    *,
    request_path: Path | None = None,
    evidence_path: Path | None = None,
    output_dir: Path | None = None,
) -> tuple[Path, Path, Path]:
    return (
        request_path or REQUEST_PATH,
        evidence_path or EVIDENCE_PATH,
        output_dir or ANALYSIS_DIR,
    )


def _bootstrap_qstore_imports(qstore_root: Path) -> None:
    extra_paths = (
        qstore_root / "mcp" / "cicd" / "src",
        qstore_root / "cli" / "cicd-qardinal-http" / "src",
        qstore_root / "packages" / "qardinal-client" / "src",
        qstore_root / "packages" / "surface-sdk" / "src",
        qstore_root / "providers" / "qardinal" / "src",
        qstore_root / "providers" / "qstore" / "src",
        qstore_root / "providers" / "toy-graph-store" / "src",
        qstore_root / "providers" / "toy-store" / "src",
        qstore_root / "surfaces" / "cicd" / "src",
        qstore_root / "packages" / "qgraph-protos" / "src",
        qstore_root / "packages" / "qstore-protos" / "src",
        qstore_root,
    )
    for path in extra_paths:
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


class _TemporaryEnvironment:
    def __init__(self, updates: dict[str, str]) -> None:
        self._updates = updates
        self._original: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self._updates.items():
            self._original[key] = os.environ.get(key)
            os.environ[key] = value
        return None

    def __exit__(self, exc_type, exc, tb) -> None:
        for key, original in self._original.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def _external_demo_env(run_info: dict[str, str]) -> dict[str, str]:
    base_url = _env(
        "QALQI_CICD_MCP_QARDINAL_BASE_URL",
        run_info.get("HOST_BASE_URL", DEFAULT_EXTERNAL_BASE_URL),
    )
    connect_timeout_ms = _env(
        "QALQI_CICD_MCP_CONNECT_TIMEOUT_MS",
        run_info.get("CONNECT_TIMEOUT_MS", "500"),
    )
    request_timeout_ms = _env(
        "QALQI_CICD_MCP_REQUEST_TIMEOUT_MS",
        run_info.get("REQUEST_TIMEOUT_MS", "20000"),
    )
    api_key = _env("QALQI_CICD_MCP_API_KEY", "local-dev")
    startup_namespace_id = _env(
        "QALQI_CICD_MCP_STARTUP_NAMESPACE_ID",
        DEFAULT_EXTERNAL_NAMESPACE_ID,
    )
    updates = {
        "CICD_DEMO_QARDINAL_BASE_URL": base_url,
        "CICD_DEMO_QARDINAL_CONNECT_TIMEOUT_MS": connect_timeout_ms,
        "CICD_DEMO_QARDINAL_REQUEST_TIMEOUT_MS": request_timeout_ms,
        "CICD_DEMO_QARDINAL_API_KEY": api_key,
        "CICD_DEMO_NAMESPACE_ID": startup_namespace_id,
    }
    for key, mapped in (
        ("STATE_ROOT", "CICD_DEMO_STATE_ROOT"),
        ("ARTIFACT_ROOT", "CICD_DEMO_ARTIFACT_ROOT"),
        ("SQLITE_PATH", "CICD_DEMO_QARDINAL_SQLITE_PATH"),
        ("GRAPH_STORE_UDS_PATH", "CICD_DEMO_GRAPH_STORE_UDS_PATH"),
        ("TENSOR_STORE_UDS_PATH", "CICD_DEMO_TENSOR_STORE_UDS_PATH"),
    ):
        if key in run_info:
            updates[mapped] = run_info[key]
    return updates


def _ensure_external_namespace_ready(namespace_id: str, qstore_root: Path) -> None:
    run_info = _load_run_info()
    _bootstrap_qstore_imports(qstore_root)

    from demo.cicd.bff.host_client import CicdHostConfig, ExternalCicdHostClient
    from demo.cicd.bff.surface_runtime import LiveCicdSurfaceBridge

    if run_info:
        env_updates = _external_demo_env(run_info)
        with _TemporaryEnvironment(env_updates):
            bridge = LiveCicdSurfaceBridge()
            try:
                bridge.seed_gate_policy(
                    namespace_id=namespace_id,
                    environment=PRIMARY_ENVIRONMENT,
                    payload=SHOWCASE_GATE_POLICY,
                )
            finally:
                bridge.close()
        return

    base_url = _env(
        "QALQI_CICD_MCP_QARDINAL_BASE_URL",
        run_info.get("HOST_BASE_URL", DEFAULT_EXTERNAL_BASE_URL),
    )
    api_key = _env("QALQI_CICD_MCP_API_KEY", "local-dev")
    connect_timeout_ms = int(
        _env("QALQI_CICD_MCP_CONNECT_TIMEOUT_MS", run_info.get("CONNECT_TIMEOUT_MS", "500"))
    )
    request_timeout_ms = int(
        _env("QALQI_CICD_MCP_REQUEST_TIMEOUT_MS", run_info.get("REQUEST_TIMEOUT_MS", "20000"))
    )
    startup_namespace_id = _env(
        "QALQI_CICD_MCP_STARTUP_NAMESPACE_ID",
        DEFAULT_EXTERNAL_NAMESPACE_ID,
    )
    principal_id = _env("CICD_DEMO_QARDINAL_PRINCIPAL_ID", "cicd-demo-principal")
    state_root = Path(run_info.get("STATE_ROOT", "/tmp/cicd-host"))
    config = CicdHostConfig(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        request_timeout_ms=request_timeout_ms,
        connect_timeout_ms=connect_timeout_ms,
        authenticated_principal_id=principal_id,
        state_root=state_root,
        sqlite_path=Path(run_info.get("SQLITE_PATH", str(state_root / "qardinal.sqlite3"))),
        artifact_root=Path(run_info.get("ARTIFACT_ROOT", str(state_root / "artifacts"))),
        graph_store_uds_path=Path(run_info.get("GRAPH_STORE_UDS_PATH", str(state_root / "graph.sock"))),
        tensor_store_uds_path=Path(run_info.get("TENSOR_STORE_UDS_PATH", str(state_root / "tensor.sock"))),
        manifest_root=qstore_root / "surfaces" / "cicd" / "manifests",
        seed_namespace_id=startup_namespace_id,
    )
    client = ExternalCicdHostClient(config)
    client.ensure_binding(namespace_id=namespace_id)


def _server_parameters(namespace_id: str) -> StdioServerParameters:
    qstore_root = Path(_env("QSTORE_REPO_ROOT", DEFAULT_QSTORE_ROOT)).resolve()
    qstore_python = _env("QSTORE_PYTHON", DEFAULT_QSTORE_PYTHON) or sys.executable
    server_path = qstore_root / "mcp" / "cicd" / "src" / "qalqi_cicd_mcp" / "main.py"
    if not server_path.is_file():
        raise RuntimeError(f"QStore MCP server not found at {server_path}")

    env = os.environ.copy()
    mode = _env("QALQI_CICD_MCP_MODE", "external" if _load_run_info() else "smoke").lower()
    env["QALQI_CICD_MCP_MODE"] = mode

    if mode == "external":
        run_info = _load_run_info()
        _ensure_external_namespace_ready(namespace_id, qstore_root)
        env.setdefault(
            "QALQI_CICD_MCP_QARDINAL_BASE_URL",
            run_info.get("HOST_BASE_URL", DEFAULT_EXTERNAL_BASE_URL),
        )
        env.setdefault("QALQI_CICD_MCP_API_KEY", _env("QALQI_CICD_MCP_API_KEY", "local-dev"))
        env.setdefault(
            "QALQI_CICD_MCP_CONNECT_TIMEOUT_MS",
            _env("QALQI_CICD_MCP_CONNECT_TIMEOUT_MS", run_info.get("CONNECT_TIMEOUT_MS", "500")),
        )
        env.setdefault(
            "QALQI_CICD_MCP_REQUEST_TIMEOUT_MS",
            _env("QALQI_CICD_MCP_REQUEST_TIMEOUT_MS", run_info.get("REQUEST_TIMEOUT_MS", "20000")),
        )
        env.setdefault(
            "QALQI_CICD_MCP_STARTUP_NAMESPACE_ID",
            _env("QALQI_CICD_MCP_STARTUP_NAMESPACE_ID", DEFAULT_EXTERNAL_NAMESPACE_ID),
        )
        env.setdefault(
            "QALQI_CICD_MCP_DEFAULT_NAMESPACE_ID",
            _env("QALQI_CICD_MCP_DEFAULT_NAMESPACE_ID", DEFAULT_EXTERNAL_NAMESPACE_ID),
        )
    else:
        env.setdefault("QALQI_CICD_MCP_STARTUP_NAMESPACE_ID", namespace_id)
        env.setdefault("QALQI_CICD_MCP_DEFAULT_NAMESPACE_ID", namespace_id)
        env.setdefault("QALQI_CICD_MCP_CONNECT_TIMEOUT_MS", "1000")
        env.setdefault("QALQI_CICD_MCP_REQUEST_TIMEOUT_MS", "30000")

    return StdioServerParameters(
        command=qstore_python,
        args=[str(server_path)],
        env=env,
        cwd=str(qstore_root),
    )


async def analyze_request(
    request: dict[str, Any],
    evidence: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    repository_id = str(request["repository_id"])
    run_id = str(request["run_id"])
    canonical_run_id = f"{repository_id}:run:{run_id}"
    deployments = request.get("payload", {}).get("deployments") or []
    deployment_id = str(deployments[0]["id"]) if deployments else None
    canonical_deployment_id = (
        f"{repository_id}:deployment:{deployment_id}" if deployment_id else None
    )
    environment = str(evidence.get("environment") or "")
    if not environment:
        environment = _normalize_environment(
            str(request.get("payload", {}).get("run", {}).get("environment", ""))
        )
    approval_projection_id = f"approval-{run_id}"

    server = _server_parameters(repository_id)
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            ingest = _structured(await session.call_tool("cicd.run.ingest", request))
            search = _structured(
                await session.call_tool(
                    "cicd.run.search",
                    {"repository_id": repository_id, "canonical_id": canonical_run_id},
                )
            )
            explain = _structured(
                await session.call_tool(
                    "cicd.failure.explain",
                    {"target_id": canonical_run_id, "target_kind": "run"},
                )
            )
            evaluate = _structured(
                await session.call_tool(
                    "cicd.release.evaluate",
                    {
                        "target_id": canonical_run_id,
                        "target_kind": "run",
                        "environment": environment,
                    },
                )
            )
            deploy = None
            rollback = None
            if canonical_deployment_id is not None:
                deploy = _structured(
                    await session.call_tool(
                        "cicd.deploy.propose",
                        {
                            "target_id": canonical_deployment_id,
                            "environment": environment,
                            "approval_projection_id": approval_projection_id,
                        },
                    )
                )
                rollback = _structured(
                    await session.call_tool(
                        "cicd.rollback.propose",
                        {
                            "target_id": canonical_deployment_id,
                            "environment": environment,
                            "deployment_id": deployment_id,
                        },
                    )
                )

    result = {
        "repository_id": repository_id,
        "run_id": run_id,
        "canonical_run_id": canonical_run_id,
        "environment": environment,
        "tool_count": len(tools.tools),
        "ingest": ingest,
        "search": search,
        "failure_explain": explain,
        "release_evaluate": evaluate,
        "deploy_propose": deploy,
        "rollback_propose": rollback,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "analysis-results.json"
    summary_path = output_dir / "analysis-summary.md"
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    summary_lines = [
        "# QStore MCP Analysis",
        "",
        f"- Repository: `{repository_id}`",
        f"- Canonical run: `{canonical_run_id}`",
        f"- Ingest status: `{ingest['ingest_status']}`",
        f"- Dominant cause: `{explain['dominant_cause']}`",
        f"- Release verdict: `{evaluate['verdict']}`",
        f"- Environment health: `{evaluate['environment_health']['status']}`",
    ]
    if deploy is not None:
        summary_lines.append(f"- Deploy readiness: `{deploy['readiness_class']}`")
    if rollback is not None:
        summary_lines.append(f"- Rollback safety: `{rollback['safety_class']}`")
    summary_lines.extend(
        [
            "",
            "## Reasons",
            *[f"- `{code}`" for code in evaluate.get("reasons", [])],
            "",
        ]
    )
    summary_text = "\n".join(summary_lines) + "\n"
    summary_path.write_text(summary_text, encoding="utf-8")
    _append_summary(summary_text)
    return result


async def analyze_paths(
    *,
    request_path: Path | None = None,
    evidence_path: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    request_file, evidence_file, resolved_output_dir = _analysis_paths(
        request_path=request_path,
        evidence_path=evidence_path,
        output_dir=output_dir,
    )
    if not request_file.is_file():
        raise RuntimeError(f"missing ingest request: {request_file}")
    if not evidence_file.is_file():
        raise RuntimeError(f"missing release evidence: {evidence_file}")

    request = json.loads(request_file.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
    return await analyze_request(request, evidence, output_dir=resolved_output_dir)


def run_analysis(
    *,
    request_path: Path | None = None,
    evidence_path: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        analyze_paths(
            request_path=request_path,
            evidence_path=evidence_path,
            output_dir=output_dir,
        )
    )


def main() -> int:
    try:
        run_analysis()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
