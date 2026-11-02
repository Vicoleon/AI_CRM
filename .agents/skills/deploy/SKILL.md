---
name: deploy
description: >-
  Prepare and run Cubie Google Cloud releases through the Phase 2 platform
  bootstrap and deploy scripts, with freeze, migration and workspace checks.
---

# Cubie platform deployment

Read `docs/GO_LIVE.md`, `docs/deployment_rollback.md`, and
`references/targets.md` before a release. Use this skill when Jose asks to
ship a Cubie release. The deployment target is chosen by the private
operator-owned environment file.

## Preflight

From a clean, known Git revision at the repository root, run:

```bash
bash .claude/skills/deploy/scripts/preflight.sh --env <private-env>
```

This wraps `scripts/platform/deploy.sh --env <private-env> --dry-run`. Review
its project, service, image, control database, workspace roster, migration
sequence, scheduled Jobs and freeze windows. Keep the environment file and
credentials out of Git and chat output. A dry run is not evidence that a live
release succeeded.

## Authorization and freeze

A real deploy changes shared cloud infrastructure and requires Jose's explicit
release approval for the specific target and revision. The platform deploy
script enforces every active workspace's weekly freeze window and the 20-minute
lead before it. Never set `DEPLOY_FREEZE_OVERRIDE_REASON` or bypass the guard
with raw `gcloud` commands. Only Jose may use manual break-glass.

## Release

```bash
bash scripts/platform/deploy.sh --env <private-env>
```

The deploy builds once, runs the migration Job against the existing control
schema and each registered workspace, then updates the service, Jobs and
schedules. It does not initialize a missing or corrupt control database. On a
genuinely new installation, use `scripts/platform/bootstrap_gcp.sh --env
<private-env> --dry-run --fresh-install`, review it, then run the same command
without `--dry-run` with separate approval. Workspace provisioning uses
`scripts/platform/workspace.sh` and an explicit client domain, admin and
service identity.

Capture the released Git revision and image digest. Confirm the service `/health`
identity and schema heads, then verify sign-in, data isolation, scheduled work
and per-workspace integrations. Follow `docs/deployment_rollback.md` for a
reversal; it is also a deploy and obeys the freeze windows.
