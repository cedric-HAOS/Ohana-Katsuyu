"""Tests for verified and resumable local-AI provisioning."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from ohana_katsuyu import ai_install
from ohana_katsuyu.ai_install import DownloadArtifact


class FakeResponse(io.BytesIO):
    def __init__(self, value: bytes, status: int, headers: dict[str, str]) -> None:
        super().__init__(value)
        self.status = status
        self.headers = headers

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_download_verified_resumes_a_matching_http_range(
    tmp_path: Path, monkeypatch: Any
) -> None:
    content = b"verified-payload"
    artifact = DownloadArtifact(
        "payload.bin",
        "https://example.invalid/payload.bin",
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
    partial = tmp_path / "payload.bin.part"
    partial.write_bytes(content[:5])
    requests: list[Any] = []

    def fake_urlopen(request: Any, *, timeout: int) -> FakeResponse:
        requests.append((request, timeout))
        return FakeResponse(
            content[5:],
            206,
            {"Content-Range": f"bytes 5-{len(content) - 1}/{len(content)}"},
        )

    monkeypatch.setattr(ai_install.urllib.request, "urlopen", fake_urlopen)

    result = ai_install.download_verified(artifact, tmp_path)

    assert result.read_bytes() == content
    assert requests[0][0].get_header("Range") == "bytes=5-"
    assert requests[0][1] == 60
    assert not partial.exists()


def test_download_verified_removes_a_payload_with_wrong_hash(
    tmp_path: Path, monkeypatch: Any
) -> None:
    artifact = DownloadArtifact(
        "payload.bin",
        "https://example.invalid/payload.bin",
        hashlib.sha256(b"expected").hexdigest(),
        len(b"invalid!"),
    )
    monkeypatch.setattr(
        ai_install.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"invalid!", 200, {}),
    )

    with pytest.raises(RuntimeError, match="SHA-256"):
        ai_install.download_verified(artifact, tmp_path)

    assert not (tmp_path / "payload.bin.part").exists()
    assert not (tmp_path / "payload.bin").exists()


def test_safe_extract_rejects_a_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("../outside.exe", b"malicious")

    with pytest.raises(RuntimeError, match="chemin non autorisé"):
        ai_install._safe_extract_zip(archive, tmp_path / "runtime")

    assert not (tmp_path / "outside.exe").exists()


def test_provision_ai_activates_verified_payload_and_reuses_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    runtime_zip = tmp_path / "runtime.zip"
    cuda_zip = tmp_path / "cuda.zip"
    model_source = tmp_path / "model.download"
    with zipfile.ZipFile(runtime_zip, "w") as output:
        output.writestr("llama-server.exe", b"server")
        output.writestr("llama.dll", b"runtime")
    with zipfile.ZipFile(cuda_zip, "w") as output:
        output.writestr("cudart.dll", b"cuda")
    model_content = b"small-test-model"
    model_source.write_bytes(model_content)
    model_artifact = DownloadArtifact(
        "model.gguf",
        "https://example.invalid/model.gguf",
        hashlib.sha256(model_content).hexdigest(),
        len(model_content),
    )
    monkeypatch.setattr(ai_install, "RUNTIME_BUILD", "test-build")
    monkeypatch.setattr(ai_install, "MODEL_FILENAME", "model.gguf")
    monkeypatch.setattr(ai_install, "MODEL_ID", "test-model")
    monkeypatch.setattr(ai_install, "MODEL_SHA256", model_artifact.sha256)
    monkeypatch.setattr(ai_install, "MODEL_SIZE", model_artifact.size)
    monkeypatch.setattr(ai_install, "MODEL_ARTIFACT", model_artifact)
    sources = {
        ai_install.RUNTIME_ARTIFACT.name: runtime_zip,
        ai_install.CUDA_ARTIFACT.name: cuda_zip,
        model_artifact.name: model_source,
    }
    calls: list[str] = []

    def fake_download(
        artifact: DownloadArtifact,
        _directory: Path,
        _on_progress: Any = None,
    ) -> Path:
        calls.append(artifact.name)
        return sources[artifact.name]

    monkeypatch.setattr(ai_install, "download_verified", fake_download)

    installed = ai_install.provision_ai(tmp_path / "state")
    reused = ai_install.provision_ai(tmp_path / "state")

    assert installed == reused
    assert installed.runtime.name == "KatsuyuAiServer.exe"
    assert installed.runtime.read_bytes() == b"server"
    assert installed.model.read_bytes() == model_content
    assert calls == [
        ai_install.RUNTIME_ARTIFACT.name,
        ai_install.CUDA_ARTIFACT.name,
        model_artifact.name,
    ]
