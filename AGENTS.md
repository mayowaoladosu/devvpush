# LayerRail agent guidelines

## Product and compatibility

- Public names, UI, emails, CLI, docs, runtime variables, and operator messages use **LayerRail**.
- Preserve internal `devpush.*` Docker labels, Compose/network names, deterministic queue IDs, migration history, legacy data paths, and compatibility environment/metric aliases unless a separately proven migration removes them.
- New production installs use `/opt/layerrail`, `/var/lib/layerrail`, the `layerrail` service user, and `layerrail.service`. Existing `/opt/devpush` installations remain supported.
- Use `LAYERRAIL_*` for new operator/runtime variables. Emit documented `DEVPUSH_*` aliases for existing applications.

## Deep modules

- All deployment triggers converge on `DeploymentService.schedule()`. Do not duplicate locking, deduplication, capacity, node placement, audit, or notification behavior in routers.
- Keep branch/author/message filtering and concurrency semantics behind `DeploymentPolicyService`.
- Keep `.layerrail.json` parsing and merging behind `ProjectConfigService`. Never accept secrets, unknown keys, unsupported versions, absolute paths, or traversal.
- Keep API token generation, digesting, expiry, revocation, scope validation, and last-use handling behind `ApiTokenService`. Raw `lr_*` values are shown once and never logged or audited.
- Route audit metadata through `AuditService`. Never persist authorization headers, cookies, environment values, credentials, private keys, API keys, webhook secrets, or storage secrets.
- Keep webhook destination policy, event selection, secret encryption, signing, retry state, and delivery metadata behind `NotificationService`. Never follow redirects or persist response bodies.
- Keep node enrollment, endpoint policy, scheduling, drain/delete safety, target generation, and origin validation behind `DeploymentNodeService`; keep remote Docker translation behind `NodeDockerClient`.
- Keep storage path, scope, conflict, and retained-container rules behind `StorageService`. Destructive actions fail closed when any runtime inventory cannot be verified.
- Keep Dockerfile archives and BuildKit operations behind `DockerfileBuilder`. Never use the host Docker build endpoint or expose GitHub/project secrets to build instructions.

## Python and FastAPI

- Use async SQLAlchemy and modern `X | None` type syntax.
- Business mutations belong in services; routers adapt HTTP/form/API input and output.
- Use `TemplateResponse`, `flash`, and existing dependencies for browser routes.
- REST routes use bearer tokens and explicit scopes; browser cookies must not authorize `/api/v1` mutations.
- Add Alembic migrations for every model change and prove fresh upgrade, downgrade/re-upgrade, and `alembic check` parity.
- Secret-bearing fields use Fernet properties or one-way digests; never plain JSON columns.
- Keep user-facing errors safe and specific. Detailed provider responses and secret values do not enter logs.

## Workers

- The jobs worker must not use source watching; restarting it can interrupt long builds.
- Lifecycle tasks use deterministic IDs and deployment heartbeats.
- Finalization/failure/cancel paths must be idempotent and preserve existing terminal conclusions.
- Notification tasks keep durable state and bounded retries.
- The independent monitor uses short-lived sessions and confirms queue state before recovering work.

## Docker and Compose

- The application stack is composed from `compose/base.yml` plus one environment override.
- The remote agent is deployed independently with `compose/node-agent.yml`.
- Do not mount the host Docker socket into app, workers, metrics, Prometheus, or BuildKit. Only the policy proxy and constrained node agent receive a local socket.
- Runtime containers drop capabilities and set no-new-privileges; Dockerfile images must declare a non-root user.
- Prometheus, metrics exporter, Redis, PostgreSQL, and Docker proxy have no public Traefik route.
- All shell entrypoints and scripts are LF-only.

## Scripts

- Source `scripts/lib.sh`; derive paths from `APP_DIR`, `DATA_DIR`, `LOG_DIR`, and `BACKUP_DIR`.
- Use `set -Eeuo pipefail`, repository error traps, `run_cmd`, `printf`, and atomic file replacement.
- New overrides use `LAYERRAIL_*`, with legacy aliases where documented.
- Never hardcode UID/GID 1000 in host scripts; use the detected service user and exported IDs.
- Sensitive flags are optional; prompt with `read -s` only on a TTY.
- Upgrade hooks are idempotent stable `X.Y.Z.sh` files. Declarative restart scope lives in matching JSON metadata.

## Git

- Work on `staging`; keep `main` stable.
- Use conventional commits.
- Never commit `data/`, `.env`, logs, backups, generated credentials, API tokens, node tokens, or private keys.
- Before pushing: fetch `origin/staging`, verify the base did not move, audit staged paths and secrets, then confirm local/tracking/remote SHAs match.

## Required validation

1. Full app unittest discovery.
2. CLI unittest discovery with `PYTHONPATH=cli`.
3. Focused Ruff fatal/import checks on new modules.
4. Parse every Jinja template and run `npm ci && npm run build`.
5. `uv lock --check`.
6. Existing database downgrade/re-upgrade and `alembic check`.
7. Fresh PostgreSQL migration to head and parity check.
8. Development, production, SSL, and node Compose rendering.
9. Production app/worker/metrics/node image builds.
10. Node-agent tests and production payload compile check.
11. BuildKit isolation E2E.
12. Browser smoke tests, REST/CLI E2E, deployment policy, rate limit, audit, email/webhook delivery, rollback, logs, metrics, and cleanup proofs.
13. Independent final diff review.
14. Secret scan, clean working tree, no temporary database/container/image/volume/process residue.
