This folder ships a default registry catalog with /dev/push so installs can run without
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
- reports root or shallow nested Dockerfiles without silently using them.

The app-versioned catalog includes Node server and static frameworks, Python
frameworks, PHP, Go, Bun, and a plain static-site fallback. Definitions are data
driven; detector implementation handles package-manager commands and framework
refinements such as SvelteKit/Astro adapters and Angular output directories.

Dockerfiles are currently detection evidence only. Deployment still uses the
selected zero-config runner and editable commands.

Notes:

- Do not edit `catalog.json` directly on the server. Update it via the sync flow.
- Edit `overrides.json` to enable/disable entries or override specific fields.
- Keep reusable framework signatures in the app-versioned framework catalog,
  not instance overrides.
- `catalog.json` `meta.source` is `bundled` for the copy shipped with /dev/push and
  `registry` for catalogs fetched from the registry.
- Catalog format: see the registry repository README:
  https://github.com/devpushhq/registry/blob/main/README.md
