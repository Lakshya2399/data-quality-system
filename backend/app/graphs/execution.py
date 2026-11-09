"""Graph B - run the highest-rated rules against a production file.

  load_rules_node --(rules found)--> execute_node -> persist_results_node -> END
        |
        +--(none rated >= threshold)--> END   (execution is marked failed with an explicit message)

Rules come from SQLite (not from memory), so a run can be executed any time after the pipeline,
including after a restart. Lookup tables are re-read from the run's stored inputs.
"""
from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, START, StateGraph

from .. import db, storage
from ..agent_schemas import DataModel
from ..config import settings
from ..rule_engine import run_rule
from .common import primary_key_column
from .state import ExecutionState


def build_execution_graph(
    db_path: Path | str | None = None,
    workdir: Path | str | None = None,
    *,
    rating_threshold: int | None = None,
):
    threshold = settings.rating_threshold if rating_threshold is None else rating_threshold

    def load_rules_node(state: ExecutionState) -> dict:
        run_id, exec_id = state["run_id"], state["exec_id"]
        with db.get_db(db_path) as conn:
            run = db.get_run(conn, run_id)
            rows = db.list_rules(conn, run_id, min_score=threshold)
            model_row = db.get_data_model(conn, run_id)
            if not rows:
                db.finish_execution(
                    conn, exec_id, "failed",
                    error=f"No rules rated {threshold} or higher for this run, so nothing was executed. "
                          f"Re-run the sample pipeline or lower the rating threshold.",
                )
        if not rows:
            return {"selected": []}

        model = DataModel.model_validate(model_row["model"])
        target = storage.table_name(run["sample_file"])
        directory = storage.run_dir(run_id, workdir)
        lookups = {t: storage.load_table(directory, t) for t in model.table_names() if t != target}
        return {
            "selected": [{"pk": r["id"], "rule": db.rule_from_row(r), "score": r["score"]} for r in rows],
            "lookups": lookups,
            "id_column": primary_key_column(model, target),
        }

    def route_after_load(state: ExecutionState) -> str:
        return "execute" if state.get("selected") else "end"

    def execute_node(state: ExecutionState) -> dict:
        results = [
            (s["pk"], run_rule(s["rule"], state["target_df"], state["lookups"], state.get("id_column")))
            for s in state["selected"]
        ]
        return {"results": results}

    def persist_results_node(state: ExecutionState) -> dict:
        with db.get_db(db_path) as conn:
            for pk, result in state["results"]:
                db.save_execution_result(conn, state["exec_id"], pk, result)
            db.finish_execution(conn, state["exec_id"], "completed", total_rows=len(state["target_df"]))
        return {}

    g = StateGraph(ExecutionState)
    g.add_node("load_rules_node", load_rules_node)
    g.add_node("execute_node", execute_node)
    g.add_node("persist_results_node", persist_results_node)
    g.add_edge(START, "load_rules_node")
    g.add_conditional_edges("load_rules_node", route_after_load, {"execute": "execute_node", "end": END})
    g.add_edge("execute_node", "persist_results_node")
    g.add_edge("persist_results_node", END)
    return g.compile()
