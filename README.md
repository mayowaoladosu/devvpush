# /dev/push

An open-source and self-hostable alternative to Vercel, Render, Netlify and the likes. It allows you to build and deploy any app (Python, Node.js, PHP, ...) with zero-downtime updates, real-time logs, team management, customizable environments and domains, etc.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://devpu.sh/media/screenshot-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="https://devpu.sh/media/screenshot-light.png">
  <img alt="A screenshot of a deployment in /dev/push." src="https://devpu.sh/media/screenshot-dark.png">
</picture>

## Key features

- **Git-based deployments**: Push to deploy from GitHub with newest-commit-wins scheduling, zero-downtime rollouts, cancellation, and instant rollback.
- **Multi-language support**: Python, Node.js, PHP... basically anything that can run on Docker.
- **Native Dockerfiles**: Build repository Dockerfiles with cached, rootless BuildKit and run the resulting image command.
- **Fast redeploys**: Reuse isolated package-manager downloads across zero-config deployments and rotate caches without disrupting running releases.
- **Persistent data**: Attach environment-scoped SQLite databases and local volumes at validated application paths that survive deploys and rollbacks.
- **Object storage**: Connect AWS S3, Cloudflare R2, or another S3-compatible bucket with encrypted credentials and verified read/write/delete access.
- **Media delivery**: Connect Cloudinary with encrypted credentials, temporary upload/read/delete verification, regional API support, and environment-scoped runtime access.
- **Environment management**: Multiple environments with branch mapping and encrypted environment variables.
- **Real-time monitoring**: Live and searchable build and runtime logs.
- **Resource monitoring**: Authenticated Prometheus-backed CPU, memory, network, disk I/O, and process dashboards per deployment.
- **Durable failure diagnostics**: Database-backed worker heartbeats and watchdog recovery keep crashes, timeouts, queue loss, and Loki outages visible to users.
- **Team collaboration**: Role-based access control with team invitations and permissions.
- **Custom domains**: Support for custom domain and automatic Let's Encrypt SSL certificates.
- **Self-hosted and open source**: Run on your own servers, MIT licensed.

## Documentation

See [devpu.sh/docs](https://devpu.sh/docs) for installation, configuration, and usage. For technical details, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Prerequisites

- **Server**: Ubuntu 20.04+ or Debian 11+ with SSH access and sudo privileges. A [Hetzner CPX31](https://devpu.sh/docs/guides/create-hetzner-server) works well.
- **DNS**: We recommend [Cloudflare](https://cloudflare.com).
- **GitHub account**: You'll create a GitHub App for login and repository access.
- **Email provider**: A [Resend](https://resend.com) account or SMTP credentials for login emails and invitations.

## Quickstart

> ⚠️ Supported on Ubuntu/Debian. Other distros may work but aren't officially supported (yet).

1. **Install** on a fresh server:

```bash
curl -fsSL https://install.devpu.sh | sudo bash
```

2. **Create a GitHub App** at [devpu.sh/docs/guides/create-github-app](https://devpu.sh/docs/guides/create-github-app)

3. **Configure** by editing `/var/lib/devpush/.env` with: `APP_HOSTNAME`, `DEPLOY_DOMAIN`, `LE_EMAIL`, `EMAIL_SENDER_ADDRESS`, `RESEND_API_KEY` (or SMTP settings), and your GitHub App credentials.

4. **Set DNS**:
   - `A` `example.com` → server IP (app hostname)
   - `A` `*.example.com` → server IP (deployments)

5. **Start** the service:

```bash
sudo systemctl start devpush.service
```

For more information, including manual installation or updates, refer to [the documentation](https://devpu.sh/docs/installation).

## Development

**Prerequisites**: Docker and Docker Compose v2+. On macOS, [Colima](https://github.com/abiosoft/colima) works well as an alternative to Docker Desktop.

```bash
git clone https://github.com/hunvreus/devpush.git
cd devpush
mkdir -p data
cp .env.dev.example data/.env
# Edit data/.env with your GitHub App credentials
```

Start the stack:

```bash
./scripts/start.sh
```

The stack auto-detects development mode on macOS and enables hot reloading. Data is stored in `./data/`.
The web app and monitor reload during development. The jobs worker deliberately
does not hot-reload because restarting it can interrupt builds; after changing
job code, run `./scripts/restart.sh --components worker-jobs --no-migrate`.

## Registry catalog

Default runner/preset definitions ship in `registry/` and are copied to `DATA_DIR/registry/` during install/update.
See `registry/README.md` for the catalog format and override rules.

Project import automatically detects framework applications and monorepo roots
from GitHub. It understands 40+ presets, package managers and lockfiles, static
outputs, framework adapters, and Dockerfile evidence, then fills editable build
and start settings before deployment. A detected Dockerfile becomes an editable
native-build recommendation; zero-config runners remain available as an explicit
alternative.

Dockerfile source is downloaded at an immutable commit, extracted with traversal
and size checks, and sent to a dedicated rootless BuildKit daemon. BuildKit has no
Docker socket or control-plane network membership. Public dependency traffic is
forced through a capability-dropped proxy that denies loopback, link-local,
cloud-metadata, carrier-grade NAT, and private destinations. Project environment
variables are runtime-only for Dockerfile projects and are never sent as build
arguments or build secrets. The resulting per-deployment image must define `CMD`
or `ENTRYPOINT`, declare a non-root `USER`, and listen on `0.0.0.0:8000`.

Webhook deployments are serialized per project environment. GitHub delivery IDs
make redelivery idempotent, while each newer push marks older
queued or starting webhook deployments as skipped, aborts their jobs with a
bounded wait, and cleans up their containers and managed images. Manual deploys
are deliberate and are never superseded. Finalizers use the same environment
lock so an older commit cannot reclaim an alias after a newer deployment wins.
If any project mapped to a repository cannot schedule, the webhook returns a
retryable failure; projects already scheduled by that delivery are reused.

Official zero-config runners place npm, pnpm, Yarn, Bun, pip, uv, Composer, and
other package-manager caches beneath `/cache`. DevPush binds that path to a
generation-scoped directory isolated by project, environment, and runner image.
Only dependency downloads are retained—checkout-specific `node_modules`, virtual
environments, and build outputs remain ephemeral. Clearing the cache rotates the
generation immediately for future deployments; generations still mounted by a
current or rollback container are preserved until those containers are removed.
Dockerfile projects continue to use BuildKit's bounded persistent layer cache.

Deployment lifecycle jobs write a short database heartbeat independently from
their build or Docker work. The monitor reconciles non-terminal deployments with
ARQ state; a missing or expired worker lease is confirmed before recovery, so
long Dockerfile builds remain valid while hard worker crashes become terminal
failures with a structured code, source, attempt, hint, and timestamp. Small
control-plane diagnostics are stored in PostgreSQL and merged into the existing
logs UI, so useful failure context remains available when Loki is offline.

Resource metrics use a separate internal-only exporter. It reads only labeled
deployment containers through the restricted Docker proxy and publishes
cumulative CPU, network, and block-I/O counters plus memory and process gauges.
An internal Prometheus instance scrapes every five seconds with bounded time and
size retention. Neither Prometheus nor the exporter exposes a host port; users
query history through the authenticated project Monitoring page.

Persistent storage is owned by a team and connected to projects for all or
selected environments. Connections may use the generated `/data/...` path or a
validated custom container directory such as `/app/data`. The same host-backed
resource is attached to zero-config and Dockerfile releases, so data remains
available across redeploys and rollback containers. Mount paths cannot overlap
within an environment, cannot target system/platform directories, and never
accept arbitrary host paths. Reset and deletion fail closed while any current,
rollback, stopped, or otherwise retained deployment container still references
the resource. Lifecycle transitions use deterministic jobs, and the independent
monitor recovers state committed immediately before an enqueue/process crash.
Local storage capacity is managed by the host filesystem; use
operator disk quotas where hard tenant limits are required.

Object storage connections are team-owned and can be attached to selected
project environments. Access keys, secret keys, and optional session tokens are
Fernet-encrypted in PostgreSQL. Provisioning performs a bounded `HEAD`, writes a
unique temporary object, reads it back, and deletes it; DevPush never creates or
deletes the bucket itself. Production custom endpoints require HTTPS and must
resolve only to public addresses. Each connection receives a stable namespace,
for example `DEVPUSH_OBJECT_ASSETS_BUCKET` and
`DEVPUSH_OBJECT_ASSETS_SECRET_ACCESS_KEY`. When exactly one object connection is
active, conventional `AWS_*`/`S3_*` aliases are also supplied unless the project
explicitly defines them. Credentials are added only when the runtime container
is created and are never sent to rootless BuildKit or Dockerfile instructions.
Rotated credentials apply to future deployments; retained releases keep their
original runtime snapshot until replaced. Production custom hosts require an
operator-approved DNS suffix. Private-network MinIO and HTTP endpoints require
additional explicit operator opt-ins; they are denied by default.

Cloudinary media connections are separate from S3-compatible object storage.
DevPush verifies each connection by uploading a tiny temporary image, reading
its metadata through the Admin API, and deleting it. Cloud name, regional API
selection, and an optional default folder remain non-secret; API keys and
secrets are Fernet-encrypted. Runtime variables use a stable
`DEVPUSH_MEDIA_<NAME>_*` namespace. When exactly one media connection is active,
DevPush also supplies `CLOUDINARY_URL` and conventional `CLOUDINARY_*` aliases.
All credentials are added only when the runtime container is created, never to
BuildKit. Rotation affects future deployments, and disconnecting a connection
never deletes customer assets or transformations. Production always uses the
official US, EU, or Asia Pacific Cloudinary API endpoint.

**Key scripts**:

- `./scripts/start.sh` / `stop.sh` / `restart.sh` — manage the full stack or selected components (`--components <csv>`)
- `./scripts/compose.sh logs -f app` — view logs
- `./scripts/buildkit-isolation-e2e.sh` — verify BuildKit and Docker API isolation
- `./scripts/db-generate.sh` — create database migration
- `./scripts/clean.sh` — remove all Docker resources and data
- `./scripts/update.sh` — update by ref (defaults to `app` only; use `--all` / `--components` / `--full` to expand scope)

See [ARCHITECTURE.md](ARCHITECTURE.md) for codebase structure.

## Scripts

| Script                     | What it does                                                                                                                                                                      |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scripts/backup.sh`        | Create backup of data directory, database, and code metadata (`--output <file>`, `--verbose`)                                                                                     |
| `scripts/buildkit-isolation-e2e.sh` | Verify the rootless BuildKit socket, network, Docker proxy policy, and build-step control-plane isolation                                                           |
| `scripts/clean.sh`         | Stop stack and remove all Docker resources and data (`--keep-docker`, `--keep-data`, `--yes`)                                                                                     |
| `scripts/compose.sh`       | Docker compose wrapper with correct files/env (`--`)                                                                                                                              |
| `scripts/db-generate.sh`   | Generate Alembic migration (prompts for message)                                                                                                                                  |
| `scripts/db-migrate.sh`    | Apply Alembic migrations (`--timeout <sec>`)                                                                                                                                      |
| `scripts/install.sh`       | Server setup: Docker, user, clone repo, .env, systemd (`--repo <url>`, `--ref <ref>`, `--yes`, `--no-telemetry`, `--verbose`)                                                     |
| `scripts/restart.sh`       | Restart services (`--components <csv>`, `--no-migrate`)                                                                                                                            |
| `scripts/restore.sh`       | Restore from backup archive (`--archive <file>`, `--no-db`, `--no-data`, `--no-code`, `--no-restart`, `--no-backup`, `--remove-runners`, `--timeout <sec>`, `--yes`, `--verbose`) |
| `scripts/start.sh`         | Start stack (`--components <csv>`, `--no-migrate`, `--timeout <sec>`, `--verbose`)                                                                                                 |
| `scripts/status.sh`        | Show stack status                                                                                                                                                                 |
| `scripts/stop.sh`          | Stop services (`--components <csv>`, `--hard`)                                                                                                                                     |
| `scripts/uninstall.sh`     | Uninstall from server (`--yes`, `--skip-backup`, `--no-telemetry`, `--verbose`)                                                                                                   |
| `scripts/update.sh`        | Update by tag (default updates `app` only; use `--all`, `--full`, or `--components <csv>` to expand scope) (`--ref <tag>`, `--all`, `--full`, `--components <csv>`, `--no-migrate`, `--no-telemetry`, `--yes`, `--verbose`) |

## Environment variables

| Variable                            | Description                                                                                                                              |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `SECRET_KEY`                        | App secret for sessions/CSRF. Auto-generated by install.sh.                                                                              |
| `ENCRYPTION_KEY`                    | Fernet key for encrypting secrets. Auto-generated by install.sh.                                                                         |
| `POSTGRES_PASSWORD`                 | PostgreSQL password. Auto-generated by install.sh.                                                                                       |
| `SERVICE_UID`                       | Container user UID. Auto-set to match host user.                                                                                         |
| `SERVICE_GID`                       | Container user GID. Auto-set to match host user.                                                                                         |
| `SERVER_IP`                         | Public IP of the server. Auto-detected by install.sh.                                                                                    |
| `CERT_CHALLENGE_PROVIDER`           | ACME challenge provider: `default` (HTTP-01) or `cloudflare`, `route53`, `gcloud`, `digitalocean`, `azure` (DNS-01). Default: `default`. |
| `GITHUB_APP_ID`                     | GitHub App ID.                                                                                                                           |
| `GITHUB_APP_NAME`                   | GitHub App name.                                                                                                                         |
| `GITHUB_APP_PRIVATE_KEY`            | GitHub App private key (PEM format, use `\n` for newlines).                                                                              |
| `GITHUB_APP_WEBHOOK_SECRET`         | GitHub webhook secret.                                                                                                                   |
| `GITHUB_APP_CLIENT_ID`              | GitHub OAuth client ID.                                                                                                                  |
| `GITHUB_APP_CLIENT_SECRET`          | GitHub OAuth client secret.                                                                                                              |
| `APP_HOSTNAME`                      | Domain for the app (e.g., `example.com`).                                                                                                |
| `DEPLOY_DOMAIN`                     | Domain for deployments (wildcard root). No default—set explicitly (e.g., `deploy.example.com`).                                          |
| `LE_EMAIL`                          | Email for Let's Encrypt notifications.                                                                                                   |
| `EMAIL_SENDER_ADDRESS`              | Email sender for invites/login.                                                                                                          |
| `RESEND_API_KEY`                    | API key for [Resend](https://resend.com). Optional if SMTP is configured.                                                               |
| `SMTP_HOST`                         | SMTP host. When set with username/password, SMTP is used instead of Resend.                                                             |
| `SMTP_PORT`                         | SMTP port. Default: `587`.                                                                                                              |
| `SMTP_USERNAME`                     | SMTP username. Required when using SMTP.                                                                                                |
| `SMTP_PASSWORD`                     | SMTP password. Required when using SMTP.                                                                                                |
| `GOOGLE_CLIENT_ID`                  | Google OAuth client ID (optional).                                                                                                       |
| `GOOGLE_CLIENT_SECRET`              | Google OAuth client secret (optional).                                                                                                   |
| `APP_NAME`                          | Display name. Default: `/dev/push`.                                                                                                      |
| `APP_DESCRIPTION`                   | App description.                                                                                                                         |
| `EMAIL_SENDER_NAME`                 | Sender display name. Default: `/dev/push`.                                                                                               |
| `POSTGRES_DB`                       | Database name. Default: `devpush`.                                                                                                       |
| `POSTGRES_USER`                     | Database user. Default: `devpush-app`.                                                                                                   |
| `REDIS_URL`                         | Redis URL. Default: `redis://redis:6379`.                                                                                                |
| `DOCKER_HOST`                       | Docker API. Default: `tcp://docker-proxy:2375`.                                                                                          |
| `PROMETHEUS_QUERY_TIMEOUT_SECONDS`  | Maximum wait for an authenticated dashboard query. Default: `8`.                                                                        |
| `PROMETHEUS_RETENTION_TIME`         | Prometheus duration for time-based retention (for example `168h` or `7d`). Default: `7d`.                                                |
| `PROMETHEUS_RETENTION_SIZE`         | Prometheus byte-size retention ceiling (for example `512MB` or `2GB`). Default: `2GB`.                                                   |
| `PROMETHEUS_MEMORY_LIMIT`           | Prometheus container memory limit. Default: `384m`.                                                                                      |
| `PROMETHEUS_CPUS`                   | Prometheus container CPU limit. Default: `0.75`.                                                                                         |
| `OBJECT_STORAGE_ALLOW_PRIVATE_ENDPOINTS` | Permit operator-trusted private DNS/IP targets for S3-compatible endpoints. Default: `false`.                                      |
| `OBJECT_STORAGE_ALLOW_INSECURE_ENDPOINTS` | Permit HTTP rather than HTTPS for operator-trusted S3-compatible endpoints. Default: `false`.                                      |
| `OBJECT_STORAGE_ALLOWED_ENDPOINT_SUFFIXES` | Comma-separated trusted DNS suffixes permitted for custom S3-compatible endpoints in production. Default: empty.                  |
| `CLOUDINARY_API_BASE_URL`            | Development-only Cloudinary-compatible test endpoint override. Production always uses the selected official regional endpoint.             |
| `BUILDKIT_HOST`                     | Rootless BuildKit socket. Default: `unix:///run/buildkit/buildkitd.sock`.                                                               |
| `BUILDKIT_INTERNAL_SUBNET`          | Private internal build network. Default: `10.250.0.0/24`.                                                                                |
| `BUILDKIT_PROXY_IP`                 | Filtered-egress proxy address inside that subnet. Default: `10.250.0.2`.                                                                 |
| `BUILDKIT_MEMORY_LIMIT`             | Compose memory limit for the BuildKit daemon. Default: `4g`.                                                                            |
| `BUILDKIT_CPUS`                     | Compose CPU limit for the BuildKit daemon. Default: `4.0`.                                                                               |
| `BUILDKIT_PIDS_LIMIT`               | Compose PID limit for the BuildKit daemon. Default: `2048`.                                                                              |
| `BUILDKIT_CACHE_GC_STORAGE`         | Cache GC reserved, free-space target, and maximum storage in MB. Default: `2048,10240,20480`.                                            |
| `DATA_DIR`                          | Data directory. Default: `/var/lib/devpush`.                                                                                             |
| `APP_DIR`                           | Code directory. Default: `/opt/devpush`.                                                                                                 |
| `DEFAULT_CPUS`                      | Default CPU limit per deployment. No limit if not provided.                                                                              |
| `MAX_CPUS`                          | Maximum allowed CPU override per project. Used only when `DEFAULT_CPUS` is set. Required to let user customize CPU.                      |
| `DEFAULT_MEMORY_MB`                 | Default memory limit (MB) per deployment. No limit if not provided.                                                                      |
| `MAX_MEMORY_MB`                     | Maximum allowed memory override per project. Used only when `DEFAULT_MEMORY_MB` is set. Required to let user customize memory.           |
| `RUNTIME_PIDS_LIMIT`                | Maximum processes per deployment container. Default: `512`.                                                                              |
| `JOB_TIMEOUT_SECONDS`               | Job timeout (seconds). Default: `320`.                                                                                                   |
| `JOB_MAX_TRIES`                     | Max retries per background job. Default: `3`.                                                                                            |
| `DEPLOYMENT_TIMEOUT_SECONDS`        | Deployment timeout (seconds). Default: `300`.                                                                                            |
| `DEPLOYMENT_SCHEDULE_LOCK_SECONDS`  | Lease duration for serializing scheduling and alias promotion per environment. Default: `120`.                                          |
| `DEPLOYMENT_SCHEDULE_WAIT_SECONDS`  | Maximum wait to acquire an environment scheduling lock. Default: `15`.                                                                   |
| `DEPLOYMENT_ABORT_TIMEOUT_SECONDS`  | Maximum wait for a superseded or canceled job to acknowledge abort. Default: `5`.                                                       |
| `DEPLOYMENT_WORKER_HEARTBEAT_SECONDS` | Interval between durable lifecycle worker heartbeats. Default: `5`.                                                                    |
| `DEPLOYMENT_ORPHAN_TIMEOUT_SECONDS` | Time without a heartbeat before a running lifecycle job becomes suspicious. Default: `20`.                                             |
| `DEPLOYMENT_ORPHAN_CONFIRM_SECONDS` | Additional confirmation window before watchdog recovery. Default: `10`.                                                                |
| `DEPLOYMENT_QUEUE_GRACE_SECONDS`    | Grace period for an unclaimed lifecycle job after the jobs-worker health key expires. Default: `90`.                                   |
| `DEPLOYMENT_RECONCILE_INTERVAL_SECONDS` | Interval between independent lifecycle reconciliation scans. Default: `5`.                                                         |
| `DOCKERFILE_BUILD_TIMEOUT_SECONDS`  | Maximum Dockerfile build duration. Default: `900`.                                                                                       |
| `DOCKERFILE_IMAGE_LOAD_TIMEOUT_SECONDS` | Maximum idle time for loading a built image into Docker. Default: `300`.                                                            |
| `DOCKERFILE_BUILD_MAX_CONCURRENCY`  | Maximum concurrent Dockerfile builds per jobs worker. Default: `2`.                                                                     |
| `DOCKERFILE_MAX_ARCHIVE_BYTES`      | Maximum compressed GitHub source archive size. Default: `268435456`.                                                                    |
| `DOCKERFILE_MAX_CONTEXT_BYTES`      | Maximum extracted Docker build context size. Default: `1073741824`.                                                                     |
| `DOCKERFILE_MAX_CONTEXT_FILES`      | Maximum files in a Docker build context. Default: `100000`.                                                                              |
| `DOCKERFILE_MAX_IMAGE_BYTES`        | Hard maximum exported image archive size. Default: `2147483648`.                                                                         |
| `CONTAINER_DELETE_GRACE_SECONDS`    | Wait before deleting containers after stop/failure to let logs ship. Default: `3`.                                                       |
| `LOG_STREAM_GRACE_SECONDS`          | Grace window for deployment log streaming (when to connect/close SSE around terminal states). Default: `5`.                              |
| `LOG_LEVEL`                         | Logging level. Default: `WARNING`.                                                                                                       |
| `MAGIC_LINK_TTL_SECONDS`            | Magic link validity (seconds). Default: `900`.                                                                                           |
| `AUTH_TOKEN_TTL_DAYS`               | Auth cookie/JWT lifetime (days). Default: `30`.                                                                                          |
| `AUTH_TOKEN_REFRESH_THRESHOLD_DAYS` | Refresh auth token when expiring within N days. Default: `1`.                                                                            |
| `AUTH_TOKEN_ISSUER`                 | JWT issuer for auth_token. Default: `devpush-app`.                                                                                       |
| `AUTH_TOKEN_AUDIENCE`               | JWT audience for auth_token. Default: `devpush-web`.                                                                                     |

## Support the project

- [Contribute code](/CONTRIBUTING.md)
- [Report issues](https://github.com/hunvreus/devpush/issues)
- [Sponsor me](https://github.com/sponsors/hunvreus)
- [Star the project on GitHub](https://github.com/hunvreus/devpush)
- [Join the Discord chat](https://devpu.sh/chat)

## License

[MIT](/LICENSE.md)
