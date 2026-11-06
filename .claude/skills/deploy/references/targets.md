# Cubie Phase 2 deployment targets

The operator-owned `--env` file defines the Google Cloud project, region,
service, Cloud SQL instance, control database, artifact repository, bucket and
runtime service account. Use a distinct file and cloud resources for each
environment. Inspect the dry-run output before releasing.

| Operation | Command | Effect |
| --- | --- | --- |
| New resource dry run | `scripts/platform/bootstrap_gcp.sh --env <file> --phase resources --dry-run` | Validates target, explicit SQL cost settings and capacity; lists cloud resources |
| New resource bootstrap | `scripts/platform/bootstrap_gcp.sh --env <file> --phase resources` | Creates reviewed cloud resources and empty secret containers |
| New control dry run | `scripts/platform/bootstrap_gcp.sh --env <file> --phase control --fresh-install --dry-run` | Checks the existing SQL target, operator proxy, privileges, live capacity, ownership and empty control target |
| New control initialization | `scripts/platform/bootstrap_gcp.sh --env <file> --phase control --fresh-install` | Creates the control role/database, secret versions and initial control schema after SQL setup |
| Release dry run | `scripts/platform/deploy.sh --env <file> --dry-run` | Checks existing control schema, workspace roster, freeze windows and release operations |
| Release | `scripts/platform/deploy.sh --env <file>` | Builds once, migrates registered databases, updates service, Jobs and schedules |
| Workspace operations | `scripts/platform/workspace.sh --env <file> workspace ...` | Creates, updates, suspends or inspects an explicit workspace |

The normal release refuses a missing or corrupt control schema. Do not use
bootstrap as a repair path. See `docs/GO_LIVE.md` for OAuth, delegation, Slack,
BYOK and browser publication steps. See `docs/deployment_rollback.md` for
reversal and evidence collection.
