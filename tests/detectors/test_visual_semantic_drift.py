"""Tests for scene-relative visual semantic drift diagnostics."""

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
from videoscope.ai.providers import (
    DEFAULT_OPENCLIP_MODEL_ID,
    OPENCLIP_PREPROCESSING_VERSION,
)
from videoscope.detectors import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
    AnalysisContext,
    DetectorRegistry,
    DetectorRunner,
    PromptAlignmentDetector,
    VisualSemanticDriftConfig,
    VisualSemanticDriftDetector,
)
from videoscope.domain import DetectorStatus
from videoscope.scenes import VideoScene

from .helpers import make_image_context

FAKE_PROVIDER_ID = "fake"
FAKE_MODEL_ID = "shared-visual-v1"
FAKE_PREPROCESSING_VERSION = "shared-pixels-v1"
PROMPT = "steady visual content"


def _scene(
    index: int,
    start: float,
    end: float,
) -> VideoScene:
    return VideoScene(
        scene_index=index,
        start_seconds=start,
        end_seconds=end,
        duration_seconds=end - start,
        representative_timestamp=start + (end - start) / 2.0,
    )


def _runtime(
    tmp_path: Path,
    context: AnalysisContext,
    vectors: tuple[tuple[float, float], ...],
    *,
    prompt_vector: tuple[float, float] = (1.0, 0.0),
) -> tuple[ModelRuntimeManager, list[FakeEmbeddingProvider]]:
    overrides: dict[bytes, tuple[float, float]] = {
        PROMPT.encode("utf-8"): prompt_vector
    }
    for sample, vector in zip(context.frame_samples, vectors, strict=True):
        payload = (context.workspace / sample.relative_path).read_bytes()
        overrides[payload] = vector
    providers: list[FakeEmbeddingProvider] = []
    config = ModelRuntimeConfig(
        device=DevicePreference.CPU,
        batch_size=64,
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
    runtime.register(
        ModelSpec(
            provider_id=FAKE_PROVIDER_ID,
            model_id=FAKE_MODEL_ID,
            capabilities=("image_embedding", "text_embedding"),
            preprocessing_version=FAKE_PREPROCESSING_VERSION,
        ),
        lambda device, precision: _make_provider(
            providers,
            device=device,
            precision=precision,
            overrides=overrides,
        ),
    )
    return runtime, providers


def _make_provider(
    providers: list[FakeEmbeddingProvider],
    *,
    device: Device,
    precision: Precision,
    overrides: dict[bytes, tuple[float, float]],
) -> FakeEmbeddingProvider:
    provider = FakeEmbeddingProvider(
        device,
        precision,
        model_id=FAKE_MODEL_ID,
        dimension=2,
        payload_vectors=overrides,
    )
    providers.append(provider)
    return provider


def _context(
    tmp_path: Path,
    vectors: tuple[tuple[float, float], ...],
    *,
    scenes: tuple[VideoScene, ...] | None = None,
    prompt: str | None = None,
) -> tuple[AnalysisContext, ModelRuntimeManager, list[FakeEmbeddingProvider]]:
    colors = [10 + index * 10 for index in range(len(vectors))]
    duration = len(vectors) / 2.0
    effective_scenes = scenes or (_scene(0, 0.0, duration),)
    context = make_image_context(
        tmp_path,
        colors,
        duration_seconds=duration,
        scenes=effective_scenes,
    )
    runtime, providers = _runtime(tmp_path, context, vectors)
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


def _configuration() -> dict[str, object]:
    return {
        "provider_id": FAKE_PROVIDER_ID,
        "model_id": FAKE_MODEL_ID,
        "preprocessing_version": FAKE_PREPROCESSING_VERSION,
        "long_gap_seconds": 1.5,
        "scene_boundary_guard_seconds": 0.0,
        "minimum_scene_samples": 4,
        "minimum_baseline_pairs": 3,
        "baseline_mad_multiplier": 3.0,
        "minimum_distance_threshold": 0.2,
        "min_duration_seconds": 0.0,
        "merge_gap_seconds": 0.5,
    }


def test_stable_within_scene_sequence_produces_no_finding(
    tmp_path: Path,
) -> None:
    context, _, _ = _context(tmp_path, ((1.0, 0.0),) * 10)

    result = DetectorRunner(DetectorRegistry([VisualSemanticDriftDetector()])).run(
        context,
        configurations={"visual_semantic_drift": _configuration()},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.OK
    diagnostics = context.shared_cache[DETECTOR_DIAGNOSTICS_CACHE_KEY]
    scene = diagnostics["visual_semantic_drift"]["scenes"][0]
    assert scene["baseline"]["median_distance"] == 0.0
    assert scene["distance_series"]
    assert {item["comparison_type"] for item in scene["distance_series"]} == {
        "adjacent",
        "long_gap",
    }


def test_middle_embedding_jump_creates_neutral_finding_and_peak_evidence(
    tmp_path: Path,
) -> None:
    vectors = ((1.0, 0.0),) * 5 + ((0.0, 1.0),) + ((1.0, 0.0),) * 4
    context, _, _ = _context(tmp_path, vectors)

    result = DetectorRunner(DetectorRegistry([VisualSemanticDriftDetector()])).run(
        context,
        configurations={"visual_semantic_drift": _configuration()},
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.title == "Abrupt visual semantic drift"
    assert finding.time_range.start_seconds <= 2.5 <= finding.time_range.end_seconds
    assert [item.metadata["peak_frame_role"] for item in finding.evidence] == [
        "before",
        "after",
    ]
    assert finding.evidence[0].metadata["peak_distance"] == 1.0
    assert finding.evidence[0].metadata["scene_baseline_median"] == 0.0
    assert finding.evidence[0].metadata["model_id"] == FAKE_MODEL_ID
    assert any("Rapid camera movement" in item for item in finding.limitations)
    assert any("Large occlusion" in item for item in finding.limitations)
    assert any("Reasonable deformation" in item for item in finding.limitations)
    assert any("Large lighting changes" in item for item in finding.limitations)
    rendered = f"{finding.title} {finding.description}"
    assert "Character identity changed" not in rendered
    assert "Person replaced" not in rendered


def test_scene_cut_is_never_compared_across_scenes(tmp_path: Path) -> None:
    scenes = (_scene(0, 0.0, 2.5), _scene(1, 2.5, 5.0))
    vectors = ((1.0, 0.0),) * 5 + ((-1.0, 0.0),) * 5
    context, _, _ = _context(tmp_path, vectors, scenes=scenes)

    result = DetectorRunner(DetectorRegistry([VisualSemanticDriftDetector()])).run(
        context,
        configurations={"visual_semantic_drift": _configuration()},
    )

    assert result.findings == ()
    diagnostics = context.shared_cache[DETECTOR_DIAGNOSTICS_CACHE_KEY]
    scene_diagnostics = diagnostics["visual_semantic_drift"]["scenes"]
    assert {item["scene_index"] for item in scene_diagnostics} == {0, 1}
    for item in scene_diagnostics:
        for comparison in item["distance_series"]:
            assert comparison["distance"] == 0.0


def test_very_short_scene_skips_before_provider_construction(
    tmp_path: Path,
) -> None:
    context, _, providers = _context(
        tmp_path,
        ((1.0, 0.0),) * 3,
        scenes=(_scene(0, 0.0, 1.5),),
    )

    result = DetectorRunner(DetectorRegistry([VisualSemanticDriftDetector()])).run(
        context,
        configurations={"visual_semantic_drift": _configuration()},
    )

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.OK
    assert providers == []
    diagnostics = context.shared_cache[DETECTOR_DIAGNOSTICS_CACHE_KEY]
    assert diagnostics["visual_semantic_drift"]["scenes"] == []


def test_repeated_run_hits_embedding_cache(tmp_path: Path) -> None:
    context, runtime, providers = _context(tmp_path, ((1.0, 0.0),) * 10)
    registry = DetectorRegistry([VisualSemanticDriftDetector()])
    config = {"visual_semantic_drift": _configuration()}

    DetectorRunner(registry).run(context, configurations=config)
    DetectorRunner(registry).run(context, configurations=config)

    assert len(providers) == 1
    assert providers[0].load_count == 1
    assert providers[0].image_encode_calls == 1
    image_records = [
        record for record in runtime.records() if record.operation == "encode_images"
    ]
    assert image_records[1].encoded_items == 0
    assert image_records[1].cache_hit_rate == 1.0


def test_performance_shared_provider_does_not_reencode_prompt_frames(
    tmp_path: Path,
) -> None:
    context, runtime, providers = _context(
        tmp_path,
        ((1.0, 0.0),) * 10,
        prompt=PROMPT,
    )
    registry = DetectorRegistry(
        [PromptAlignmentDetector(), VisualSemanticDriftDetector()]
    )

    result = DetectorRunner(registry).run(
        context,
        detector_ids=("prompt_alignment", "visual_semantic_drift"),
        configurations={
            "prompt_alignment": {
                "mode": "descriptive",
                "representative_frames_per_scene": 3,
                "provider_id": FAKE_PROVIDER_ID,
                "model_id": FAKE_MODEL_ID,
                "preprocessing_version": FAKE_PREPROCESSING_VERSION,
            },
            "visual_semantic_drift": _configuration(),
        },
    )

    assert [item.status for item in result.executions] == [
        DetectorStatus.OK,
        DetectorStatus.OK,
    ]
    assert len(providers) == 1
    assert providers[0].load_count == 1
    image_records = [
        record for record in runtime.records() if record.operation == "encode_images"
    ]
    assert sum(record.encoded_items for record in image_records) == len(
        context.frame_samples
    )
    assert image_records[1].cache_hits == 3


def test_openclip_selection_resolves_matching_cache_identity() -> None:
    config = VisualSemanticDriftConfig(provider_id="openclip")

    assert config.model_id == DEFAULT_OPENCLIP_MODEL_ID
    assert config.preprocessing_version == OPENCLIP_PREPROCESSING_VERSION
