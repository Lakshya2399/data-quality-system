"""Response models for the HTTP API (they also drive the OpenAPI docs at /docs)."""
from typing import Any

from pydantic import BaseModel, Field


class RunOut(BaseModel):
    run_id: str
    created_at: str
    sample_file: str | None = None
    lookup_file: str | None = None
    status: str = Field(description="created | running | completed | failed")
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    rule_count: int | None = None
    execution_count: int | None = None


class StartedRun(BaseModel):
    run_id: str
    status: str
    poll_url: str


class ModelOut(BaseModel):
    run_id: str
    created_at: str
    model: dict[str, Any]
    profile: dict[str, Any] | None = Field(default=None, description="only with ?include_profile=true")


class RuleOut(BaseModel):
    id: int
    rule_key: str
    name: str
    type: str
    severity: str | None = None
    description: str | None = None
    definition: dict[str, Any]
    sample_fail_count: int | None = None
    sample_pass_rate: float | None = None
    score: int | None = None
    justification: str | None = None
    will_execute: bool = Field(description="score >= min_score")


class RulesOut(BaseModel):
    run_id: str
    status: str
    min_score: int
    executable_count: int
    score_distribution: dict[str, int]
    warnings: list[str]
    rules: list[RuleOut]


class ExecutionStarted(BaseModel):
    exec_id: int
    run_id: str
    status: str
    poll_url: str


class ExecutionResultOut(BaseModel):
    rule_key: str
    rule_name: str
    rule_type: str
    severity: str | None = None
    description: str | None = None
    score: int | None = None
    justification: str | None = None
    status: str = Field(description="ok | error (the rule could not run, e.g. a missing column)")
    error: str | None = None
    total_rows: int | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    skipped_count: int | None = None
    failed_row_ids: list[str] = Field(default_factory=list)
    failed_row_indices: list[int] = Field(default_factory=list)
    failed_ids_truncated: bool = False
    sample_reasons: list[dict[str, Any]] = Field(default_factory=list)


class ExecutionSummary(BaseModel):
    rules_executed: int
    rules_with_failures: int
    rules_errored: int


class ExecutionListItem(BaseModel):
    exec_id: int
    run_id: str
    target_file: str | None = None
    total_rows: int | None = None
    status: str
    error: str | None = None
    started_at: str
    finished_at: str | None = None


class ExecutionOut(ExecutionListItem):
    summary: ExecutionSummary
    results: list[ExecutionResultOut]
