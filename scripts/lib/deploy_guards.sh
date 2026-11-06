#!/bin/bash
# Compatibility decision functions and freeze guard. The live release entry
# point is scripts/platform/deploy.sh, backed by rubik_core.platform.deployment.
#
# Why this exists: a checkout months behind main — its Alembic head far older
# than the live database's, missing whole packages — can be deployed to the
# service again and again, with deploy.sh printing "Deployment Complete!" every
# time. It is not an outage only if traffic happens to be pinned to an older
# revision, so the new revisions sit at 0% and never boot against the real
# database. The same pin then silently swallows a legitimate deploy from main.
#
# Three things can be true at once, and without these guards none of them stops
# the deploy:
#   1. the working tree is behind origin/main           -> guard_tree_is_main
#   2. the image's Alembic head predates the live DB    -> guard_tree_knows_live_db
#   3. the new revision holds 0% of traffic             -> guard_new_revision_is_serving
#
# Contract, matching scripts/lib/migration_preflight.sh: a guard that finds the
# problem it exists for STOPS with its own exit code; a guard that cannot RUN
# (no network, no service yet, no python) WARNS and continues — a diagnostic
# must never block a deploy for its own bugs. deploy.sh already uses exit 3
# for "SERVICE DEPLOYED, JOBS STALE", so these start at 4.
#
#   4  tree is dirty or is not origin/main         DEPLOY_ALLOW_UNMERGED=1
#   5  tree does not know the live DB revision     DEPLOY_ALLOW_BEHIND_DB=1
#   6  new revision is not serving traffic         DEPLOY_ALLOW_PINNED_TRAFFIC=1
#   7  inside the declared deploy freeze           DEPLOY_FREEZE_OVERRIDE_REASON="…"
#                                                  (a person types it by hand; Claude never sets it)
#
# The pure decision functions (_tree_knows_revision, _traffic_serving_latest,
# _parse_freeze, _in_freeze_window) take their inputs as arguments so
# backend/tests/test_deploy_guards.py can exercise them without gcloud, curl or
# a network.

# ── 0. Never deploy inside the declared deploy freeze ───────────────────────
#
# A deploy restarts the service, so a team that runs a meeting in the app (an
# L10, say) can declare a weekly window in which no deploy may start:
#
#   CUBIE_DEPLOY_FREEZE="TUE 08:00-12:00 America/Costa_Rica"
#
# i.e. "<DOW> <HH:MM>-<HH:MM> <IANA time zone>", the day as MON…SUN. Empty or
# unset means no freeze. A deploy takes ~10-15 minutes to roll out, so one that
# would still be rolling out when the window opens is refused too
# (DEPLOY_FREEZE_LEAD_MIN before it). Phase 2 moves the source of the window to
# the control database (each workspace's own optional freeze window).
#
# Unlike guards 4-6 this one does not fail open on its own errors: a window it
# cannot read (a typo in the value, an unknown time zone) or a clock it cannot
# read refuses the deploy. A declared freeze is a promise to the people in the
# meeting, and "could not tell" is not a reason to break it.
DEPLOY_FREEZE_LEAD_MIN=20     # a deploy started 20 min before still lands inside
_FREEZE_WEEK_MIN=$((7 * 24 * 60))

# Pure: parse "<DOW> <HH:MM>-<HH:MM> <TZ>" and print
# "<iso weekday> <start minute> <end minute> <TZ>". Exit 1 when malformed.
_parse_freeze() {
    local spec="$1" day span tz extra start end dow sh sm eh em
    read -r day span tz extra <<<"$spec"
    [ -n "$day" ] && [ -n "$span" ] && [ -n "$tz" ] && [ -z "$extra" ] || return 1
    case "$(printf '%s' "$day" | tr '[:lower:]' '[:upper:]')" in
        MON) dow=1 ;; TUE) dow=2 ;; WED) dow=3 ;; THU) dow=4 ;;
        FRI) dow=5 ;; SAT) dow=6 ;; SUN) dow=7 ;;
        *) return 1 ;;
    esac
    [[ "$span" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])$ ]] || return 1
    sh="${BASH_REMATCH[1]}" sm="${BASH_REMATCH[2]}" eh="${BASH_REMATCH[3]}" em="${BASH_REMATCH[4]}"
    start=$((10#$sh * 60 + 10#$sm))
    end=$((10#$eh * 60 + 10#$em))
    [ "$start" -lt "$end" ] || return 1
    # An unknown zone makes `date` silently use UTC, which would move the
    # window by hours: only a zone the system has a file for is accepted.
    [[ "$tz" =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$ ]] || return 1
    [ -f "${TZDIR:-/usr/share/zoneinfo}/$tz" ] || return 1
    echo "$dow $start $end $tz"
}

# Pure: would a deploy started at <iso weekday> <minute of day>, on the freeze's
# own clock, land inside the window <freeze weekday> <start> <end> (or in the
# lead before it)? Exit 0 = yes (refuse), 1 = no. Counted in minutes of the
# week, so a lead that reaches back past midnight (or past Monday 00:00) is
# still caught.
_in_freeze_window() {
    local dow="$1" minutes="$2" fdow="$3" start="$4" end="$5" now opens span
    now=$(((dow - 1) * 1440 + minutes))
    opens=$(((fdow - 1) * 1440 + start - DEPLOY_FREEZE_LEAD_MIN))
    span=$((end - start + DEPLOY_FREEZE_LEAD_MIN))
    [ $(( ((now - opens) % _FREEZE_WEEK_MIN + _FREEZE_WEEK_MIN) % _FREEZE_WEEK_MIN )) -lt "$span" ]
}

guard_not_during_freeze() {
    # Every entry point checks independently. A caller-controlled parent marker
    # must never bypass this window.
    local spec="${CUBIE_DEPLOY_FREEZE:-}"
    if [ -z "${spec// /}" ]; then
        echo "✓ No deploy freeze declared (CUBIE_DEPLOY_FREEZE is empty)."
        return 0
    fi
    if [ -n "${DEPLOY_FREEZE_OVERRIDE_REASON:-}" ]; then
        echo "⚠️  DEPLOY_FREEZE_OVERRIDE_REASON set — deploying INSIDE the deploy freeze (${spec})."
        echo "   Reason given: ${DEPLOY_FREEZE_OVERRIDE_REASON}"
        return 0
    fi
    local parsed fdow start end tz now dow hh mm minutes
    if ! parsed="$(_parse_freeze "$spec")"; then
        echo "❌ Deploy refused: CUBIE_DEPLOY_FREEZE=\"${spec}\" is not a window this guard can read." >&2
        echo "   Expected \"<DOW> <HH:MM>-<HH:MM> <time zone>\", e.g. \"TUE 08:00-12:00 America/Costa_Rica\"" >&2
        echo "   (MON…SUN, start before end, a zone from the tz database), or empty for no freeze." >&2
        exit 7
    fi
    read -r fdow start end tz <<<"$parsed"
    if ! now="$(TZ="$tz" date +'%u %H %M' 2>/dev/null)" || [ -z "$now" ]; then
        echo "❌ Deploy refused: could not read the clock to check the deploy freeze (${spec})." >&2
        exit 7
    fi
    read -r dow hh mm <<<"$now"
    minutes=$((10#$hh * 60 + 10#$mm))
    if _in_freeze_window "$dow" "$minutes" "$fdow" "$start" "$end"; then
        echo "" >&2
        echo "❌ Deploy refused: inside the deploy freeze (CUBIE_DEPLOY_FREEZE=\"${spec}\")," >&2
        echo "   or in the ${DEPLOY_FREEZE_LEAD_MIN} minutes before it. It is ${hh}:${mm} ${tz} now (ISO weekday ${dow})." >&2
        echo "   Merge the PR now if you like; deploy once the window has closed." >&2
        echo "" >&2
        echo "   Break-glass (the service is down for the people in the meeting, and a person" >&2
        echo "   says so by hand):" >&2
        echo "       DEPLOY_FREEZE_OVERRIDE_REASON=\"<what is broken>\" bash scripts/platform/deploy.sh --env <file>" >&2
        exit 7
    fi
    echo "✓ Outside the deploy freeze (${hh}:${mm} ${tz}, ISO weekday ${dow}; window ${spec})."
    return 0
}

_dg_repo_root() {
    git rev-parse --show-toplevel 2>/dev/null || pwd
}

# ── 1. The tree must be clean and must BE origin/main ────────────────────────
#
# `gcloud run deploy --source .` uploads the working directory as-is — not a
# git ref — so whatever sits in the tree ships. A clean tree whose content
# equals origin/main is a precondition, not a nicety. Content is compared with
# `git diff`, not by SHA, so a detached HEAD checked out AT origin/main passes.
guard_tree_is_main() {
    if [ "${DEPLOY_ALLOW_UNMERGED:-}" = "1" ]; then
        echo "⚠️  DEPLOY_ALLOW_UNMERGED=1 — deploying a tree that may not be origin/main."
        echo "   The live service will be ahead of (or off) main until this lands."
        return 0
    fi
    local root; root="$(_dg_repo_root)"
    if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "⚠️  Not a git checkout — cannot verify the tree is origin/main. Continuing."
        return 0
    fi

    # Best-effort refresh; an offline laptop still compares against the last
    # fetched origin/main rather than skipping the check.
    git -C "$root" fetch --quiet origin main 2>/dev/null || \
        echo "⚠️  Could not fetch origin/main — comparing against the last fetched copy."

    if ! git -C "$root" rev-parse --verify --quiet origin/main >/dev/null; then
        echo "⚠️  No origin/main ref in this checkout — cannot verify. Continuing."
        return 0
    fi

    local dirty; dirty="$(git -C "$root" status --porcelain 2>/dev/null)"
    if [ -n "$dirty" ]; then
        echo "" >&2
        echo "❌ Deploy refused: the working tree is DIRTY, and --source . uploads it as-is." >&2
        echo "$dirty" | head -15 >&2
        echo "" >&2
        echo "   Land the change through a PR and deploy main, or remove stray files." >&2
        echo "   Override (dev only, on purpose): DEPLOY_ALLOW_UNMERGED=1" >&2
        exit 4
    fi

    if ! git -C "$root" diff --quiet HEAD origin/main 2>/dev/null; then
        local behind ahead
        behind="$(git -C "$root" rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
        ahead="$(git -C "$root" rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
        echo "" >&2
        echo "❌ Deploy refused: this tree is NOT origin/main." >&2
        echo "   HEAD $(git -C "$root" rev-parse --short HEAD) vs origin/main $(git -C "$root" rev-parse --short origin/main)" >&2
        echo "   behind by ${behind} commit(s), ahead by ${ahead}." >&2
        echo "" >&2
        echo "   A tree behind main ships old code; a tree ahead of main makes the live" >&2
        echo "   service outrun what was reviewed. Align it first:" >&2
        echo "       fetch origin/main, inspect the difference, then merge or rebase the reviewed branch" >&2
        echo "   Override (dev only, on purpose): DEPLOY_ALLOW_UNMERGED=1" >&2
        exit 4
    fi
    echo "✓ Tree is clean and equals origin/main ($(git -C "$root" rev-parse --short HEAD))."
    return 0
}

# ── 2. The tree must KNOW the live database's revision ───────────────────────
#
# AUTO_MIGRATE=true runs `alembic upgrade head` at boot. If the live database is
# at a revision this tree has never heard of, the image is BEHIND the database:
# alembic cannot locate the current revision, and what happens next is decided
# at boot on the live instance. The existing migration preflight compares the
# tree against whatever database it can reach — which, from a worktree with no
# Cloud SQL config, is a local SQLite file. This guard asks the SERVING service
# instead (GET /health is public), so it cannot be fooled by local config.
#
# Pure: does <versions_dir> contain a migration file for <rev>?
_tree_knows_revision() {
    local rev="$1" versions_dir="$2"
    [ -n "$rev" ] && [ -d "$versions_dir" ] || return 1
    compgen -G "${versions_dir}/${rev}_*.py" >/dev/null 2>&1 || \
        compgen -G "${versions_dir}/${rev}.py" >/dev/null 2>&1
}

# Pull migrations.db_revision out of a /health payload. Empty when absent.
_health_db_revision() {
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
m = d.get("migrations") or {}
print(m.get("db_revision") or "")
' 2>/dev/null
}

# guard_tree_knows_live_db <service> <region> <project>
guard_tree_knows_live_db() {
    if [ "${DEPLOY_ALLOW_BEHIND_DB:-}" = "1" ]; then
        echo "⚠️  DEPLOY_ALLOW_BEHIND_DB=1 — NOT checking the tree against the live DB revision."
        return 0
    fi
    local service="$1" region="$2" project="$3" root url health rev
    root="$(_dg_repo_root)"
    url="$(gcloud run services describe "$service" --region "$region" --project "$project" \
            --format='value(status.url)' 2>/dev/null || true)"
    if [ -z "$url" ]; then
        echo "⚠️  Could not resolve the live service URL (first deploy, or gcloud unavailable)."
        echo "   Skipping the live-DB revision check. Continuing."
        return 0
    fi
    health="$(curl -fsS --max-time 25 "${url}/health" 2>/dev/null || true)"
    rev="$(printf '%s' "$health" | _health_db_revision)"
    if [ -z "$rev" ]; then
        echo "⚠️  /health did not report a database revision — cannot compare. Continuing."
        return 0
    fi
    if _tree_knows_revision "$rev" "${root}/alembic/versions"; then
        echo "✓ Tree knows the live database revision (${rev})."
        return 0
    fi
    echo "" >&2
    echo "❌ Deploy refused: the live database is at Alembic revision ${rev}," >&2
    echo "   and this tree has NO migration file for it. The image would be BEHIND" >&2
    echo "   the database it boots against with AUTO_MIGRATE=true." >&2
    echo "" >&2
    echo "   This is almost always a checkout that has not pulled in a long time." >&2
    echo "   Tree's newest migrations:" >&2
    ls "${root}/alembic/versions" 2>/dev/null | grep -oE '^(zc|y|c)[0-9]+' | sort | tail -3 | sed 's/^/       /' >&2
    echo "" >&2
    echo "   Fix: fetch origin/main and bring the reviewed migration into the release branch." >&2
    echo "   Override only if you are certain: DEPLOY_ALLOW_BEHIND_DB=1" >&2
    exit 5
}

# ── 3. After deploying, the NEW revision must actually be serving ────────────
#
# Cloud Run treats a revision at 0% traffic as a successful deploy, and
# `gcloud run deploy` reports it as one — so with traffic pinned to an older
# revision, every deploy "succeeds" and nothing changes for users. This is the
# check that would have caught three stranded deploys in one morning.
#
# Pure: given the service JSON, is the latest ready revision at 100%?
# Prints "OK", or "PINNED <revision-holding-traffic>" for the error message.
_traffic_serving_latest() {
    python3 -c '
import json, sys
d = json.load(sys.stdin)
st = d.get("status") or {}
latest = st.get("latestReadyRevisionName") or ""
traffic = st.get("traffic") or []
for t in traffic:
    pct = t.get("percent") or 0
    if pct == 100 and (t.get("latestRevision") or t.get("revisionName") == latest):
        print("OK"); sys.exit(0)
holder = next((t.get("revisionName") or "LATEST" for t in traffic if (t.get("percent") or 0) > 0), "nothing")
print(f"PINNED {holder}")
' 2>/dev/null
}

# guard_new_revision_is_serving <service> <region> <project>
guard_new_revision_is_serving() {
    if [ "${DEPLOY_ALLOW_PINNED_TRAFFIC:-}" = "1" ]; then
        echo "⚠️  DEPLOY_ALLOW_PINNED_TRAFFIC=1 — not asserting the new revision holds traffic."
        return 0
    fi
    local service="$1" region="$2" project="$3" json verdict latest
    json="$(gcloud run services describe "$service" --region "$region" --project "$project" \
              --format=json 2>/dev/null || true)"
    if [ -z "$json" ]; then
        echo "⚠️  Could not read the service after deploy — cannot confirm it is serving. Continuing."
        return 0
    fi
    verdict="$(printf '%s' "$json" | _traffic_serving_latest)"
    latest="$(printf '%s' "$json" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("latestReadyRevisionName") or "")' 2>/dev/null)"
    case "$verdict" in
        OK)
            echo "✓ New revision ${latest} is serving 100% of traffic."
            return 0 ;;
        PINNED*)
            echo "" >&2
            echo "❌ REVISION DEPLOYED BUT NOT SERVING: ${latest} was built and is healthy," >&2
            echo "   but traffic is pinned to ${verdict#PINNED } and this revision holds 0%." >&2
            echo "   Users are still on the OLD code. Nothing you just shipped is live." >&2
            echo "" >&2
            echo "   Release the pin (this promotes the newest revision):" >&2
            echo "       gcloud run services update-traffic ${service} --region ${region} --project ${project} --to-latest" >&2
            echo "   then re-point the Jobs, which this script deliberately did NOT do while" >&2
            echo "   the Service was not serving (that split is worse than stale jobs):" >&2
            echo "       bash scripts/platform/deploy.sh --env <file>" >&2
            echo "" >&2
            echo "   If the pin is deliberate (a canary), rerun with DEPLOY_ALLOW_PINNED_TRAFFIC=1." >&2
            exit 6 ;;
        *)
            echo "⚠️  Could not interpret the service's traffic block — cannot confirm serving. Continuing."
            return 0 ;;
    esac
}
