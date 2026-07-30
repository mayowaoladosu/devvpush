This folder ships a default registry catalog with LayerRail so installs can run without
network access to the remote registry.

What this is:

- `catalog.json`: a bundled catalog snapshot (runners + presets + metadata).
- `overrides.json`: a default overrides file (enables common runners/presets).
- `../app/services/framework_catalog.json`: app-versioned framework definitions
  layered onto any bundled or remotely synchronized runner catalog.

What happens on an instance:

- The installer copies these files into `DATA_DIR/registry/` if the target files
  do not already exist.
- The app reads `DATA_DIR/registry/catalog.json` + `DATA_DIR/registry/overrides.json`
  plus the app-versioned framework catalog and computes a resolved catalog
  (instance overrides win).

## Automatic framework detection

When a user selects a GitHub repository, the app fetches one recursive Git tree
and a bounded concurrent batch of relevant manifests. Detection then:

- evaluates likely application roots independently, including monorepos;
- matches exact package dependencies and file signatures by priority;
- detects npm, pnpm, Yarn, Bun, pip, Pipenv, and uv projects;
- derives runner, root directory, install/build/start commands, static output,
  confidence, evidence, and warnings;
- offers every deployable application found in a monorepo; and
- associates root and nested Dockerfiles with build contexts and recommends the
  native Dockerfile strategy while keeping zero-config alternatives editable.

The app-versioned catalog includes Node server and static frameworks, Python
frameworks, PHP, Go, Bun, and a plain static-site fallback. Definitions are data
driven; detector implementation handles package-manager commands and framework
refinements such as SvelteKit/Astro adapters and Angular output directories.

Dockerfile deployments use rootless BuildKit and the image's `CMD` or
`ENTRYPOINT`. The configured Dockerfile path is relative to the selected root
directory/build context. The image must declare a non-root `USER` and listen on
`0.0.0.0:8000`.

Notes:

- Do not edit `catalog.json` directly on the server. Update it via the sync flow.
- Edit `overrides.json` to enable/disable entries or override specific fields.
- Keep reusable framework signatures in the app-versioned framework catalog,
  not instance overrides.
- `catalog.json` `meta.source` is `bundled` for the copy shipped with LayerRail and
  `registry` for catalogs fetched from the registry.
- Catalog format: see the registry repository README:
  https://github.com/devpushhq/registry/blob/main/README.md

## Dependency-cache contract

Official runner images currently remain at `ghcr.io/devpushhq/runner-*` as an
upstream compatibility dependency and automatically participate in
dependency caching and configure their package managers beneath `/cache`.
Custom runners can opt in with `"cache_directory": "/cache"`; no other target is
accepted. An opted-in image must start as root, make `/cache` writable by the
configured `PUID`/`PGID`, and drop privileges before running repository code.
The cache is for package downloads/stores only—do not point it at `node_modules`,
a virtual environment, the repository checkout, or application output.
