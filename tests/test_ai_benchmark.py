"""Tests for the Phase 9 benchmark scoring and safety contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_ai.py"
SPEC = importlib.util.spec_from_file_location("benchmark_ai", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark_ai = importlib.util.module_from_spec(SPEC)
sys.modules["benchmark_ai"] = benchmark_ai
SPEC.loader.exec_module(benchmark_ai)


def test_complete_diagnosis_scores_one_hundred() -> None:
    case = {
        "expected_verdict": "KO",
        "required_terms": ["node 37", "neighbor"],
        "forbidden_terms": ["hardware failure"],
    }
    document = {
        "verdict": "KO",
        "summary": "Node 37 is unreachable; neighbor data is missing.",
        "findings": [],
        "missing_context": ["neighbor table"],
        "recommended_investigation": ["Collect neighbor table"],
    }

    result = benchmark_ai.score_diagnosis(case, {"content": json.dumps(document)})

    assert result["score"] == 100
    assert result["schema_errors"] == []


def test_hallucination_and_wrong_verdict_are_penalised() -> None:
    case = {
        "expected_verdict": "INSUFFICIENT_CONTEXT",
        "required_terms": ["target"],
        "forbidden_terms": ["router failure"],
    }
    document = {
        "verdict": "KO",
        "summary": "Confirmed router failure.",
        "findings": [],
        "missing_context": [],
        "recommended_investigation": [],
    }

    result = benchmark_ai.score_diagnosis(case, {"content": json.dumps(document)})

    assert result["score"] == 25
    assert result["forbidden_hits"] == ["router failure"]


def test_tool_score_never_treats_text_as_an_executed_tool() -> None:
    case = {"expected_tool": None, "expected_arguments": {}}
    response = {"tool_calls": [], "content": "I would run a shell command"}

    assert benchmark_ai.score_tool(case, response)["score"] == 100


def test_tool_score_requires_allowlisted_name_and_arguments() -> None:
    case = {
        "expected_tool": "request_targeted_logs",
        "expected_arguments": {"source": "HA-01"},
    }
    response = {
        "tool_calls": [
            {
                "name": "request_targeted_logs",
                "arguments": {"source": "HA-01", "start": "09:40Z", "end": "09:45Z"},
            }
        ]
    }

    assert benchmark_ai.score_tool(case, response)["score"] == 100


def test_long_context_stress_is_deterministic_and_bounded() -> None:
    case = {
        "context": "tail anomaly",
        "stress": {"position": "before", "count": 3, "line": "normal {index}"},
    }

    assert benchmark_ai.expanded_context(case) == (
        "normal 0\nnormal 1\nnormal 2\ntail anomaly"
    )
