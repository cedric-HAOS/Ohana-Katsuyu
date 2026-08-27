"""Verified, resumable provisioning of Katsuyu's optional local AI payload."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ohana_katsuyu import __version__

MODEL_ID = "ministral-3-14b-reasoning-2512-q4-k-m"
MODEL_FILENAME = "Ministral-3-14B-Reasoning-2512-Q4_K_M.gguf"
MODEL_URL = (
    "https://huggingface.co/mistralai/"
    "Ministral-3-14B-Reasoning-2512-GGUF/resolve/main/"
    f"{MODEL_FILENAME}?download=true"
)
MODEL_SHA256 = "fe08ca2158cd7438211ec6a4e5256d31bc980f016e3f5b635fe91fe6848d461c"
MODEL_SIZE = 8_239_591_488
RUNTIME_BUILD = "b10545"
RUNTIME_FILENAME = f"llama-{RUNTIME_BUILD}-bin-win-cuda-13.3-x64.zip"
RUNTIME_URL = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_BUILD}/"
    f"{RUNTIME_FILENAME}"
)
RUNTIME_SHA256 = "59e053f64837d6da766708272f6b814ddcf8286acf39e3a4632fcc535a3fa1b5"
RUNTIME_SIZE = 146_881_007
CUDA_FILENAME = "cudart-llama-bin-win-cuda-13.3-x64.zip"
CUDA_URL = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_BUILD}/"
    f"{CUDA_FILENAME}"
)
CUDA_SHA256 = "1462a050eb4c684921ba51dcc4cc488a036674c3e73e9945ee705b854808d03e"
CUDA_SIZE = 390_970_417
CONTEXT_SIZE = 32768
AI_SERVER_FILENAME = "KatsuyuAiServer.exe"

ProgressCallback = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    name: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class AiInstallation:
    runtime: Path
    model: Path
    model_id: str = MODEL_ID
    model_sha256: str = MODEL_SHA256
    context_size: int = CONTEXT_SIZE


RUNTIME_ARTIFACT = DownloadArtifact(
    RUNTIME_FILENAME, RUNTIME_URL, RUNTIME_SHA256, RUNTIME_SIZE
)
CUDA_ARTIFACT = DownloadArtifact(CUDA_FILENAME, CUDA_URL, CUDA_SHA256, CUDA_SIZE)
MODEL_ARTIFACT = DownloadArtifact(MODEL_FILENAME, MODEL_URL, MODEL_SHA256, MODEL_SIZE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_file(path: Path, artifact: DownloadArtifact) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == artifact.size
        and _sha256(path) == artifact.sha256
    )


def download_verified(
    artifact: DownloadArtifact,
    directory: Path,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Download one pinned artifact, resuming only a valid HTTP byte range."""
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / artifact.name
    if _verified_file(destination, artifact):
        if on_progress is not None:
            on_progress(artifact.name, artifact.size, artifact.size)
        return destination
    destination.unlink(missing_ok=True)
    partial = directory / f"{artifact.name}.part"
    received = partial.stat().st_size if partial.is_file() else 0
    if received > artifact.size:
        partial.unlink()
        received = 0
    headers = {"User-Agent": f"Ohana-Katsuyu/{__version__}"}
    if received:
        headers["Range"] = f"bytes={received}-"
    request = urllib.request.Request(artifact.url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = int(getattr(response, "status", response.getcode()))
            if received and status == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {received}-"):
                    raise RuntimeError("La reprise HTTP a retourné une plage invalide.")
                mode = "ab"
            elif status == 200:
                received = 0
                mode = "wb"
            else:
                raise RuntimeError(f"Le téléchargement IA a retourné HTTP {status}.")
            with partial.open(mode) as output:
                while chunk := response.read(8 * 1024 * 1024):
                    output.write(chunk)
                    received += len(chunk)
                    if received > artifact.size:
                        raise RuntimeError(
                            "Le téléchargement IA dépasse la taille attendue."
                        )
                    if on_progress is not None:
                        on_progress(artifact.name, received, artifact.size)
    except (OSError, urllib.error.URLError) as error:
        raise RuntimeError(f"Le téléchargement IA a échoué : {error}") from error
    if received != artifact.size:
        raise RuntimeError(
            f"Téléchargement IA incomplet : {received} sur {artifact.size} octets."
        )
    if _sha256(partial) != artifact.sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError("L’empreinte SHA-256 du téléchargement IA est invalide.")
    os.replace(partial, destination)
    return destination


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("L’archive IA contient un chemin non autorisé.")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("L’archive IA contient un lien symbolique.")
            target = destination.joinpath(*relative.parts).resolve()
            if not target.is_relative_to(resolved_destination):
                raise RuntimeError("L’archive IA sort du répertoire prévu.")
        source.extractall(destination)


def _manifest_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "runtime_build": RUNTIME_BUILD,
        "runtime": asdict(RUNTIME_ARTIFACT),
        "cuda": asdict(CUDA_ARTIFACT),
        "model": asdict(MODEL_ARTIFACT),
        "model_id": MODEL_ID,
        "context_size": CONTEXT_SIZE,
    }


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def provision_ai(
    state_root: Path,
    on_progress: ProgressCallback | None = None,
) -> AiInstallation:
    """Install the pinned runtime and model atomically below Katsuyu state."""
    ai_root = state_root / "ai"
    runtime_root = ai_root / "runtime" / RUNTIME_BUILD
    model_root = ai_root / "models"
    model = model_root / MODEL_FILENAME
    runtime = runtime_root / AI_SERVER_FILENAME
    manifest = ai_root / "install.json"
    expected_manifest = _manifest_document()
    installed_manifest = _read_manifest(manifest)
    compatible_manifest = installed_manifest is not None and {
        key: value for key, value in installed_manifest.items() if key != "context_size"
    } == {
        key: value for key, value in expected_manifest.items() if key != "context_size"
    }
    if (
        compatible_manifest
        and runtime.is_file()
        and _verified_file(model, MODEL_ARTIFACT)
    ):
        if installed_manifest != expected_manifest:
            temporary_manifest = manifest.with_suffix(".json.new")
            temporary_manifest.write_text(
                json.dumps(expected_manifest, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary_manifest, manifest)
        if on_progress is not None:
            on_progress("IA locale déjà vérifiée", MODEL_SIZE, MODEL_SIZE)
        return AiInstallation(
            runtime=runtime,
            model=model,
            model_id=MODEL_ID,
            model_sha256=MODEL_SHA256,
            context_size=CONTEXT_SIZE,
        )

    downloads = ai_root / "downloads"
    runtime_archive = download_verified(RUNTIME_ARTIFACT, downloads, on_progress)
    cuda_archive = download_verified(CUDA_ARTIFACT, downloads, on_progress)
    model_download = download_verified(MODEL_ARTIFACT, downloads, on_progress)

    staging = ai_root / f".runtime-{RUNTIME_BUILD}-{uuid.uuid4().hex}"
    try:
        _safe_extract_zip(runtime_archive, staging)
        _safe_extract_zip(cuda_archive, staging)
        original_server = staging / "llama-server.exe"
        if not original_server.is_file():
            raise RuntimeError("llama-server.exe est absent du runtime vérifié.")
        original_server.rename(staging / AI_SERVER_FILENAME)
        runtime_root.parent.mkdir(parents=True, exist_ok=True)
        if runtime_root.exists():
            resolved_runtime = runtime_root.resolve()
            if not resolved_runtime.is_relative_to(ai_root.resolve()):
                raise RuntimeError("Le runtime IA sort du répertoire Katsuyu.")
            shutil.rmtree(runtime_root)
        os.replace(staging, runtime_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    model_root.mkdir(parents=True, exist_ok=True)
    if model.exists():
        model.unlink()
    os.replace(model_download, model)
    temporary_manifest = manifest.with_suffix(".json.new")
    temporary_manifest.write_text(
        json.dumps(expected_manifest, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest)
    runtime_archive.unlink(missing_ok=True)
    cuda_archive.unlink(missing_ok=True)
    return AiInstallation(
        runtime=runtime,
        model=model,
        model_id=MODEL_ID,
        model_sha256=MODEL_SHA256,
        context_size=CONTEXT_SIZE,
    )
