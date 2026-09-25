"""Tests for Katsuyu's deterministic local handlers."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import string
import tarfile
from io import StringIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from ohana_katsuyu.handlers import (
    BackupCompressHandler,
    BackupEncryptHandler,
    BackupVerifyHandler,
    HandlerContext,
    InfraBackupHandler,
    JobCancelledError,
    KatsuyuWorkspace,
    LogsHealthCheckHandler,
    LogsInvestigateHandler,
    SystemHealthHandler,
    SystemMetrics,
    _signature,
)


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        (
            [
                "s6-rc: info: service example: starting",
                "s6-rc: info: service example successfully started",
            ],
            0,
        ),
        (["s6-rc: info: service example: starting"], 1),
        (["s6-rc: info: service example successfully started"], 1),
        (
            [
                "s6-rc: info: service example successfully started",
                "s6-rc: info: service example: starting",
            ],
            2,
        ),
        (
            [
                "s6-rc: info: service example: starting",
                "s6-rc: info: service other successfully started",
            ],
            2,
        ),
        (
            [
                "s6-rc: info: service example: starting",
                "s6-rc: info: service example successfully started",
            ]
            * 2,
            2,
        ),
        (
            [
                "s6-rc: info: service example: starting",
                "s6-rc: warning: service example failed",
            ],
            2,
        ),
        (
            [
                "s6-rc: info: service example: starting",
                "s6-rc: info: service example successfully started; timeout",
            ],
            2,
        ),
        (
            [
                "s6-rc: info: service example: starting",
                "s6-rc: info: service example successfully started",
                "s6-rc: warning: service example failed",
            ],
            1,
        ),
    ],
)
def test_s6_single_completed_startup_is_not_a_fault(
    monkeypatch, targeted, messages, expected
):
    handler_type = LogsInvestigateHandler if targeted else LogsHealthCheckHandler
    handler = handler_type(lambda *_args: {})
    monkeypatch.setattr(handler.reader, "read", lambda *_args: (messages, 1000, True))
    parameters = {
        "window_started_at": "2026-09-21T08:00:00+02:00",
        "window_ended_at": "2026-09-21T10:00:00+02:00",
    }
    if targeted:
        parameters.update(
            source="zwave-01",
            pattern="s6-rc",
            max_bytes=4096,
            incident_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        parameters.update(sources=["zwave-01"], max_bytes_per_source=4096)
    result = handler.execute(parameters, _log_context())
    source = result if targeted else result["sources"][0]
    assert len(source["findings"]) == expected
    assert source["truncated"] is True
    assert all(
        f["first_at"] is None and f["last_at"] is None for f in source["findings"]
    )
    if targeted:
        assert result["matched_lines"] == len(messages)
    else:
        assert source["analyzed_lines"] == len(messages)


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize(
    ("source_id", "prefixes", "pattern", "expected", "matches"),
    [
        ("linky-01", ("", ""), "starting", 0, 1),
        ("ha-01", ("", ""), "s6-rc", 0, 2),
        ("infra-01", ("", ""), "s6-rc", 2, 2),
        (
            "zwave-01",
            ("2026-09-21T08:10:00+02:00 ", "2026-09-21T08:11:00+02:00 "),
            "s6-rc",
            0,
            2,
        ),
        (
            "zwave-01",
            ("2026-09-21T07:10:00+02:00 ", "2026-09-21T08:11:00+02:00 "),
            "s6-rc",
            1,
            1,
        ),
        (
            "zwave-01",
            ("2026-09-21T08:12:00+02:00 ", "2026-09-21T08:11:00+02:00 "),
            "s6-rc",
            2,
            2,
        ),
        ("zwave-01", ("", "2026-09-21T08:11:00+02:00 "), "s6-rc", 2, 2),
        ("zwave-01", ("ERROR ", "ERROR "), "s6-rc", 2, 2),
    ],
)
def test_s6_pair_respects_source_window_and_search_scope(
    monkeypatch, targeted, source_id, prefixes, pattern, expected, matches
):
    lines = [
        prefixes[0] + "s6-rc: info: service example: starting",
        prefixes[1] + "s6-rc: info: service example successfully started",
    ]
    handler = (LogsInvestigateHandler if targeted else LogsHealthCheckHandler)(
        lambda *_args: {}
    )
    monkeypatch.setattr(handler.reader, "read", lambda *_args: (lines, 1000, False))
    parameters = {
        "window_started_at": "2026-09-21T08:00:00+02:00",
        "window_ended_at": "2026-09-21T10:00:00+02:00",
    }
    if targeted:
        parameters.update(
            source=source_id,
            pattern=pattern,
            max_bytes=4096,
            incident_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        parameters.update(sources=[source_id], max_bytes_per_source=4096)
    result = handler.execute(parameters, _log_context())
    source = result if targeted else result["sources"][0]
    assert len(source["findings"]) == expected
    assert source["truncated"] is False
    if targeted:
        assert result["matched_lines"] == matches


@pytest.mark.parametrize("targeted", [False, True])
def test_s6_success_beyond_analysis_budget_does_not_hide_incomplete_start(
    monkeypatch, targeted
):
    lines = ["s6-rc: info: service example: starting"] + ["ordinary activity"] * 199_999
    lines.append("s6-rc: info: service example successfully started")
    handler = (LogsInvestigateHandler if targeted else LogsHealthCheckHandler)(
        lambda *_args: {}
    )
    monkeypatch.setattr(handler.reader, "read", lambda *_args: (lines, 1000, False))
    parameters = {
        "window_started_at": "2026-09-21T08:00:00+02:00",
        "window_ended_at": "2026-09-21T10:00:00+02:00",
    }
    if targeted:
        parameters.update(
            source="zwave-01",
            pattern="s6-rc",
            max_bytes=4096,
            incident_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        parameters.update(sources=["zwave-01"], max_bytes_per_source=4096)
    result = handler.execute(parameters, _log_context())
    source = result if targeted else result["sources"][0]
    assert len(source["findings"]) == 1
    assert source["truncated"] is True
    assert source["findings"][0]["occurrences"] == 1


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize(
    ("message", "expected_severity"),
    [
        ('INFO: 127.0.0.1:1234 - "GET /search?q=critical HTTP/1.1" 200 OK', None),
        (
            'INFO: 127.0.0.1:1234 - "POST /restart?error=fatal HTTP/1.1" 202 Accepted',
            None,
        ),
        ('INFO: 127.0.0.1:1234 - "GET /error HTTP/1.1" 302 Found', None),
        ('INFO: 127.0.0.1:1234 - "GET /error HTTP/1.1" 401 Unauthorized', None),
        (
            'INFO: 127.0.0.1:1234 - "GET /critical HTTP/1.1" 500 Internal Server Error',
            "error",
        ),
        (
            'INFO: 127.0.0.1:1234 - "GET /ready HTTP/1.1" 503 Service Unavailable',
            "error",
        ),
        (
            'INFO: 127.0.0.1:1234 - "GET /critical?value='
            + "x" * 600
            + ' HTTP/1.1" 503 Service Unavailable',
            "error",
        ),
        ('ERROR: 127.0.0.1:1234 - "GET /critical HTTP/1.1" 200 OK', "error"),
        ('CRITICAL: 127.0.0.1:1234 - "GET /ready HTTP/1.1" 200 OK', "critical"),
        (
            'INFO: 127.0.0.1:1234 - "GET /ready HTTP/1.1" 200 OK; critical failure',
            "critical",
        ),
        ("WARNING failed request GET /critical", "critical"),
    ],
)
def test_http_access_targets_do_not_define_anomaly_severity(
    monkeypatch, targeted, message, expected_severity
):
    line = "2026-09-20T17:35:10+02:00 infra-01 ohana-vision[42]: " + message
    handler_type = LogsInvestigateHandler if targeted else LogsHealthCheckHandler
    handler = handler_type(lambda *_args: {})
    monkeypatch.setattr(handler.reader, "read", lambda *_args: ([line], 1000, False))
    parameters = {
        "window_started_at": "2026-09-20T17:00:00+02:00",
        "window_ended_at": "2026-09-20T18:00:00+02:00",
    }
    if targeted:
        parameters.update(
            source="infra-01",
            pattern="HTTP" if "HTTP" in message else "request",
            max_bytes=4096,
            incident_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        parameters.update(sources=["infra-01"], max_bytes_per_source=4096)
    result = handler.execute(parameters, _log_context())
    source = result if targeted else result["sources"][0]
    assert [f["severity"] for f in source["findings"]] == (
        [] if expected_severity is None else [expected_severity]
    )
    assert source["truncated"] is False
    if targeted:
        assert result["matched_lines"] == 1


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("limit", ["lines", "groups"])
@pytest.mark.parametrize("overflow", [False, True])
def test_log_analysis_reports_its_own_truncation(
    monkeypatch, targeted, limit, overflow
):
    if limit == "lines":
        lines = ["ok"] * 200_000 + (["ERROR omitted"] if overflow else [])
    else:
        names = [a + b for a in string.ascii_lowercase for b in string.ascii_lowercase]
        lines = [f"ERROR failure {name}" for name in names[: 64 + int(overflow)]]
    handler_type = LogsInvestigateHandler if targeted else LogsHealthCheckHandler
    handler = handler_type(lambda *_args: {})
    monkeypatch.setattr(handler.reader, "read", lambda *_args: (lines, 1000, False))
    parameters = {
        "window_started_at": "2026-09-19T00:00:00+02:00",
        "window_ended_at": "2026-09-19T02:00:00+02:00",
    }
    if targeted:
        parameters.update(
            source="zwave-01",
            pattern="ERROR",
            max_bytes=4 * 1024 * 1024,
            incident_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        parameters["sources"] = ["zwave-01"]
        parameters["max_bytes_per_source"] = 4 * 1024 * 1024
        parameters["baseline"] = [
            {"source": "zwave-01", "signature": "old error", "occurrences": 1}
        ]
    result = handler.execute(parameters, _log_context())
    source = result if targeted else result["sources"][0]
    assert source["truncated"] is overflow
    assert len(source["findings"]) == (0 if limit == "lines" else 64)
    if not targeted:
        assert len(result["disappeared_anomalies"]) == (0 if overflow else 1)


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("critical_message", ["CRITICAL unique", "FATAL unique"])
def test_rare_critical_group_survives_findings_limit(
    monkeypatch, targeted, critical_message
):
    names = [a + b for a in string.ascii_lowercase for b in string.ascii_lowercase]
    lines = [f"ERROR unique failure {name}" for name in names[:64]] * 3
    lines.append(critical_message)
    handler_type = LogsInvestigateHandler if targeted else LogsHealthCheckHandler
    handler = handler_type(lambda *_args: {})
    monkeypatch.setattr(handler.reader, "read", lambda *_args: (lines, 1000, False))
    parameters = {
        "window_started_at": "2026-09-20T00:00:00+02:00",
        "window_ended_at": "2026-09-20T02:00:00+02:00",
    }
    if targeted:
        parameters.update(
            source="infra-01",
            pattern="unique",
            max_bytes=4096,
            incident_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        parameters.update(sources=["infra-01"], max_bytes_per_source=4096)
    result = handler.execute(parameters, _log_context())
    source = result if targeted else result["sources"][0]
    assert source["truncated"] is True
    assert len(source["findings"]) == 64
    critical = source["findings"][0]
    assert critical["severity"] == "critical"
    assert critical["occurrences"] == 1
    assert critical["first_at"] is None
    assert critical["last_at"] is None
    assert all(item["occurrences"] == 3 for item in source["findings"][1:])
    if targeted:
        assert result["matched_lines"] == 193


@pytest.mark.parametrize("truncated", [False, True])
def test_disappearance_requires_a_complete_collected_source(monkeypatch, truncated):
    handler = LogsHealthCheckHandler(lambda *_args: {})
    monkeypatch.setattr(handler.reader, "read", lambda *_args: ([], 0, truncated))
    result = handler.execute(
        {
            "sources": ["zwave-01"],
            "max_bytes_per_source": 4096,
            "window_started_at": "2026-09-19T00:00:00+02:00",
            "window_ended_at": "2026-09-20T00:00:00+02:00",
            "baseline": [
                {"source": source, "signature": "old error", "occurrences": 1}
                for source in ["zwave-01", "ha-01"]
            ],
        },
        _log_context(),
    )
    assert [item["source"] for item in result["disappeared_anomalies"]] == (
        [] if truncated else ["zwave-01"]
    )


def test_mqtt_information_is_not_an_anomaly_in_health_or_targeted_collection():
    content = "\n".join(
        [
            "2026-09-15T05:00:00+02:00 INFO scheduled task completed mqtt.roundtrip OK",
            "2026-09-15T05:00:01+02:00 INFO teleinfo2mqtt frame received",
        ]
    )
    provider = lambda *_args: {  # noqa: E731
        "source": "infra-01",
        "transport": "inline",
        "content": content,
        "truncated": False,
    }
    common = {
        "window_started_at": "2026-09-15T04:00:00+02:00",
        "window_ended_at": "2026-09-15T06:00:00+02:00",
    }
    health = LogsHealthCheckHandler(provider).execute(
        {**common, "sources": ["infra-01"], "max_bytes_per_source": 4096},
        _log_context(),
    )
    assert health["status"] == "OK"
    result = LogsInvestigateHandler(provider).execute(
        {
            **common,
            "source": "infra-01",
            "pattern": "mqtt",
            "max_bytes": 4096,
            "incident_id": "11111111-1111-4111-8111-111111111111",
        },
        _log_context(),
    )
    assert result["matched_lines"] == 2
    assert result["findings"] == []
    assert result["status"] == "OK"


class FakeProbe:
    def collect(self) -> SystemMetrics:
        return SystemMetrics(
            platform="Windows 11",
            cpu_percent=12.5,
            memory_total_bytes=8 * 1024**3,
            memory_available_bytes=5 * 1024**3,
        )


def _log_context() -> HandlerContext:
    return HandlerContext(job_id="job-logs", worker_id="bubule", attempt=1)


def _log_provider(
    job_id: str, worker_id: str, attempt: int, source: str
) -> dict[str, object]:
    assert (job_id, worker_id, attempt) == ("job-logs", "bubule", 1)
    return {
        "source": source,
        "url": f"http://{source}.ohana.lan:8123/api/error_log",
        "access_token": "secret",
        "verify_tls": True,
        "timeout_seconds": 10,
    }


def test_logs_health_check_groups_and_compares_without_llm(monkeypatch) -> None:
    content = (
        b"2026-08-24T09:00:00+00:00 ERROR Node 17 transmission failed\n"
        b"2026-08-24T09:01:00+00:00 ERROR Node 18 transmission failed\n"
        b"2026-08-22T09:01:00+00:00 ERROR old timeout\n"
        b"2026-08-24T09:02:00+00:00 INFO healthy\n"
    )
    monkeypatch.setattr(
        "ohana_katsuyu.handlers.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(content),
    )
    result = LogsHealthCheckHandler(_log_provider).execute(
        {
            "sources": ["zwave-01"],
            "window_started_at": "2026-08-24T00:00:00+00:00",
            "window_ended_at": "2026-08-25T00:00:00+00:00",
            "max_bytes_per_source": 4096,
            "baseline": [
                {
                    "source": "zwave-01",
                    "signature": _signature(
                        "2026-08-24T09:00:00+00:00 ERROR Node 17 transmission failed"
                    ),
                    "occurrences": 1,
                }
            ],
            "incident_id": None,
        },
        _log_context(),
    )

    assert result["status"] == "KO"
    assert result["sources"][0]["analyzed_lines"] == 3
    assert result["sources"][0]["findings"][0]["occurrences"] == 2
    assert result["sources"][0]["findings"][0]["reference_occurrences"] == 1
    assert result["sources"][0]["findings"][0]["category"] == "zwave"
    assert result["sources"][0]["findings"][0]["trend"] == "known"
    assert result["new_anomaly_count"] == 0


def test_logs_health_check_keeps_bounded_home_assistant_entity_references(
    monkeypatch,
) -> None:
    content = (
        b"2026-08-24T09:00:00+00:00 ERROR TemplateError: "
        b"states('sensor.teleinfo_041964385922_easf02') failed for entity "
        b"'sensor.linky_bleue_hp'\n"
    )
    monkeypatch.setattr(
        "ohana_katsuyu.handlers.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(content),
    )

    result = LogsHealthCheckHandler(_log_provider).execute(
        {
            "sources": ["ha-01"],
            "window_started_at": "2026-08-24T00:00:00+00:00",
            "window_ended_at": "2026-08-25T00:00:00+00:00",
            "max_bytes_per_source": 4096,
            "baseline": [],
            "incident_id": None,
        },
        _log_context(),
    )

    assert result["sources"][0]["findings"][0]["references"] == [
        "sensor.teleinfo_041964385922_easf02",
        "sensor.linky_bleue_hp",
    ]


def test_logs_health_check_reads_infra_journal_and_detects_service_lifecycle() -> None:
    content = "\n".join(
        (
            "2026-08-29T09:14:04+02:00 Ohana-Agent shutdown requested.",
            "2026-08-29T09:14:14+02:00 Stopped ohana-agent.service.",
            "2026-08-29T09:16:44+02:00 Started ohana-agent.service.",
        )
    )

    def provider(*_args) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "infra-01",
            "transport": "inline",
            "content": content,
            "truncated": False,
        }

    result = LogsHealthCheckHandler(provider).execute(
        {
            "sources": ["infra-01"],
            "window_started_at": "2026-08-29T09:00:00+02:00",
            "window_ended_at": "2026-08-29T10:00:00+02:00",
            "max_bytes_per_source": 4096,
            "baseline": [],
            "incident_id": None,
        },
        _log_context(),
    )

    assert result["status"] == "KO"
    assert result["sources"][0]["source"] == "infra-01"
    assert result["sources"][0]["analyzed_lines"] == 3
    assert len(result["sources"][0]["findings"]) == 3
    assert {finding["category"] for finding in result["sources"][0]["findings"]} == {
        "restart"
    }


@pytest.mark.parametrize("targeted", [False, True])
def test_camera_session_paths_are_removed_before_grouping_and_references(targeted):
    secrets = ["session.private*ABC!", "another.private*XYZ!"]
    content = "\n".join(
        f"2026-08-24T09:00:00Z ERROR connection failed /stok={secret}/ds sensor.camera"
        for secret in secrets
    )

    def provider(*_args):
        return {
            "schema_version": 1,
            "source": "ha-01",
            "transport": "inline",
            "content": content,
            "truncated": False,
        }

    parameters = {
        "window_started_at": "2026-08-24T08:00:00Z",
        "window_ended_at": "2026-08-24T10:00:00Z",
    }
    if targeted:
        result = LogsInvestigateHandler(provider).execute(
            {
                **parameters,
                "source": "ha-01",
                "pattern": "failed",
                "max_bytes": 4096,
                "incident_id": "11111111-1111-4111-8111-111111111111",
            },
            _log_context(),
        )
        findings = result["findings"]
    else:
        result = LogsHealthCheckHandler(provider).execute(
            {
                **parameters,
                "sources": ["ha-01"],
                "max_bytes_per_source": 4096,
                "baseline": [
                    {
                        "source": "ha-01",
                        "signature": _signature(content.splitlines()[0]).replace(
                            "[redacted]", secret.lower()
                        ),
                        "occurrences": 1,
                    }
                    for secret in secrets
                ],
                "incident_id": None,
            },
            _log_context(),
        )
        findings = result["sources"][0]["findings"]
    encoded = json.dumps(result)
    assert all(secret not in encoded for secret in secrets)
    assert "session.private" not in encoded
    assert "another.private" not in encoded
    assert len(findings) == 1
    assert findings[0]["occurrences"] == 2
    assert "sensor.camera" in findings[0]["references"]
    assert "/stok=[redacted]/ds" in findings[0]["signature"]
    if not targeted:
        assert findings[0]["trend"] == "stable"
        assert findings[0]["reference_occurrences"] == 2


def test_journal_iso_dates_do_not_create_new_signatures_across_days():
    previous = (
        "2026-09-15T09:01:43.443348+02:00 infra-01 ohana-agent[123]: "
        "2026-09-15 09:01:43 WARNING connection timed out"
    )
    current = (
        "2026-09-16T10:02:44.123456+02:00 infra-01 ohana-agent[456]: "
        "2026-09-16 10:02:44 WARNING connection timed out"
    )
    signature = _signature(previous)
    assert signature == _signature(current)
    assert signature.count("<timestamp>") == 2
    assert "16t" not in signature
    from datetime import UTC, datetime

    result = LogsHealthCheckHandler._analyze_source(
        "infra-01",
        [current],
        len(current),
        False,
        datetime(2026, 9, 16, tzinfo=UTC),
        datetime(2026, 9, 17, tzinfo=UTC),
        {("infra-01", signature): 1},
    )
    assert len(result.findings) == 1
    assert result.findings[0].trend == "stable"
    assert result.findings[0].reference_occurrences == 1
    assert result.findings[0].last_at == datetime(
        2026, 9, 16, 8, 2, 44, 123456, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "message,anomaly",
    [
        ("INFO Z-WAVE-SERVER: Client disconnected", False),
        ("WARNING Z-WAVE-SERVER: Client disconnected", True),
        ("ERROR Z-WAVE-SERVER: Client disconnected", True),
        ("INFO Z-WAVE-SERVER: Client disconnected due to timeout", True),
        ("INFO OTHER-SERVER: Client disconnected", True),
        ("INFO Z-WAVE-SERVER: Node dead", True),
        ("INFO Z-WAVE: Starting bulk firmware update check for all nodes", False),
        ("INFO BACKUP: Backup store started", False),
        ("WARNING BACKUP: Backup store started", True),
        ("ERROR Z-WAVE: Starting bulk firmware update check for all nodes", True),
        ("INFO BACKUP: Backup store started with timeout", True),
        ("INFO Z-WAVE: Starting bulk firmware update check for all nodes failed", True),
        ("INFO OTHER: Backup store started", True),
        ("INFO Z-WAVE: Starting driver", True),
    ],
)
def test_zwave_info_activity_alone_does_not_create_anomaly_or_correlation(
    message, anomaly
):
    def provider(_job, _worker, _attempt, source):
        text = message if source == "zwave-01" else "ERROR connection timed out"
        return {
            "schema_version": 1,
            "source": source,
            "transport": "inline",
            "content": "2026-09-16T09:01:43+02:00 " + text,
            "truncated": False,
        }

    window = {
        "window_started_at": "2026-09-16T09:00:00+02:00",
        "window_ended_at": "2026-09-16T09:02:00+02:00",
    }
    general = LogsHealthCheckHandler(provider).execute(
        {
            **window,
            "sources": ["infra-01", "zwave-01"],
            "max_bytes_per_source": 4096,
            "baseline": [],
        },
        _log_context(),
    )
    zwave = next(s for s in general["sources"] if s["source"] == "zwave-01")
    assert bool(zwave["findings"]) is anomaly
    assert bool(general["correlations"]) is anomaly
    assert general["new_anomaly_count"] == 1 + int(anomaly)
    targeted = LogsInvestigateHandler(provider).execute(
        {
            **window,
            "source": "zwave-01",
            "pattern": message.split(":", 1)[0],
            "max_bytes": 4096,
            "incident_id": "11111111-1111-4111-8111-111111111111",
        },
        _log_context(),
    )
    assert targeted["matched_lines"] == 1
    assert bool(targeted["findings"]) is anomaly
    assert targeted["status"] == ("KO" if anomaly else "OK")


@pytest.mark.parametrize("source", ["infra-01", "ha-01"])
@pytest.mark.parametrize(
    "message",
    [
        "INFO BACKUP: Backup store started",
        "INFO Z-WAVE: Starting bulk firmware update check for all nodes",
    ],
)
def test_zwave_activity_exclusions_do_not_hide_other_source_events(source, message):
    def provider(_job, _worker, _attempt, requested_source):
        return {
            "schema_version": 1,
            "source": requested_source,
            "transport": "inline",
            "content": "2026-09-19T15:38:32+02:00 " + message,
            "truncated": False,
        }

    result = LogsHealthCheckHandler(provider).execute(
        {
            "sources": [source],
            "window_started_at": "2026-09-19T15:00:00+02:00",
            "window_ended_at": "2026-09-19T16:00:00+02:00",
            "max_bytes_per_source": 4096,
            "baseline": [],
        },
        _log_context(),
    )
    assert len(result["sources"][0]["findings"]) == 1


def test_logs_investigate_returns_only_a_grouped_synthesis(monkeypatch) -> None:
    content = "\n".join(
        [
            "2026-08-24T09:00:00+00:00 before",
            "2026-08-24T09:01:00+00:00 Node 17 transmission failed",
            "2026-08-24T09:02:00+00:00 after",
            "2026-08-22T09:02:00+00:00 Node 17 old",
        ]
    ).encode()
    monkeypatch.setattr(
        "ohana_katsuyu.handlers.urlopen",
        lambda *_args, **_kwargs: io.BytesIO(content),
    )
    result = LogsInvestigateHandler(_log_provider).execute(
        {
            "source": "zwave-01",
            "window_started_at": "2026-08-24T08:30:00+00:00",
            "window_ended_at": "2026-08-24T09:30:00+00:00",
            "pattern": "Node 17",
            "max_bytes": 4096,
            "incident_id": "11111111-1111-4111-8111-111111111111",
        },
        _log_context(),
    )

    assert result["status"] == "KO"
    assert result["matched_lines"] == 1
    assert len(result["findings"]) == 1
    assert "transmission failed" in result["findings"][0]["signature"]
    assert "2026-08-24" not in result["findings"][0]["summary"]


def test_logs_health_check_uses_supervisor_proxy_and_discovered_addon(
    monkeypatch,
) -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []
            self.responses = iter(
                [
                    {"type": "auth_required"},
                    {"type": "auth_ok"},
                    {
                        "id": 1,
                        "type": "result",
                        "success": True,
                        "result": {
                            "addons": [
                                {
                                    "slug": "a0d7b954_zwavejs2mqtt",
                                    "name": "Z-Wave JS UI",
                                }
                            ]
                        },
                    },
                ]
            )

        def recv(self) -> str:
            return json.dumps(next(self.responses))

        def send(self, payload: str) -> None:
            self.sent.append(json.loads(payload))

        def close(self) -> None:
            return None

    socket = FakeWebSocket()
    monkeypatch.setattr(
        "ohana_katsuyu.handlers.create_connection",
        lambda *_args, **_kwargs: socket,
    )
    requested_urls: list[str] = []

    def fake_urlopen(request, **_kwargs):
        requested_urls.append(request.full_url)
        if "/core/logs/latest" in request.full_url:
            return io.BytesIO(b"2026-08-24T09:00:00+00:00 INFO core healthy\n")
        if "/addons/a0d7b954_zwavejs2mqtt/logs" in request.full_url:
            return io.BytesIO(
                b"2026-08-24T09:01:00+00:00 ERROR Node 17 transmission failed\n"
            )
        raise AssertionError(f"unexpected URL: {request.full_url}")

    monkeypatch.setattr("ohana_katsuyu.handlers.urlopen", fake_urlopen)

    def provider(*_args) -> dict[str, object]:
        return {
            "source": "zwave-01",
            "base_url": "http://zwave-01.ohana.lan:8123",
            "url": (
                "http://zwave-01.ohana.lan:8123/api/hassio/"
                "core/logs/latest?lines=10000&no_colors=1"
            ),
            "access_token": "secret",
            "verify_tls": True,
            "timeout_seconds": 10,
            "addon_name_patterns": ["z-wave js", "zwavejs"],
        }

    result = LogsHealthCheckHandler(provider).execute(
        {
            "sources": ["zwave-01"],
            "window_started_at": "2026-08-24T00:00:00+00:00",
            "window_ended_at": "2026-08-25T00:00:00+00:00",
            "max_bytes_per_source": 4096,
            "baseline": [],
            "incident_id": None,
        },
        _log_context(),
    )

    assert result["status"] == "KO"
    assert result["sources"][0]["findings"][0]["category"] == "zwave"
    assert [request.get("endpoint") for request in socket.sent[1:]] == ["/addons"]
    assert requested_urls == [
        (
            "http://zwave-01.ohana.lan:8123/api/hassio/"
            "core/logs/latest?lines=10001&no_colors=1"
        ),
        (
            "http://zwave-01.ohana.lan:8123/api/hassio/addons/"
            "a0d7b954_zwavejs2mqtt/logs?lines=10001&no_colors=1"
        ),
    ]


@pytest.mark.parametrize("line_count", [9999, 10000, 10001])
def test_supervisor_line_cap_is_reported_even_below_byte_limit(monkeypatch, line_count):
    content = b"INFO normal activity\n" * line_count

    def response(request, **kwargs):
        assert "lines=10001" in request.full_url
        return io.BytesIO(content)

    monkeypatch.setattr("ohana_katsuyu.handlers.urlopen", response)
    from ohana_katsuyu.handlers import _DirectLogReader

    payload, truncated = _DirectLogReader._read_supervisor(
        "http://ha.test:8123", "private", [], 4 * 1024 * 1024, 5, verify_tls=True
    )
    assert len(payload.splitlines()) == min(line_count, 10000)
    assert truncated is (line_count > 10000)


@pytest.mark.parametrize("size", [4095, 4096, 4097])
def test_supervisor_byte_cap_distinguishes_exact_fit(monkeypatch, size):
    monkeypatch.setattr(
        "ohana_katsuyu.handlers.urlopen", lambda *a, **k: io.BytesIO(b"x" * size)
    )
    from ohana_katsuyu.handlers import _DirectLogReader

    payload, truncated = _DirectLogReader._read_supervisor(
        "http://ha.test:8123", "private", [], 4096, 5, verify_tls=True
    )
    assert len(payload) == min(size, 4096)
    assert truncated is (size > 4096)


def test_supervisor_fallback_is_incomplete_and_does_not_expose_error_secrets(
    monkeypatch,
):
    from ohana_katsuyu.handlers import _DirectLogReader

    def failure(*args, **kwargs):
        raise RuntimeError("access_token=privatePassword http://user:secret@ha.test/")

    monkeypatch.setattr(_DirectLogReader, "_read_supervisor", failure)
    monkeypatch.setattr(
        "ohana_katsuyu.handlers.urlopen",
        lambda *a, **k: io.BytesIO(b"INFO Core started"),
    )
    reader = _DirectLogReader(
        lambda *a: {
            "source": "zwave-01",
            "base_url": "http://ha.test:8123",
            "url": "http://ha.test:8123/api/hassio/core/logs/latest?lines=10000",
            "access_token": "privatePassword",
        }
    )
    lines, size, truncated = reader.read("zwave-01", 4096, _log_context())
    assert truncated is True
    assert size > 0
    assert "core log fallback" in str(lines)
    assert "privatePassword" not in str(lines)
    assert "secret" not in str(lines)


def _infra_source_tar(backup_id: str = "20260820T120000Z") -> bytes:
    output = io.BytesIO()
    descriptor = json.dumps(
        {
            "schema_version": 1,
            "backup_id": backup_id,
            "profile": "infra-01",
            "contents": [
                "etc/ohana-agent",
                "etc/ohana-vision",
                "etc/dnsmasq.d",
                "etc/chrony/chrony.conf",
                "var/lib/ohana-vision/vision.db",
            ],
        }
    ).encode()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for name in ("etc/ohana-agent", "etc/ohana-vision", "etc/dnsmasq.d"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, content in (
            ("etc/chrony/chrony.conf", b"pool example.test\n"),
            ("var/lib/ohana-vision/vision.db", b"SQLite format 3\x00"),
            ("ohana-backup/descriptor.json", descriptor),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def test_system_health_is_local_and_strict(tmp_path: Path) -> None:
    result = SystemHealthHandler(
        KatsuyuWorkspace(tmp_path),
        probe=FakeProbe(),  # type: ignore[arg-type]
        disk_usage=lambda _path: SimpleNamespace(total=1000, used=250, free=750),
    ).execute({})

    assert result["status"] == "OK"
    assert result["platform"] == "Windows 11"
    assert result["memory_available_bytes"] == 5 * 1024**3


def test_compress_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "incoming" / "backup.tar"
    source.parent.mkdir()
    source.write_bytes(b"ohana-backup" * 1000)
    handler = BackupCompressHandler(KatsuyuWorkspace(tmp_path))
    parameters = {
        "source": "incoming/backup.tar",
        "destination": "artifacts/backup.tar.gz",
        "compression_level": 6,
    }

    first = handler.execute(parameters)
    second = handler.execute(parameters)

    destination = tmp_path / "artifacts" / "backup.tar.gz"
    assert gzip.decompress(destination.read_bytes()) == source.read_bytes()
    assert first == second
    assert first["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_verify_reports_mismatch_as_a_structured_result(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.age"
    artifact.write_bytes(b"encrypted")
    result = BackupVerifyHandler(KatsuyuWorkspace(tmp_path)).execute(
        {
            "path": "artifact.age",
            "expected_sha256": "0" * 64,
            "expected_size": 10,
        }
    )

    assert result["valid"] is False
    assert result["sha256_matches"] is False
    assert result["size_matches"] is False


@pytest.mark.parametrize("value", ["../secret", "C:/Windows/secret", r"..\secret"])
def test_workspace_rejects_escape_paths(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="relative|escapes"):
        KatsuyuWorkspace(tmp_path).output_file(value)


def test_handler_stops_when_cancelled(tmp_path: Path) -> None:
    (tmp_path / "backup.tar").write_bytes(b"x" * 100)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(JobCancelledError):
        BackupCompressHandler(KatsuyuWorkspace(tmp_path)).execute(
            {"source": "backup.tar", "destination": "backup.tar.gz"},
            HandlerContext(cancelled=cancelled),
        )


def test_encrypt_uses_only_the_configured_age_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "backup.tar.gz").write_bytes(b"compressed")
    calls: list[list[str]] = []

    class FakeProcess:
        returncode = 0
        stderr = StringIO("")

        def __init__(self, command: list[str], **_kwargs: object) -> None:
            calls.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(b"encrypted")

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: int) -> int:
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr("ohana_katsuyu.handlers.subprocess.Popen", FakeProcess)
    recipient = "age1" + "q" * 58
    result = BackupEncryptHandler(
        KatsuyuWorkspace(tmp_path), Path(r"C:\Tools\age.exe")
    ).execute(
        {
            "source": "backup.tar.gz",
            "destination": "backup.tar.gz.age",
            "recipient": recipient,
        }
    )

    assert calls[0][0] == r"C:\Tools\age.exe"
    assert result["recipient"] == recipient
    assert result["destination_sha256"] == hashlib.sha256(b"encrypted").hexdigest()


def test_infra_backup_fetches_and_returns_only_a_verified_remote_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProcess:
        returncode = 0
        stderr = StringIO("")

        def __init__(self, command: list[str], **_kwargs: object) -> None:
            Path(command[command.index("--output") + 1]).write_bytes(b"encrypted")

        def poll(self) -> int:
            return 0

        def terminate(self) -> None:
            return None

        def wait(self, timeout: int) -> int:
            return 0

        def kill(self) -> None:
            return None

    class FakeTransfer:
        def download_job_input(
            self,
            job_id: str,
            worker_id: str,
            attempt: int,
            destination: Path,
            context: HandlerContext,
        ) -> tuple[str, int]:
            assert (job_id, worker_id, attempt) == ("job-1", "bubule", 2)
            context.check()
            content = _infra_source_tar()
            destination.write_bytes(content)
            return hashlib.sha256(content).hexdigest(), len(content)

        def upload_job_artifact(
            self,
            job_id: str,
            worker_id: str,
            attempt: int,
            source: Path,
            sha256: str,
            context: HandlerContext,
        ) -> dict[str, object]:
            context.check()
            assert source.read_bytes() == b"encrypted"
            assert sha256 == hashlib.sha256(b"encrypted").hexdigest()
            return {
                "remote_path": "icloud:Ohana/Backups/infra-01/20260820T120000Z",
                "sha256": sha256,
                "size_bytes": len(b"encrypted"),
                "deleted_remote_backups": 1,
            }

    monkeypatch.setattr("ohana_katsuyu.handlers.subprocess.Popen", FakeProcess)
    result = InfraBackupHandler(
        KatsuyuWorkspace(tmp_path),
        FakeTransfer(),  # type: ignore[arg-type]
        Path(r"C:\Tools\age.exe"),
    ).execute(
        {
            "backup_id": "20260820T120000Z",
            "recipient": "age1" + "q" * 58,
            "compression_level": 6,
        },
        HandlerContext(job_id="job-1", worker_id="bubule", attempt=2),
    )

    assert result["backup_id"] == "20260820T120000Z"
    assert result["size_bytes"] == len(b"encrypted")
    assert not (tmp_path / "jobs" / "job-1").exists()


def test_infra_backup_rejects_truncated_source_before_encryption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeTransfer:
        def download_job_input(
            self,
            _job_id: str,
            _worker_id: str,
            _attempt: int,
            destination: Path,
            _context: HandlerContext,
        ) -> tuple[str, int]:
            content = _infra_source_tar().rstrip(b"\0")
            destination.write_bytes(content)
            return hashlib.sha256(content).hexdigest(), len(content)

        def upload_job_artifact(
            self, *_args: object, **_kwargs: object
        ) -> dict[str, object]:
            raise AssertionError("an incomplete source must never be uploaded")

    popen_called = False

    def unexpected_popen(*_args: object, **_kwargs: object) -> object:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("an incomplete source must never be encrypted")

    monkeypatch.setattr("ohana_katsuyu.handlers.subprocess.Popen", unexpected_popen)
    with pytest.raises(ValueError, match="complete tar trailer"):
        InfraBackupHandler(
            KatsuyuWorkspace(tmp_path),
            FakeTransfer(),  # type: ignore[arg-type]
            Path(r"C:\Tools\age.exe"),
        ).execute(
            {
                "backup_id": "20260820T120000Z",
                "recipient": "age1" + "q" * 58,
                "compression_level": 6,
            },
            HandlerContext(job_id="job-1", worker_id="bubule", attempt=1),
        )
    assert not popen_called


def _inline_health(source: str, lines: list[str], started: str, ended: str):
    def provider(*_args) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": source,
            "transport": "inline",
            "content": "\n".join(lines),
            "truncated": False,
        }

    return LogsHealthCheckHandler(provider).execute(
        {
            "sources": [source],
            "window_started_at": started,
            "window_ended_at": ended,
            "max_bytes_per_source": 65536,
            "baseline": [],
            "incident_id": None,
        },
        _log_context(),
    )["sources"][0]


def test_clock_only_addon_lines_are_dated_and_windowed() -> None:
    # LINKY-01, 25 September: 8 of 11 teleinfo2mqtt findings had no date.
    source = _inline_health(
        "linky-01",
        [
            "12:52:10.123 WARN teleinfo2mqtt: mqtt connection error (read econnreset)",
            "12:52:40.456 WARN teleinfo2mqtt: mqtt connection error (read econnreset)",
            "09:00:00.000 WARN teleinfo2mqtt: unable to publish frame to "
            "ohana-agent [http://192.168.1.10:8770]",
        ],
        "2026-09-25T14:00:00+02:00",
        "2026-09-25T15:00:00+02:00",
    )
    assert source["analyzed_lines"] == 2  # 09:00 is outside the window.
    [finding] = source["findings"]
    assert finding["occurrences"] == 2
    assert finding["first_at"] == "2026-09-25T12:52:10Z"
    assert finding["last_at"] == "2026-09-25T12:52:40Z"
    assert finding["category"] == "mqtt"


def test_linky_categories_name_the_failing_layer_not_the_addon() -> None:
    source = _inline_health(
        "linky-01",
        [
            "2026-09-25T14:52:10+02:00 WARN teleinfo2mqtt: unable to publish "
            "frame to ohana-agent [http://192.168.1.10:8770] failed",
            "2026-09-25T14:52:11+02:00 ERROR teleinfo2mqtt: checksum invalid",
        ],
        "2026-09-25T14:00:00+02:00",
        "2026-09-25T15:00:00+02:00",
    )
    categories = {f["signature"][:60]: f["category"] for f in source["findings"]}
    assert sorted(categories.values()) == ["network", "serial"]


def test_traceback_continuation_belongs_to_its_timestamped_record() -> None:
    # HA-01, 25 September: 10 dateless findings were traceback frames, chained
    # exception headers and template text split from their error line.
    source = _inline_health(
        "ha-01",
        [
            "2026-09-25 12:40:00.123 ERROR (MainThread) [homeassistant] "
            "Error doing job: Future exception was never retrieved",
            "Traceback (most recent call last):",
            '  File "/usr/src/homeassistant/core.py", line 10, in run',
            "ValueError: could not convert string to float: 'unavailable'",
            "During handling of the above exception, another exception occurred:",
            "homeassistant.exceptions.TemplateError: ValueError: template error",
            "{{ total_offset + mesure_actuelle }}",
        ],
        "2026-09-25T14:00:00+02:00",
        "2026-09-25T15:00:00+02:00",
    )
    assert source["analyzed_lines"] == 7
    [finding] = source["findings"]
    assert finding["occurrences"] == 1
    assert finding["last_at"] == "2026-09-25T12:40:00.123000Z"


def test_dateless_s6_lines_are_not_swallowed_as_continuations() -> None:
    source = _inline_health(
        "ha-01",
        [
            "2026-09-25 12:40:00 ERROR (MainThread) [x] failed",
            "s6-rc: info: service example: starting",
        ],
        "2026-09-25T14:00:00+02:00",
        "2026-09-25T15:00:00+02:00",
    )
    assert len(source["findings"]) == 2
