# LayerRail product ledger

## Launch core — complete

- [x] GitHub App authentication, repository import, signed webhooks, and immutable commit deployments.
- [x] Vercel-grade framework, package manager, monorepo, static adapter, and Dockerfile detection.
- [x] Zero-config language runners and rootless isolated Dockerfile builds.
- [x] Environment, branch, immutable deployment, custom-domain, TLS, and redirect routing.
- [x] Newest-commit-wins webhook scheduling, deterministic jobs, cancel, redeploy, rollback, and queue recovery.
- [x] Project deployment policy: trigger enablement, branch globs, ignored authors, skip tokens, concurrency, and supersession.
- [x] Isolated dependency caching with atomic generation rotation and safe pruning.
- [x] Local SQLite and persistent volumes with environment scoping and retained-container safety.
- [x] Encrypted S3, R2, generic object storage, and Cloudinary media connections.
- [x] Live/retained logs, structured durable diagnostics, log copy/export, Prometheus resource monitoring, and outage-safe dashboards.
- [x] Authenticated remote deployment nodes with capacity, drain/delete safety, central routing, logs, metrics, and Dockerfile image transfer.
- [x] Team RBAC, invitations, scoped API tokens, redacted audit history, and baseline browser security headers.
- [x] Atomic Redis rate limiting for sign-in and REST/deployment automation.
- [x] Durable signed webhooks and deployment email notification preferences.
- [x] Versioned REST API and dependency-free installable `layerrail` CLI.
- [x] Project export and `.layerrail.json` config-as-code with a published JSON Schema.
- [x] LayerRail product identity, runtime variables, metrics, emails, control-plane UI, documentation, and compatibility aliases.
- [x] Reversible database migrations, upgrade metadata, CI validation, and release runbook.

## Ecosystem expansion — optional, not launch blockers

- [ ] GitLab, Bitbucket, and self-hosted Git provider adapters behind the existing source-provider seam.
- [ ] Pull-request preview comments and automatic preview cleanup.
- [ ] Managed cron, queue, and worker product primitives beyond application deployments.
- [ ] Java-specific first-party runner image (Dockerfile Java applications already work).
- [ ] Billing plans and hosted-control-plane metering.
- [ ] AI/MCP operational assistants after stable public API adoption.

These items expand provider reach or add new product categories. They are intentionally outside the completed deployment control-plane core.
