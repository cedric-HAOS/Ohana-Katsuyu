"""Phase 4: daily disk values rebuilt from Home Assistant statistics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ohana_katsuyu.handlers import (
    HANDLER_TYPES,
    HandlerContext,
    TrendsHistoryBackfillHandler,
)

# Summer time; Katsuyu has no tz database on Windows, nor do these tests.
PARIS = timezone(timedelta(hours=2))
START = datetime(2026, 9, 21, tzinfo=PARIS)
END = datetime(2026, 9, 28, tzinfo=PARIS)


class FakeHomeAssistant:
    """Answer the WebSocket exchange like Home Assistant 2026.x."""

    def __init__(self, rows, *, registry=None, token="ha-token"):
        self.rows = rows
        self.registry = (
            registry
            if registry is not None
            else [
                {"entity_id": "sensor.cpu", "platform": "mqtt", "unique_id": "cpu"},
                {
                    "entity_id": "sensor.ohana_host_utilisation_disque_racine",
                    "platform": "mqtt",
                    "unique_id": "ohana_host_disk_usage",
                },
            ]
        )
        self.token = token
        self.sent: list[dict] = []
        self.outbox = [{"type": "auth_required"}]
        self.url = None
        self.closed = False

    def __call__(self, url, **_options):
        self.url = url
        return self

    def recv(self):
        return json.dumps(self.outbox.pop(0))

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "auth":
            ok = message["access_token"] == self.token
            self.outbox.append({"type": "auth_ok" if ok else "auth_invalid"})
        elif message["type"] == "config/entity_registry/list":
            # An unrelated event arrives first: it must be skipped.
            self.outbox.append({"id": 99, "type": "event"})
            self.outbox.append(
                {"id": message["id"], "success": True, "result": self.registry}
            )
        elif message["type"] == "recorder/statistics_during_period":
            [entity] = message["statistic_ids"]
            self.outbox.append(
                {"id": message["id"], "success": True, "result": {entity: self.rows}}
            )

    def close(self):
        self.closed = True


def _hourly(day_values):
    """Hourly rows from Paris midnight, epoch milliseconds like HA sends."""
    rows = []
    for offset, (low, high) in enumerate(day_values):
        midnight = START + timedelta(days=offset)
        for hour in range(24):
            start = midnight + timedelta(hours=hour)
            value = high if hour == 15 else low
            rows.append(
                {
                    "start": start.astimezone(UTC).timestamp() * 1000,
                    "min": value - 0.1,
                    "max": value,
                    "mean": value - 0.05,
                }
            )
    return rows


def _run(fake):
    handler = TrendsHistoryBackfillHandler(
        lambda job, worker, attempt, source: {
            "schema_version": 1,
            "source": source,
            "base_url": "https://ha-01.ohana.lan:8123",
            "access_token": "ha-token",
            "verify_tls": True,
            "timeout_seconds": 30,
        },
        connect=fake,
    )
    return handler.execute(
        {
            "source": "ha-01",
            "node_id": "infra-01",
            "metric": "disk_percent",
            "unique_id": "ohana_host_disk_usage",
            "window_started_at": START.isoformat(),
            "window_ended_at": END.isoformat(),
        },
        HandlerContext(job_id="job-1", worker_id="katsuyu", attempt=1),
    )


def test_hourly_statistics_become_one_row_per_paris_day() -> None:
    fake = FakeHomeAssistant(
        _hourly([(70.0, 70.4), (71.0, 71.5), (72.0, 72.3)])
        # Outside the window: ignored.
        + [
            {
                "start": (END + timedelta(hours=1)).timestamp() * 1000,
                "min": 1,
                "max": 99,
                "mean": 50,
            }
        ]
    )

    result = _run(fake)

    assert fake.url == "wss://ha-01.ohana.lan:8123/api/websocket"
    assert fake.closed
    assert result["status"] == "OK"
    assert result["entity_id"] == "sensor.ohana_host_utilisation_disque_racine"
    assert result["rows_read"] == 73
    assert result["days"] == [
        {
            "day": "2026-09-21",
            "minimum": 69.9,
            "maximum": 70.4,
            "last": 69.95,
            "hours": 24,
        },
        {
            "day": "2026-09-22",
            "minimum": 70.9,
            "maximum": 71.5,
            "last": 70.95,
            "hours": 24,
        },
        {
            "day": "2026-09-23",
            "minimum": 71.9,
            "maximum": 72.3,
            "last": 71.95,
            "hours": 24,
        },
    ]
    statistics = next(
        m for m in fake.sent if m["type"] == "recorder/statistics_during_period"
    )
    assert statistics["period"] == "hour"
    assert statistics["statistic_ids"] == [
        "sensor.ohana_host_utilisation_disque_racine"
    ]


def test_an_unknown_sensor_returns_no_data_without_asking_statistics() -> None:
    fake = FakeHomeAssistant([], registry=[])

    result = _run(fake)

    assert result["status"] == "NO_DATA"
    assert result["entity_id"] is None
    assert result["days"] == []
    assert all(m["type"] != "recorder/statistics_during_period" for m in fake.sent)


def test_a_rejected_token_fails_the_job_and_closes_the_socket() -> None:
    fake = FakeHomeAssistant([], token="other")

    with pytest.raises(RuntimeError, match="rejected the history access"):
        _run(fake)
    assert fake.closed


def test_the_worker_declares_the_backfill_capability() -> None:
    assert "trends.history_backfill" in HANDLER_TYPES
