# Cubie release rollback

A rollback restarts services or Jobs and is therefore a deploy. Before any
release change, inspect every workspace freeze window, including its 20-minute
lead. Do not use a direct Cloud Run command to bypass the deploy guard.

## Before a rollout

Keep the source commit, image digest, control and workspace schema revisions,
workspace roster, schedule manifest and `scripts/platform/deploy.sh --env <private-env> --dry-run`
output in the release record. Confirm a clean tree and a known revision.
Production startup does not run Alembic.

## If a new service image fails

Stop the rollout and preserve the failing logs and `/health` response. Disable
the affected scheduled trigger only through an authorized, freeze-aware
operator procedure; do not start another Job during a freeze. Inspect whether
the previous image supports the current database schema. If so, prepare a
clean checkout of its release commit and review the platform deploy dry run
against the current control/workspace revisions. A prior image whose Alembic
tree does not know the live revision must be rejected, even if it previously
served traffic.

Run the guarded deploy only after the freeze windows clear. Verify `/health`
commit and every workspace schema revision, then test both a user route and
the affected Job/schedule. Record which image and manifest are now live.

## If migration fails or corrupts data

The migrate Job stops at the first failed workspace; later workspaces remain
untouched. Do not run ad hoc `alembic downgrade` on a live tenant or assume
that a previous image can read a newer schema. First identify the exact
control/workspace database, migration step and affected rows. Restore from a
verified, workspace-specific backup or write a reviewed forward repair. Run a
dry run and the migration preflight before any database change; keep the
affected workspace unavailable until its revision and row integrity are
verified. Other workspaces continue using their own databases.

## Workspace credential or provider incident

Disconnect or rotate the affected workspace credential in its admin settings.
An explicitly selected client AI provider fails closed if its credential is
revoked; it does not spend against Cubie's managed Vertex account. Review that
workspace's AI Cost events and the provider's actual invoice separately.

See [GO_LIVE.md](GO_LIVE.md) for the full rollout sequence and evidence list.
