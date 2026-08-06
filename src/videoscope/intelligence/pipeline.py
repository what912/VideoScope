"""Local Advanced AI preparation built on the deterministic C evidence layer."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from videoscope.ai import DevicePreference, ModelRuntimeConfig, ModelRuntimeManager
from videoscope.content import (
    ContentConfig,
    ContentGoal,
    ContentPipelineConfig,
    ContentPreparation,
    LongVideoContentPipeline,
)
from videoscope.content.transcript import load_timed_transcript
from videoscope.intelligence.models import (
    AIReviewDecision,
    AIReviewManifest,
    AISuggestionBatch,
    AITranscript,
)
from videoscope.intelligence.protocols import ASRProvider, ContentIntelligenceProvider
from videoscope.intelligence.runtime import (
    get_asr_provider,
    get_content_intelligence_provider,
    register_faster_whisper_provider,
    register_ollama_provider,
)
from videoscope.intelligence.serialization import write_intelligence_json
from videoscope.intelligence.service import (
    build_intelligence_request,
    build_review_manifest,
    normalize_asr_transcript,
    normalize_trusted_transcript,
    run_content_intelligence,
)


class AdvancedAIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output_directory: Path = Path("videoscope-ai-output")
    transcript_path: Path | None = None
    asr_model_id: str = "small"
    asr_language: str | None = None
    semantic_model_id: str
    ollama_endpoint: str = "http://127.0.0.1:11434"
    locale: Literal["en", "zh-CN"] = "en"
    device: DevicePreference = DevicePreference.AUTO
    allow_model_download: bool = False
    maximum_suggestions: int = Field(default=24, ge=1, le=200)
    keep_workspace: bool = False
    cancellation_callback: Callable[[], bool] | None = Field(default=None, exclude=True)


class AdvancedAICancelledError(RuntimeError):
    """An explicit user cancellation stopped AI orchestration at a safe boundary."""


@dataclass(frozen=True, slots=True)
class AdvancedAIPreparation:
    transcript: AITranscript
    suggestions: AISuggestionBatch
    private_root: Path
    cpu_map_digest: str
    cpu_warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdvancedAIReview:
    preparation: AdvancedAIPreparation
    manifest: AIReviewManifest


@dataclass(slots=True)
class AdvancedAIDependencies:
    runtime_factory: Callable[[ModelRuntimeConfig], ModelRuntimeManager] = (
        ModelRuntimeManager
    )
    content_pipeline_factory: Callable[
        [ContentPipelineConfig], ContentPreparationService
    ] = LongVideoContentPipeline
    asr_provider: ASRProvider | None = None
    content_provider: ContentIntelligenceProvider | None = None


class ContentPreparationService(Protocol):
    def prepare(self, input_path: Path) -> ContentPreparation: ...
    def close(self) -> None: ...


class AdvancedAIContentPipeline:
    """Prepare grounded private AI suggestions without changing source media."""

    def __init__(
        self,
        config: AdvancedAIConfig,
        *,
        dependencies: AdvancedAIDependencies | None = None,
    ) -> None:
        self.config = config
        self.dependencies = dependencies or AdvancedAIDependencies()
        self._model_runtime: ModelRuntimeManager | None = None

    def prepare(self, input_path: Path) -> AdvancedAIPreparation:
        source = Path(input_path)
        if not source.is_file():
            raise ValueError("source video does not exist")
        private_root = self.config.output_directory / "ai-review-private"
        private_root.mkdir(parents=True, exist_ok=True)
        content_pipeline = self.dependencies.content_pipeline_factory(
            ContentPipelineConfig(
                output_directory=self.config.output_directory / "cpu-evidence",
                transcript_path=self.config.transcript_path,
                keep_workspace=True,
                content=ContentConfig(goal=ContentGoal.CHAPTERED_FULL),
            )
        )
        try:
            self._check_cancelled()
            cpu = content_pipeline.prepare(source)
            self._check_cancelled()
            transcript = self._transcript(source, cpu.metadata.duration_seconds)
            self._check_cancelled()
            provider = self.dependencies.content_provider
            if provider is None:
                runtime = self._runtime()
                register_ollama_provider(
                    runtime,
                    model_id=self.config.semantic_model_id,
                    endpoint=self.config.ollama_endpoint,
                )
                provider = get_content_intelligence_provider(
                    runtime, "ollama", self.config.semantic_model_id
                )
            elif not provider.health().status.value == "ready":
                provider.load()
            request = build_intelligence_request(
                cpu.content_map,
                transcript,
                locale=self.config.locale,
                maximum_suggestions=self.config.maximum_suggestions,
            )
            batch = run_content_intelligence(
                provider,
                request,
                effective_parameters={
                    "locale": self.config.locale,
                    "maximum_suggestions": self.config.maximum_suggestions,
                    "temperature": 0,
                    "seed": 0,
                },
            )
            self._check_cancelled()
            write_intelligence_json(transcript, private_root / "transcript.json")
            write_intelligence_json(batch, private_root / "suggestions.json")
            return AdvancedAIPreparation(
                transcript=transcript,
                suggestions=batch,
                private_root=private_root,
                cpu_map_digest=cpu.content_map.map_digest,
                cpu_warnings=cpu.warnings,
            )
        except BaseException:
            if not self.config.keep_workspace:
                shutil.rmtree(private_root, ignore_errors=True)
            raise
        finally:
            content_pipeline.close()
            cpu_root = self.config.output_directory / "cpu-evidence"
            if not self.config.keep_workspace:
                shutil.rmtree(cpu_root, ignore_errors=True)

    def review(
        self,
        preparation: AdvancedAIPreparation,
        decisions: tuple[AIReviewDecision, ...],
    ) -> AdvancedAIReview:
        manifest = build_review_manifest(preparation.suggestions, decisions)
        write_intelligence_json(
            manifest, preparation.private_root / "review-manifest.json"
        )
        return AdvancedAIReview(preparation=preparation, manifest=manifest)

    def _transcript(self, input_path: Path, duration_seconds: float) -> AITranscript:
        if self.config.transcript_path is not None:
            trusted = load_timed_transcript(
                self.config.transcript_path,
                duration_seconds=duration_seconds,
            )
            return normalize_trusted_transcript(trusted)
        provider = self.dependencies.asr_provider
        if provider is None:
            runtime = self._runtime()
            register_faster_whisper_provider(
                runtime,
                model_id=self.config.asr_model_id,
                language=self.config.asr_language,
            )
            provider = get_asr_provider(
                runtime, "faster_whisper", self.config.asr_model_id
            )
        elif not provider.health().status.value == "ready":
            provider.load()
        transcript, _execution = normalize_asr_transcript(
            provider,
            input_path,
            duration_seconds=duration_seconds,
        )
        return transcript

    def _runtime(self) -> ModelRuntimeManager:
        if self._model_runtime is None:
            self._model_runtime = self.dependencies.runtime_factory(
                ModelRuntimeConfig(
                    device=self.config.device,
                    allow_model_download=self.config.allow_model_download,
                    interactive=False,
                )
            )
        return self._model_runtime

    def _check_cancelled(self) -> None:
        callback = self.config.cancellation_callback
        if callback is not None and callback():
            raise AdvancedAICancelledError("Advanced AI preparation was cancelled")


__all__ = [
    "AdvancedAIConfig",
    "AdvancedAICancelledError",
    "AdvancedAIContentPipeline",
    "AdvancedAIDependencies",
    "AdvancedAIPreparation",
    "AdvancedAIReview",
]
