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
