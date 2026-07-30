"""LayerRail CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from layerrail_cli.client import (
    ConfigStore,
    LayerRailClient,
    LayerRailClientError,
)


def _print(value: Any, *, compact: bool = False) -> None:
    if value is None:
        return
    print(json.dumps(value, indent=None if compact else 2, sort_keys=True, default=str))


def _client() -> LayerRailClient:
    return LayerRailClient(ConfigStore.load())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="layerrail",
        description="Deploy and operate applications on LayerRail.",
    )
    root.add_argument("--json", action="store_true", help="Emit compact JSON")
    commands = root.add_subparsers(dest="command", required=True)

    config = commands.add_parser("config", help="Manage CLI credentials")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_set = config_commands.add_parser("set")
    config_set.add_argument("--url", required=True)
    config_set.add_argument("--token", required=True)
    config_commands.add_parser("show")

    commands.add_parser("whoami")

    projects = commands.add_parser("projects", help="Manage projects")
    project_commands = projects.add_subparsers(dest="projects_command", required=True)
    project_list = project_commands.add_parser("list")
    project_list.add_argument("--page", type=int, default=1)
    project_create = project_commands.add_parser("create")
    project_create.add_argument("--name", required=True)
    project_create.add_argument("--repo-id", type=int, required=True)
    project_create.add_argument("--repo", required=True)
    project_create.add_argument("--installation-id", type=int, required=True)
    project_create.add_argument("--branch", default="main")
    project_get = project_commands.add_parser("get")
    project_get.add_argument("project_id")
    project_export = project_commands.add_parser("export")
    project_export.add_argument("project_id")
    project_export.add_argument("--output")

    project_config = commands.add_parser("project-config", help="Pull or push .layerrail.json")
    config_commands = project_config.add_subparsers(dest="project_config_command", required=True)
    config_pull = config_commands.add_parser("pull")
    config_pull.add_argument("project_id")
    config_pull.add_argument("--output", default=".layerrail.json")
    config_push = config_commands.add_parser("push")
    config_push.add_argument("project_id")
    config_push.add_argument("--file", default=".layerrail.json")

    deployments = commands.add_parser("deployments", help="Manage deployments")
    deployment_commands = deployments.add_subparsers(dest="deployments_command", required=True)
    deployment_list = deployment_commands.add_parser("list")
    deployment_list.add_argument("project_id")
    deployment_list.add_argument("--page", type=int, default=1)
    deploy = deployment_commands.add_parser("create")
    deploy.add_argument("project_id")
    deploy.add_argument("--branch")
    deploy.add_argument("--commit")
    deployment_get = deployment_commands.add_parser("get")
    deployment_get.add_argument("deployment_id")
    deployment_cancel = deployment_commands.add_parser("cancel")
    deployment_cancel.add_argument("deployment_id")
    deployment_logs = deployment_commands.add_parser("logs")
    deployment_logs.add_argument("deployment_id")
    deployment_logs.add_argument("--limit", type=int, default=1000)
    deployment_logs.add_argument("--keyword")
    rollback = deployment_commands.add_parser("rollback")
    rollback.add_argument("project_id")
    rollback.add_argument("environment_id")

    webhooks = commands.add_parser("webhooks", help="Manage signed webhooks")
    webhook_commands = webhooks.add_subparsers(dest="webhooks_command", required=True)
    webhook_commands.add_parser("list")
    webhook_create = webhook_commands.add_parser("create")
    webhook_create.add_argument("--name", required=True)
    webhook_create.add_argument("--url", required=True)
    webhook_create.add_argument("--event", action="append", required=True)
    webhook_delete = webhook_commands.add_parser("delete")
    webhook_delete.add_argument("endpoint_id")
    webhook_test = webhook_commands.add_parser("test")
    webhook_test.add_argument("endpoint_id")

    audit = commands.add_parser("audit", help="Read team audit history")
    audit.add_argument("--limit", type=int, default=100)
    return root


def run(args: argparse.Namespace) -> Any:
    if args.command == "config":
        if args.config_command == "set":
            path = ConfigStore.save(url=args.url, token=args.token)
            return {"configured": True, "path": str(path)}
        config = ConfigStore.load()
        return {"url": config.url, "token": config.token[:16] + "••••"}

    client = _client()
    if args.command == "whoami":
        return client.request("GET", "whoami")
    if args.command == "projects":
        if args.projects_command == "list":
            return client.request("GET", "projects", query={"page": args.page})
        if args.projects_command == "create":
            return client.request(
                "POST",
                "projects",
                payload={
                    "name": args.name,
                    "repo_id": args.repo_id,
                    "repo_full_name": args.repo,
                    "github_installation_id": args.installation_id,
                    "production_branch": args.branch,
                },
            )
        if args.projects_command == "get":
            return client.request("GET", f"projects/{args.project_id}")
        result = client.request("GET", f"projects/{args.project_id}/export")
        if args.output:
            Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return {"written": args.output}
        return result
    if args.command == "project-config":
        if args.project_config_command == "pull":
            result = client.request("GET", f"projects/{args.project_id}/config")
            Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return {"written": args.output}
        document = json.loads(Path(args.file).read_text(encoding="utf-8"))
        return client.request(
            "PUT",
            f"projects/{args.project_id}/config",
            payload={"config": document},
        )
    if args.command == "deployments":
        if args.deployments_command == "list":
            return client.request(
                "GET",
                f"projects/{args.project_id}/deployments",
                query={"page": args.page},
            )
        if args.deployments_command == "create":
            return client.request(
                "POST",
                f"projects/{args.project_id}/deployments",
                payload={"branch": args.branch, "commit_sha": args.commit},
            )
        if args.deployments_command == "get":
            return client.request("GET", f"deployments/{args.deployment_id}")
        if args.deployments_command == "cancel":
            return client.request("POST", f"deployments/{args.deployment_id}/cancel")
        if args.deployments_command == "logs":
            return client.request(
                "GET",
                f"deployments/{args.deployment_id}/logs",
                query={"limit": args.limit, "keyword": args.keyword},
            )
        return client.request(
            "POST",
            f"projects/{args.project_id}/environments/{args.environment_id}/rollback",
        )
    if args.command == "webhooks":
        if args.webhooks_command == "list":
            return client.request("GET", "webhooks")
        if args.webhooks_command == "create":
            return client.request(
                "POST",
                "webhooks",
                payload={"name": args.name, "url": args.url, "events": args.event},
            )
        if args.webhooks_command == "delete":
            return client.request("DELETE", f"webhooks/{args.endpoint_id}")
        return client.request("POST", f"webhooks/{args.endpoint_id}/test")
    if args.command == "audit":
        return client.request("GET", "audit-events", query={"limit": args.limit})
    raise LayerRailClientError("Unknown command.")


def main() -> None:
    args = parser().parse_args()
    try:
        _print(run(args), compact=args.json)
    except (LayerRailClientError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"layerrail: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
