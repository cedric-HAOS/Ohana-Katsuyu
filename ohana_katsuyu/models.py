"""Minimal local representation of Ohana-Agent's distributed job protocol v1."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProtocolModel(BaseModel):
    """Strict protocol model rejecting fields unknown to Katsuyu v1."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JobStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    WAITING_WORKER = "WAITING_WORKER"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


class JobError(ProtocolModel):
    code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False


class JobProgress(ProtocolModel):
    percent: float = Field(ge=0, le=100)
    stage: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    message: str | None = Field(default=None, max_length=500)


class JobDocument(ProtocolModel):
    protocol_version: Literal[1] = 1
    job_id: UUID
    type: str
    created_at: datetime
    parameters: dict[str, Any]
    timeout: int
    status: JobStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    result_sha256: str | None = None
    error: JobError | None = None
    worker_id: str | None = None
    attempt: int = Field(default=0, ge=0)
    lease_expires_at: datetime | None = None
    progress: JobProgress | None = None


class JobClaim(ProtocolModel):
    protocol_version: Literal[1] = 1
    worker_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    supported_types: list[str] = Field(min_length=1, max_length=32)


class JobClaimResult(ProtocolModel):
    protocol_version: Literal[1] = 1
    job: JobDocument | None = None


class JobHeartbeat(ProtocolModel):
    protocol_version: Literal[1] = 1
    worker_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    attempt: int = Field(ge=1)
    progress: JobProgress | None = None


class JobCompletion(JobHeartbeat):
    status: Literal[JobStatus.SUCCEEDED, JobStatus.FAILED]
    result: dict[str, Any] | None = None
    error: JobError | None = None

    @model_validator(mode="after")
    def validate_terminal_payload(self) -> Self:
        if self.status == JobStatus.SUCCEEDED:
            if self.result is None or self.error is not None:
                raise ValueError("SUCCEEDED requires result and forbids error")
        elif self.result is not None or self.error is None:
            raise ValueError("FAILED requires error and forbids result")
        return self


class WorkerRegistration(ProtocolModel):
    protocol_version: Literal[1] = 1
    worker_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    capabilities: list[str] = Field(min_length=1, max_length=32)
    platform: str = Field(min_length=1, max_length=100)
    worker_version: str = Field(min_length=1, max_length=40)


class WorkerDocument(WorkerRegistration):
    registered_at: datetime
    last_seen_at: datetime
    availability: Literal["AVAILABLE", "UNAVAILABLE", "WAKING"] = "AVAILABLE"
    woken_by_ohana: bool = False
    wake_requested_at: datetime | None = None
    wake_deadline_at: datetime | None = None


class SystemHealthParameters(ProtocolModel):
    pass


class SystemHealthIssue(ProtocolModel):
    code: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    message: str = Field(min_length=1, max_length=500)


class SystemHealthResult(ProtocolModel):
    status: Literal["OK", "DEGRADED"]
    collected_at: datetime
    platform: str = Field(min_length=1, max_length=100)
    cpu_percent: float = Field(ge=0, le=100)
    memory_total_bytes: int = Field(ge=1)
    memory_available_bytes: int = Field(ge=0)
    disk_total_bytes: int = Field(ge=1)
    disk_free_bytes: int = Field(ge=0)
    temperature_c: float | None = Field(default=None, ge=-50, le=150)
    issues: list[SystemHealthIssue] = Field(default_factory=list, max_length=32)


class BackupCompressParameters(ProtocolModel):
    source: str = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=500)
    compression_level: int = Field(default=6, ge=1, le=9)


class BackupCompressResult(ProtocolModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size: int = Field(ge=0)
    destination_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_size: int = Field(ge=1)


class BackupEncryptParameters(ProtocolModel):
    source: str = Field(min_length=1, max_length=500)
    destination: str = Field(min_length=1, max_length=500)
    recipient: str = Field(min_length=20, max_length=200, pattern=r"^age1[0-9a-z]+$")


class BackupEncryptResult(ProtocolModel):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size: int = Field(ge=0)
    destination_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_size: int = Field(ge=1)
    recipient: str = Field(min_length=20, max_length=200, pattern=r"^age1[0-9a-z]+$")


class BackupVerifyParameters(ProtocolModel):
    path: str = Field(min_length=1, max_length=500)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_size: int | None = Field(default=None, ge=0)


class BackupVerifyResult(ProtocolModel):
    valid: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    sha256_matches: bool
    size_matches: bool | None = None


class InfraBackupParameters(ProtocolModel):
    backup_id: str = Field(pattern=r"^[0-9]{8}T[0-9]{6}Z$")
    recipient: str = Field(min_length=20, max_length=200, pattern=r"^age1[0-9a-z]+$")
    compression_level: int = Field(default=6, ge=1, le=9)


class InfraBackupResult(ProtocolModel):
    backup_id: str = Field(pattern=r"^[0-9]{8}T[0-9]{6}Z$")
    remote_path: str = Field(min_length=1, max_length=1000)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_size: int = Field(ge=1)
    compressed_size: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    deleted_remote_backups: int = Field(default=0, ge=0)
    duration_seconds: float = Field(ge=0)
    cpu_seconds: float = Field(ge=0)
    peak_working_set_bytes: int | None = Field(default=None, ge=0)
    logical_io_read_bytes: int = Field(ge=0)
    logical_io_written_bytes: int = Field(ge=0)


LogSourceId = Literal["ha-01", "linky-01", "zwave-01"]


class LogBaseline(ProtocolModel):
    source: LogSourceId
    signature: str = Field(min_length=1, max_length=160)
    occurrences: int = Field(ge=0, le=1_000_000)


class LogsHealthCheckParameters(ProtocolModel):
    sources: list[LogSourceId] = Field(min_length=1, max_length=3)
    window_started_at: datetime
    window_ended_at: datetime
    max_bytes_per_source: int = Field(ge=1024, le=4 * 1024 * 1024)
    baseline: list[LogBaseline] = Field(default_factory=list, max_length=192)
    incident_id: UUID | None = None

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("log sources must be unique")
        timestamps = (self.window_started_at, self.window_ended_at)
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in timestamps
        ):
            raise ValueError("log analysis timestamps must include a timezone")
        duration = self.window_ended_at - self.window_started_at
        if duration.total_seconds() <= 0 or duration.total_seconds() > 48 * 3600:
            raise ValueError(
                "log health window must be greater than zero and at most 48 hours"
            )
        return self


class LogsInvestigateParameters(ProtocolModel):
    source: LogSourceId
    window_started_at: datetime
    window_ended_at: datetime
    pattern: str = Field(min_length=1, max_length=160)
    max_bytes: int = Field(ge=1024, le=4 * 1024 * 1024)
    incident_id: UUID

    @model_validator(mode="after")
    def validate_window_and_pattern(self) -> Self:
        timestamps = (self.window_started_at, self.window_ended_at)
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in timestamps
        ):
            raise ValueError("log investigation timestamps must include a timezone")
        duration = self.window_ended_at - self.window_started_at
        if duration.total_seconds() <= 0 or duration.total_seconds() > 2 * 3600:
            raise ValueError(
                "log investigation window must be greater than zero "
                "and at most two hours"
            )
        if any(character in self.pattern for character in "\r\n\0"):
            raise ValueError("log investigation pattern must be plain single-line text")
        return self


class LogFinding(ProtocolModel):
    source: LogSourceId
    signature: str = Field(min_length=1, max_length=160)
    category: Literal[
        "exception",
        "timeout",
        "mqtt",
        "network",
        "restart",
        "serial",
        "zwave",
        "automation",
        "other",
    ]
    severity: Literal["warning", "error", "critical"]
    summary: str = Field(min_length=1, max_length=500)
    occurrences: int = Field(ge=1, le=1_000_000)
    first_at: datetime | None = None
    last_at: datetime | None = None
    trend: Literal["new", "known", "stable", "increasing", "decreasing", "disappeared"]


class LogSourceHealth(ProtocolModel):
    source: LogSourceId
    status: Literal["OK", "KO"]
    fetched_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    truncated: bool
    analyzed_lines: int = Field(ge=0, le=200_000)
    findings: list[LogFinding] = Field(default_factory=list, max_length=64)


class LogCorrelation(ProtocolModel):
    sources: list[LogSourceId] = Field(min_length=2, max_length=3)
    occurred_at: datetime
    summary: str = Field(min_length=1, max_length=500)


class LogsHealthCheckResult(ProtocolModel):
    status: Literal["OK", "KO"]
    analyzed_at: datetime
    window_started_at: datetime
    window_ended_at: datetime
    sources: list[LogSourceHealth] = Field(min_length=1, max_length=3)
    new_anomaly_count: int = Field(ge=0)
    worsening_anomaly_count: int = Field(ge=0)
    disappeared_anomalies: list[LogBaseline] = Field(
        default_factory=list, max_length=192
    )
    correlations: list[LogCorrelation] = Field(default_factory=list, max_length=32)
    recommended_investigations: list[str] = Field(default_factory=list, max_length=16)


class LogsInvestigateResult(ProtocolModel):
    status: Literal["OK", "KO"]
    analyzed_at: datetime
    source: LogSourceId
    pattern: str = Field(min_length=1, max_length=160)
    matched_lines: int = Field(ge=0, le=200_000)
    findings: list[LogFinding] = Field(default_factory=list, max_length=64)
    truncated: bool


class AiInferenceEvidence(ProtocolModel):
    source: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    content: str = Field(min_length=1, max_length=16_000)


class AiInferenceParameters(ProtocolModel):
    task: Literal["technical.diagnosis"] = "technical.diagnosis"
    incident_id: UUID
    question: str = Field(min_length=1, max_length=2_000)
    evidence: list[AiInferenceEvidence] = Field(min_length=1, max_length=8)
    max_output_tokens: int = Field(default=1_024, ge=128, le=1_024)

    @model_validator(mode="after")
    def bound_total_evidence(self) -> Self:
        if sum(len(item.content) for item in self.evidence) > 48_000:
            raise ValueError("ai.inference evidence cannot exceed 48000 characters")
        return self


class AiInferenceFinding(ProtocolModel):
    code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z0-9_.-]+$")
    evidence: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class AiInferenceMetrics(ProtocolModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    ttft_ms: float = Field(ge=0)
    tokens_per_second: float = Field(ge=0)
    duration_seconds: float = Field(ge=0)


class AiInferenceHypothesis(ProtocolModel):
    statement: str = Field(min_length=1, max_length=1_000)
    confidence: float = Field(ge=0, le=1)
    possible_causes: list[str] = Field(default_factory=list, max_length=8)
    supporting_evidence: list[str] = Field(default_factory=list, max_length=8)
    contradicting_evidence: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_bounded_hypothesis_text(self) -> Self:
        values = [
            *self.possible_causes,
            *self.supporting_evidence,
            *self.contradicting_evidence,
        ]
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("hypothesis entries must contain 1 to 500 characters")
        return self


class AiInferenceResult(ProtocolModel):
    analysis_version: Literal[1, 2] = 1
    verdict: Literal["OK", "KO", "INSUFFICIENT_CONTEXT"]
    generated_at: datetime
    model_id: str = Field(min_length=1, max_length=120)
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interpretation: str = Field(default="", max_length=2_000)
    summary: str = Field(min_length=1, max_length=1_000)
    findings: list[AiInferenceFinding] = Field(default_factory=list, max_length=16)
    hypotheses: list[AiInferenceHypothesis] = Field(default_factory=list, max_length=8)
    missing_context: list[str] = Field(default_factory=list, max_length=16)
    recommended_investigation: list[str] = Field(default_factory=list, max_length=16)
    metrics: AiInferenceMetrics

    @model_validator(mode="after")
    def validate_diagnostic_consistency(self) -> Self:
        if self.verdict == "OK" and self.findings:
            raise ValueError("OK cannot contain anomaly findings")
        if self.verdict == "KO" and not self.findings:
            raise ValueError("KO requires at least one finding")
        if self.analysis_version == 2 and not self.interpretation.strip():
            raise ValueError("analysis version 2 requires an interpretation")
        if self.analysis_version == 2 and self.verdict == "KO" and not self.hypotheses:
            raise ValueError("KO requires at least one explicitly uncertain hypothesis")
        if self.verdict == "INSUFFICIENT_CONTEXT" and not self.missing_context:
            raise ValueError("INSUFFICIENT_CONTEXT requires missing_context")
        bounded_text = [*self.missing_context, *self.recommended_investigation]
        if any(not value.strip() or len(value) > 500 for value in bounded_text):
            raise ValueError("diagnostic list entries must contain 1 to 500 characters")
        return self
