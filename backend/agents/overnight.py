"""Department-pluggable overnight orchestrator.

Runs nightly at 02:00 UTC via Cloud Scheduler. Iterates every plugin in the
registry, persists their recommendations, and reports a per-plugin summary.
A failure in one plugin (e.g. HubSpot pipeline broken) does NOT cascade —
the orchestrator catches, records, and moves on to the next plugin.

Callable two ways:
  - `python -m backend.agents.overnight`               standalone CLI
  - `POST /api/agents/overnight/run` with `X-Scheduler-Key`  via Cloud Scheduler
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from backend.agents.base import (
    DepartmentAgentPlugin,
    persist_recommendations,
)
from backend.agents.registry import default_plugins
from rubik_core.database.db import SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class PluginResult:
    plugin_name: str
    department: str
    written: int = 0
    skipped_duplicates: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    started_at: datetime
    finished_at: datetime
    plugin_results: list[PluginResult] = field(default_factory=list)

    @property
    def written(self) -> int:
        return sum(p.written for p in self.plugin_results)

    @property
    def skipped_duplicates(self) -> int:
        return sum(p.skipped_duplicates for p in self.plugin_results)

    @property
    def by_type(self) -> dict[str, int]:
        agg: dict[str, int] = {}
        for p in self.plugin_results:
            for t, n in p.by_type.items():
                agg[t] = agg.get(t, 0) + n
        return agg

    @property
    def errors(self) -> list[str]:
        return [f"{p.plugin_name}: {p.error}" for p in self.plugin_results if p.error]


class OvernightAgent:
    """Orchestrates all registered department plugins for a single nightly run."""

    def __init__(
        self,
        plugins: list[DepartmentAgentPlugin] | None = None,
        db_factory=SessionLocal,
    ):
        self._plugins = plugins if plugins is not None else default_plugins()
        self._db_factory = db_factory

    def run(self) -> RunResult:
        run_id = str(uuid.uuid4())
        started = datetime.utcnow()
        results: list[PluginResult] = []

        for plugin in self._plugins:
            results.append(self._run_one_plugin(plugin, run_id))

        finished = datetime.utcnow()
        result = RunResult(
            run_id=run_id,
            started_at=started,
            finished_at=finished,
            plugin_results=results,
        )
        logger.info(
            f"[overnight] run_id={run_id} written={result.written} "
            f"skipped={result.skipped_duplicates} by_type={result.by_type} "
            f"plugins={[(p.plugin_name, p.written) for p in results]} "
            f"errors={result.errors} duration={finished - started}"
        )
        return result

    def _run_one_plugin(
        self, plugin: DepartmentAgentPlugin, run_id: str
    ) -> PluginResult:
        """Catch any exception inside a plugin so one bad plugin doesn't break the run."""
        out = PluginResult(plugin_name=plugin.name, department=plugin.department)
        db = self._db_factory()
        try:
            recs = plugin.run(db)
            for rec in recs:
                out.by_type[rec.type] = out.by_type.get(rec.type, 0) + 1
            written, skipped = persist_recommendations(db, recs, run_id, plugin.name)
            out.written = written
            out.skipped_duplicates = skipped
        except Exception as e:
            logger.exception(f"[overnight] plugin {plugin.name} failed")
            out.error = str(e)
        finally:
            db.close()
        return out


def main() -> int:
    """CLI entrypoint: `python -m backend.agents.overnight`."""
    from rubik_core.tenancy.cli import require_cli_workspace

    require_cli_workspace()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    result = OvernightAgent().run()
    logger.info(f"Done. Result: {result}")
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
