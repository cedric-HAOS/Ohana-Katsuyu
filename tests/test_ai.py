"""Tests for the bounded, on-demand Phase 9 inference handler."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from pydantic import ValidationError

from ohana_katsuyu.ai import (
    DIAGNOSTIC_SCHEMA,
    LLAMA_DIAGNOSTIC_SCHEMA,
    AiInferenceHandler,
)
from ohana_katsuyu.handlers import HandlerContext
from ohana_katsuyu.models import AiInferenceParameters, AiInferenceResult

MODEL_SHA256 = "fe08ca2158cd7438211ec6a4e5256d31bc980f016e3f5b635fe91fe6848d461c"


@pytest.mark.parametrize(
    "invalid",
    [
        {
            "code": "EVIDENCE source='logs.analysis'",
            "evidence": "bounded",
            "confidence": 0.8,
        },
        {"code": "NETWORK_ERROR", "evidence": "x" * 501, "confidence": 0.8},
    ],
)
def test_invalid_finding_is_regenerated_once_without_changing_evidence(
    tmp_path, monkeypatch, invalid
):
    calls = []
    valid = {"code": "NETWORK_ERROR", "evidence": "logs.analysis", "confidence": 0.8}

    def generate(self, base, request, context, *, repair_instruction=""):
        calls.append((request.model_dump(), repair_instruction))
        return {
            "document": {
                "verdict": "KO",
                "summary": "Erreur observée",
                "findings": [invalid if len(calls) == 1 else valid],
            },
            "metrics": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "ttft_ms": 1,
                "tokens_per_second": 5,
                "duration_seconds": 1,
            },
        }

    monkeypatch.setattr(AiInferenceHandler, "_stream_diagnostic", generate)
    handler = AiInferenceHandler(
        runtime=tmp_path / "runtime",
        model=tmp_path / "model",
        model_id="test",
        model_sha256=MODEL_SHA256,
    )
    result = handler._validated_diagnostic(
        "http://localhost",
        AiInferenceParameters.model_validate(parameters()),
        HandlerContext(),
    )
    assert result.verdict == "KO"
    assert result.findings[0].evidence == "logs.analysis"
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert "Regenerate" in calls[1][1]
    assert result.metrics.prompt_tokens == 20


def test_invalid_regeneration_stops_without_exposing_generated_content(
    tmp_path, monkeypatch
):
    calls = []

    def generate(*args, **kwargs):
        calls.append(1)
        return {
            "document": {
                "verdict": "KO",
                "summary": "Erreur",
                "findings": [
                    {
                        "code": "private user data",
                        "evidence": "secret",
                        "confidence": 0.8,
                    }
                ],
            },
            "metrics": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "ttft_ms": 1,
                "tokens_per_second": 5,
                "duration_seconds": 1,
            },
        }

    monkeypatch.setattr(AiInferenceHandler, "_stream_diagnostic", generate)
    handler = AiInferenceHandler(
        runtime=tmp_path / "runtime",
        model=tmp_path / "model",
        model_id="test",
        model_sha256=MODEL_SHA256,
    )
    with pytest.raises(RuntimeError, match="after one regeneration") as failure:
        handler._validated_diagnostic(
            "http://localhost",
            AiInferenceParameters.model_validate(parameters()),
            HandlerContext(),
        )
    assert len(calls) == 2
    assert "private" not in str(failure.value)
    assert "secret" not in str(failure.value)


def parameters() -> dict[str, object]:
    return {
        "task": "technical.diagnosis",
        "incident_id": "11111111-1111-4111-8111-111111111111",
        "question": "Qualifier l'état borné.",
        "evidence": [{"source": "HA-01", "content": "health=OK"}],
        "max_output_tokens": 512,
    }


def test_parameters_reject_unbounded_or_unknown_inputs() -> None:
    with pytest.raises(ValidationError):
        AiInferenceParameters.model_validate(
            {
                **parameters(),
                "evidence": [
                    {"source": f"node-{index}", "content": "x" * 7_000}
                    for index in range(8)
                ],
            }
        )
    with pytest.raises(ValidationError):
        AiInferenceParameters.model_validate({**parameters(), "shell": "dir"})
    AiInferenceParameters.model_validate({**parameters(), "max_output_tokens": 16_384})
    with pytest.raises(ValidationError):
        AiInferenceParameters.model_validate(
            {**parameters(), "max_output_tokens": 16_385}
        )


def test_result_enforces_ok_ko_and_missing_context_consistency() -> None:
    base = {
        "analysis_version": 2,
        "generated_at": "2026-08-21T10:00:00Z",
        "model_id": "ministral-3-14b-reasoning-2512-q4-k-m",
        "model_sha256": MODEL_SHA256,
        "interpretation": "Les éléments bornés indiquent un état normal.",
        "summary": "État borné normal.",
        "hypotheses": [],
        "missing_context": [],
        "recommended_investigation": [],
        "metrics": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "ttft_ms": 100,
            "tokens_per_second": 80,
            "duration_seconds": 0.5,
        },
    }

    AiInferenceResult.model_validate({**base, "verdict": "OK", "findings": []})
    with pytest.raises(ValidationError):
        AiInferenceResult.model_validate({**base, "verdict": "KO", "findings": []})
    with pytest.raises(ValidationError):
        AiInferenceResult.model_validate(
            {**base, "verdict": "INSUFFICIENT_CONTEXT", "findings": []}
        )

    legacy = {key: value for key, value in base.items() if key != "analysis_version"}
    legacy.pop("interpretation")
    validated = AiInferenceResult.model_validate(
        {
            **legacy,
            "verdict": "KO",
            "findings": [{"code": "LEGACY", "evidence": "bounded", "confidence": 0.5}],
            "hypotheses": [],
        }
    )
    assert validated.analysis_version == 1


def test_runtime_schema_requires_advanced_hypothesis_contract() -> None:
    assert DIAGNOSTIC_SCHEMA["properties"]["analysis_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "analysis_version" in DIAGNOSTIC_SCHEMA["required"]


def test_llama_runtime_schema_avoids_gbnf_incompatible_validation_hints() -> None:
    encoded_runtime = json.dumps(LLAMA_DIAGNOSTIC_SCHEMA)

    assert '"maxLength"' not in encoded_runtime
    assert '"minLength"' not in encoded_runtime
    assert '"pattern"' not in encoded_runtime
    assert DIAGNOSTIC_SCHEMA["properties"]["interpretation"]["maxLength"] == 2000
    assert (
        DIAGNOSTIC_SCHEMA["properties"]["findings"]["items"]["properties"]["code"][
            "pattern"
        ]
        == "^[A-Z0-9_.-]+$"
    )


def test_streaming_runtime_response_is_measured_and_parsed(tmp_path: Path) -> None:
    document = {
        "analysis_version": 2,
        "verdict": "OK",
        "interpretation": "Les éléments bornés indiquent un état normal.",
        "summary": "Intervalle borné normal.",
        "findings": [],
        "hypotheses": [],
        "missing_context": [],
        "recommended_investigation": [],
    }
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "check"}}]},
        {"choices": [{"delta": {"content": json.dumps(document)}}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 42, "completion_tokens": 21},
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"

    class ServerHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(size))
            assert payload["temperature"] == 0
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            assert payload["messages"][1]["content"].startswith(
                "EVIDENCE source='HA-01'"
            )
            assert "maxLength" not in json.dumps(
                payload["response_format"]["json_schema"]["schema"]
            )
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), ServerHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handler = AiInferenceHandler(
        runtime=tmp_path / "llama-server.exe",
        model=tmp_path / "model.gguf",
        model_id="ministral-3-14b-reasoning-2512-q4-k-m",
        model_sha256=MODEL_SHA256,
    )
    try:
        generated = handler._stream_diagnostic(
            f"http://127.0.0.1:{server.server_address[1]}",
            AiInferenceParameters.model_validate(parameters()),
            HandlerContext(),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert generated["document"] == document
    assert generated["metrics"]["prompt_tokens"] == 42
    assert generated["metrics"]["completion_tokens"] == 21
    assert generated["metrics"]["ttft_ms"] >= 0


def test_completion_budget_preserves_room_for_bounded_evidence(tmp_path: Path) -> None:
    handler = AiInferenceHandler(
        runtime=tmp_path / "llama-server.exe",
        model=tmp_path / "model.gguf",
        model_id="ministral-3-14b-reasoning-2512-q4-k-m",
        model_sha256=MODEL_SHA256,
        context_size=4_096,
    )

    assert handler._completion_token_budget("x" * 4_000, 4_096) == 1_072


def test_streaming_runtime_reports_output_token_truncation(tmp_path: Path) -> None:
    chunks = [
        {"choices": [{"delta": {"content": '{"analysis_version":2'}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"

    class ServerHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            size = int(self.headers["Content-Length"])
            self.rfile.read(size)
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), ServerHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handler = AiInferenceHandler(
        runtime=tmp_path / "llama-server.exe",
        model=tmp_path / "model.gguf",
        model_id="ministral-3-14b-reasoning-2512-q4-k-m",
        model_sha256=MODEL_SHA256,
    )
    try:
        with pytest.raises(RuntimeError, match="truncated at the output token limit"):
            handler._stream_diagnostic(
                f"http://127.0.0.1:{server.server_address[1]}",
                AiInferenceParameters.model_validate(parameters()),
                HandlerContext(),
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
