# LayerRail

LayerRail is a self-hosted deployment control plane for building, releasing, and operating applications from GitHub. It combines Vercel-grade framework detection with secure Dockerfile builds, zero-downtime routing, durable logs, resource metrics, persistent data, and capacity-aware remote nodes.

> This repository is the LayerRail distribution derived from the open-source DevPush project. Internal `devpush.*` labels, network names, and compatibility variables remain where changing them would break existing installations.

## What ships in LayerRail 1.0

- **Git deployments** — manual, API, and signed GitHub webhook triggers with deterministic queue jobs, idempotent deliveries, newest-commit-wins scheduling, cancellation, redeploy, and instant rollback.
- **Vercel-grade detection** — 40+ framework presets, monorepo application selection, package-manager-aware commands, static/server adapters, and Dockerfile discovery.
- **Two secure build paths** — official zero-config runners with isolated dependency caches, or repository Dockerfiles built by rootless BuildKit behind filtered egress.
- **Multi-node runtime** — authenticated constrained node agents, encrypted enrollment tokens, health/capacity scheduling, drain safety, central Traefik routing, and federated logs and metrics. Raw remote Docker is never exposed.
- **Stateful applications** — environment-scoped SQLite and volume mounts, encrypted S3/R2-compatible object storage, and encrypted Cloudinary media connections.
- **Observability** — live and retained Loki logs, structured diagnostics in PostgreSQL, worker heartbeat recovery, and Prometheus CPU/memory/network/block-I/O/PID charts.
- **Team governance** — owner/admin/member roles, scoped and revocable API tokens, redacted audit history, deployment admission policy, request rate limits, email notifications, and signed outbound webhooks.
- **Automation** — versioned REST API, installable `layerrail` CLI, project export, and `.layerrail.json` config-as-code.
- **Domains** — immutable deployment URLs, environment and branch aliases, verified custom domains, TLS, and permanent/temporary redirects.

## Local quickstart

Requirements: Docker Desktop or Docker Engine with Compose v2, Git, and a GitHub App.

```bash
git clone https://github.com/mayowaoladosu/devvpush.git layerrail
cd layerrail
mkdir -p data
cp .env.dev.example data/.env
# Fill generated secrets and GitHub App credentials in data/.env
./scripts/start.sh
```

Open `http://localhost`. Health is available at `http://localhost/health`; the versioned API health endpoint is `http://localhost/api/v1/health`.

On Windows, run repository shell scripts through Git Bash. The development app and monitor reload automatically. The jobs worker deliberately does not watch source files because a reload can interrupt a build; restart it after job-code changes:

```bash
./scripts/restart.sh --components worker-jobs --no-migrate
```

## Production installation

LayerRail supports Ubuntu 20.04+ and Debian 11+ hosts. Start with [.env.example](.env.example), configure `APP_HOSTNAME`, `DEPLOY_DOMAIN`, email, GitHub App, PostgreSQL, and encryption values, then use the scripts in [scripts](scripts):

```bash
sudo ./scripts/install.sh --repo https://github.com/mayowaoladosu/devvpush.git --ref staging
sudo systemctl start layerrail.service
```

Create DNS records for the control plane and wildcard deployment domain. Use a DNS-01 certificate provider when wildcard certificates are required. Operational and product documentation lives at [docs.layerrail.com](https://docs.layerrail.com); service status is at [status.layerrail.com](https://status.layerrail.com).

## REST API and CLI

Create a scoped token under **Team settings → API tokens**. The token is displayed once and only a SHA-256 digest is stored.

```bash
python -m pip install ./cli
layerrail config set --url https://deploy.example.com --token lr_live_...
layerrail whoami
layerrail projects list
layerrail deployments create PROJECT_ID --branch main
layerrail deployments logs DEPLOYMENT_ID
```

Environment-only configuration is also supported:

```bash
export LAYERRAIL_URL=https://deploy.example.com
export LAYERRAIL_TOKEN=lr_live_...
```

Primary API resources:

- `GET/POST /api/v1/projects`
- `GET/PATCH /api/v1/projects/{project_id}`
- `GET/PUT /api/v1/projects/{project_id}/config`
- `GET /api/v1/projects/{project_id}/export`
- `GET/POST /api/v1/projects/{project_id}/deployments`
- `GET/POST /api/v1/deployments/{deployment_id}` and `/cancel`
- `GET /api/v1/deployments/{deployment_id}/logs`
- `POST /api/v1/projects/{project_id}/environments/{environment_id}/rollback`
- `GET /api/v1/audit-events`
- `GET/POST/DELETE /api/v1/webhooks`

OpenAPI documentation is available at `/docs` on an installation.

## Config as code

Place `.layerrail.json` in the repository. LayerRail reads it from the exact deployment commit before build decisions are made. The legacy `devpush.json` filename remains accepted during the compatibility window.

```json
{
  "$schema": "https://raw.githubusercontent.com/mayowaoladosu/devvpush/staging/schema/layerrail.schema.json",
  "version": "1",
  "build": {
    "strategy": "dockerfile",
    "rootDirectory": "apps/api",
    "dockerfile": "Dockerfile"
  },
  "resources": {
    "cpus": 2,
    "memoryMb": 2048
  },
  "deployment": {
    "webhookEnabled": true,
    "allowedBranches": ["main", "release/*"],
    "ignoredAuthors": ["dependabot[bot]"],
    "skipMessageTokens": ["[skip layerrail]"],
    "maxConcurrent": 1,
    "supersedeOlder": true
  }
}
```

Project environment secrets are intentionally excluded from this format and from project exports.

## Remote deployment nodes

Copy [.env.node.example](.env.node.example) to `data/node-agent.env` on a remote Docker host. Generate a unique token, set the runtime hostname and Docker socket GID, configure TLS, then start the constrained agent:

```bash
docker compose --env-file data/node-agent.env -f compose/node-agent.yml up -d --build
```

Enroll the endpoint from **Admin → Deployment nodes**. In production, the control endpoint must use HTTPS unless the operator explicitly opts into insecure endpoints. Private endpoints likewise require an explicit opt-in. Central Traefik must reach only the configured runtime port range. Local SQLite and volume attachments always pin a deployment to the primary node; object and media connections remain remotely eligible.

The agent runs non-root with a read-only filesystem, no capabilities, no-new-privileges, bounded resources, ownership-checked images and containers, a writable cache readiness gate, and an atomic hard capacity ceiling.

## Security model

- Fernet encryption for GitHub, storage, media, and node credentials.
- One-way API token digests; raw values are shown once.
- CSRF-protected browser forms and bearer-only REST authentication.
- Redis-backed atomic request limits for login and API surfaces.
- Redacted audit metadata; secret-like keys are never persisted.
- GitHub user-access and App-installation binding before API project creation.
- HMAC-SHA256 outbound webhook signatures with durable delivery state, outage reconciliation, and bounded retries.
- DNS-pinned public-address and HTTPS policy for production webhooks; public-address policy for custom storage endpoints.
- Same-origin browser content policy plus frame, MIME-sniffing, referrer, and feature restrictions.
- Restricted Docker socket proxy; host build, exec, and system-management endpoints are denied.
- Rootless BuildKit on an internal network with filtered public egress and no Docker socket.
- Non-root runtime containers with dropped capabilities, no-new-privileges, and PID/resource ceilings.
- Bound source archives, contexts, image exports, environment values, and runtime ports.

## Compatibility contracts

LayerRail exposes canonical `LAYERRAIL_*` runtime variables and `layerrail_*` Prometheus metrics. Legacy `DEVPUSH_*` variables and `devpush_*` metrics are emitted in parallel so existing applications and dashboards continue to work. Internal Compose project/network names and `devpush.*` Docker labels remain stable for safe upgrades.

Changing JWT defaults does not immediately sign users out: LayerRail accepts the legacy `devpush-app` issuer and `devpush-web` audience until operators remove them from `LEGACY_AUTH_TOKEN_ISSUERS` and `LEGACY_AUTH_TOKEN_AUDIENCES`.

## Validation

Core release validation includes:

```bash
# Python tests
docker compose exec -T app uv run --no-sync python -m unittest discover -s tests -v

# CLI tests
PYTHONPATH=cli python -m unittest discover -s cli/tests -v

# Schema parity
docker compose exec -T app uv run --no-sync alembic check

# Build isolation
./scripts/buildkit-isolation-e2e.sh
```

Development, production, and node Compose files must render successfully, and production app/worker/metrics/node images must build before release.

## Repository map

- [app](app) — FastAPI UI, REST API, models, migrations, workers, tests, and templates.
- [cli](cli) — dependency-free official Python CLI.
- [node_agent](node_agent) — constrained remote Docker lifecycle protocol.
- [compose](compose) — control-plane and standalone-node topology.
- [docker](docker) — production images, proxy policy, telemetry, and BuildKit configuration.
- [registry](registry) — versioned runner and framework catalog.
- [scripts](scripts) — install, update, backup, restore, migration, and isolation workflows.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the system model and [CONTRIBUTING.md](CONTRIBUTING.md) for repository conventions.

## License and attribution

LayerRail remains distributed under the [MIT License](LICENSE.md). It is derived from [hunvreus/devpush](https://github.com/hunvreus/devpush); upstream attribution and license notices are preserved.
