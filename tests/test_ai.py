"""Tests for the bounded, on-demand Phase 9 inference handler."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from pydantic import ValidationError

from ohana_katsuyu.ai import DIAGNOSTIC_SCHEMA, AiInferenceHandler
from ohana_katsuyu.handlers import HandlerContext
from ohana_katsuyu.models import AiInferenceParameters, AiInferenceResult

MODEL_SHA256 = "fe08ca2158cd7438211ec6a4e5256d31bc980f016e3f5b635fe91fe6848d461c"


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
            assert payload["messages"][1]["content"].startswith(
                "EVIDENCE source='HA-01'"
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
