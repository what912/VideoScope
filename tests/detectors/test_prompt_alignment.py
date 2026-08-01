"""Tests for optional scene-level prompt/frame similarity diagnostics."""

from __future__ import annotations

from pathlib import Path

from videoscope.ai import (
    MODEL_RUNTIME_CACHE_KEY,
    Device,
    DevicePreference,
    EmbeddingCache,
    FakeEmbeddingProvider,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    ModelSpec,
    Precision,
)
from videoscope.detectors import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
    AnalysisContext,
    DetectorRegistry,
    DetectorRunner,
    PromptAlignmentDetector,
)
from videoscope.domain import DetectorStatus
from videoscope.scenes import VideoScene

from .helpers import make_image_context

PROMPT = "A red car driving through snow"
FAKE_PROVIDER_ID = "fake"
FAKE_MODEL_ID = "prompt-test-v1"
FAKE_PREPROCESSING_VERSION = "test-pixels-v1"


def _scenes() -> tuple[VideoScene, ...]:
    return tuple(
        VideoScene(
            scene_index=index,
            start_seconds=float(index),
            end_seconds=float(index + 1),
            duration_seconds=1.0,
            representative_timestamp=index + 0.5,
        )
        for index in range(3)
    )


def _runtime_for_scene_vectors(
    tmp_path: Path,
    context: AnalysisContext,
    scene_vectors: tuple[tuple[float, float], ...],
    *,
    fail_encode: bool = False,
) -> tuple[ModelRuntimeManager, list[FakeEmbeddingProvider]]:
    samples = context.frame_samples
    workspace = context.workspace
    overrides: dict[bytes, tuple[float, float]] = {PROMPT.encode("utf-8"): (1.0, 0.0)}
    for sample in samples:
        scene_index = min(int(sample.timestamp_seconds), len(scene_vectors) - 1)
        payload = (workspace / sample.relative_path).read_bytes()
        overrides[payload] = scene_vectors[scene_index]
    providers: list[FakeEmbeddingProvider] = []
    config = ModelRuntimeConfig(
        device=DevicePreference.CPU,
        disk_cache_directory=tmp_path / "embedding cache",
    )
    runtime = ModelRuntimeManager(
        config,
        cache=EmbeddingCache(
            memory_budget_bytes=config.memory_budget_bytes,
            disk_directory=config.disk_cache_directory,
        ),
        cuda_available=lambda: False,
    )
    spec = ModelSpec(
        provider_id=FAKE_PROVIDER_ID,
        model_id=FAKE_MODEL_ID,
        preprocessing_version=FAKE_PREPROCESSING_VERSION,
    )

    def factory(device: Device, precision: Precision) -> FakeEmbeddingProvider:
        provider = FakeEmbeddingProvider(
            device,
            precision,
            model_id=FAKE_MODEL_ID,
            dimension=2,
            fail_encode=fail_encode,
            payload_vectors=overrides,
        )
        providers.append(provider)
        return provider

    runtime.register(spec, factory)
    return runtime, providers


def _context(
    tmp_path: Path,
    scene_vectors: tuple[tuple[float, float], ...],
    *,
    prompt: str | None = PROMPT,
    fail_encode: bool = False,
) -> tuple[AnalysisContext, ModelRuntimeManager, list[FakeEmbeddingProvider]]:
    context = make_image_context(
        tmp_path,
        [10, 20, 30, 40, 50, 60],
        duration_seconds=3.0,
        scenes=_scenes(),
    )
    runtime, providers = _runtime_for_scene_vectors(
        tmp_path,
        context,
        scene_vectors,
        fail_encode=fail_encode,
    )
    return (
        context.model_copy(
            update={
                "prompt": prompt,
                "shared_cache": {MODEL_RUNTIME_CACHE_KEY: runtime},
            }
        ),
        runtime,
        providers,
    )


def _configuration(
    *,
    mode: str = "threshold",
    threshold: float | None = 0.5,
) -> dict[str, object]:
    config: dict[str, object] = {
        "mode": mode,
        "representative_frames_per_scene": 2,
        "provider_id": FAKE_PROVIDER_ID,
        "model_id": FAKE_MODEL_ID,
        "preprocessing_version": FAKE_PREPROCESSING_VERSION,
    }
    if threshold is not None:
        config["similarity_threshold"] = threshold
    return config


def test_high_similarity_scenes_produce_no_threshold_finding(
    tmp_path: Path,
) -> None:
    context, _, _ = _context(
        tmp_path,
        ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0)),
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])

    result = DetectorRunner(registry).run(
        context,
        configurations={"prompt_alignment": _configuration()},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.OK


def test_low_consecutive_scenes_merge_with_scores_and_lowest_evidence(
    tmp_path: Path,
) -> None:
    context, _, _ = _context(
        tmp_path,
        ((1.0, 0.0), (-1.0, 0.0), (-1.0, 0.0)),
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])

    result = DetectorRunner(registry).run(
        context,
        configurations={"prompt_alignment": _configuration()},
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.title == "Low prompt-frame similarity"
    assert finding.time_range.start_seconds == 1.0
    assert finding.time_range.end_seconds == 3.0
    assert finding.score == 1.0
    assert finding.evidence[0].metadata["prompt"] == PROMPT
    assert finding.evidence[0].metadata["mean_similarity"] == -1.0
    assert finding.evidence[0].metadata["minimum_similarity"] == -1.0
    assert finding.evidence[0].metadata["scene_indices"] == [1, 2]
    assert any(
        "not a complete semantic verification" in item for item in finding.limitations
    )
    assert any("negation, counts, and spatial" in item for item in finding.limitations)
    assert "Prompt violation confirmed" not in finding.title
    assert "Prompt violation confirmed" not in finding.description


def test_descriptive_mode_records_curve_and_lowest_scene_without_finding(
    tmp_path: Path,
) -> None:
    context, _, _ = _context(
        tmp_path,
        ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)),
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])

    result = DetectorRunner(registry).run(
        context,
        configurations={
            "prompt_alignment": _configuration(
                mode="descriptive",
                threshold=None,
            )
        },
    )

    assert result.findings == ()
    diagnostics = context.shared_cache[DETECTOR_DIAGNOSTICS_CACHE_KEY]
    assert diagnostics["prompt_alignment"]["mode"] == "descriptive"
    assert len(diagnostics["prompt_alignment"]["scenes"]) == 3
    assert diagnostics["prompt_alignment"]["lowest_scene"]["scene_index"] == 2


def test_missing_prompt_is_skipped_without_constructing_provider(
    tmp_path: Path,
) -> None:
    context, _, providers = _context(
        tmp_path,
        ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0)),
        prompt=None,
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])

    result = DetectorRunner(registry).run(
        context,
        configurations={"prompt_alignment": _configuration()},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.SKIPPED
    assert providers == []


def test_provider_failure_becomes_detector_error(tmp_path: Path) -> None:
    context, _, _ = _context(
        tmp_path,
        ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0)),
        fail_encode=True,
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])

    result = DetectorRunner(registry).run(
        context,
        configurations={"prompt_alignment": _configuration()},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.DETECTOR_ERROR
    assert result.executions[0].error_type == "ModelProviderExecutionError"


def test_repeated_detector_run_reuses_cached_frame_embeddings(
    tmp_path: Path,
) -> None:
    context, runtime, providers = _context(
        tmp_path,
        ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0)),
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])
    config = {"prompt_alignment": _configuration()}

    DetectorRunner(registry).run(context, configurations=config)
    DetectorRunner(registry).run(context, configurations=config)

    assert len(providers) == 1
    assert providers[0].load_count == 1
    assert providers[0].image_encode_calls == 1
    image_records = [
        record for record in runtime.records() if record.operation == "encode_images"
    ]
    assert image_records[1].cache_hit_rate == 1.0


def test_threshold_mode_requires_explicit_user_threshold(tmp_path: Path) -> None:
    context, _, providers = _context(
        tmp_path,
        ((1.0, 0.0), (1.0, 0.0), (1.0, 0.0)),
    )
    registry = DetectorRegistry([PromptAlignmentDetector()])

    result = DetectorRunner(registry).run(
        context,
        configurations={
            "prompt_alignment": _configuration(
                mode="threshold",
                threshold=None,
            )
        },
    )

    assert result.executions[0].status is DetectorStatus.DETECTOR_ERROR
    assert result.executions[0].error_type == "ValidationError"
    assert providers == []
