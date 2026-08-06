"""Shared, read-only structural feature providers for C."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from videoscope.content.features import (
    ContentFeatureBundle,
    ContentFeatureContext,
    ContentObservation,
    FeatureProviderResult,
    FFmpegSilenceProvider,
    StructuralFeatureConfig,
    VisualStructureProvider,
    build_silence_command,
    collect_content_features,
    parse_silence_output,
)
from videoscope.content.models import (
    ContentProviderStatus,
    ContentSignalType,
    ContentTimeRange,
)
from videoscope.domain import VideoMetadata
from videoscope.scenes import SceneDetectionResult, VideoScene
from videoscope.video import FrameSample, FrameSamplingResult


def make_metadata(*, has_audio: bool = True) -> VideoMetadata:
    return VideoMetadata(
        filename="input.mp4",
        container_format="mov,mp4",
        codec="h264",
        width=64,
        height=36,
        duration_seconds=4.0,
        average_frame_rate=2.0,
        estimated_frame_count=8,
        has_audio=has_audio,
        file_size_bytes=10,
        raw_probe={},
    )


def make_scenes() -> SceneDetectionResult:
    return SceneDetectionResult(
        source="fake",
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=4.0,
                duration_seconds=4.0,
                representative_timestamp=2.0,
            ),
        ),
    )


def make_sampling(workspace: Path) -> FrameSamplingResult:
    frames = workspace / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    samples: list[FrameSample] = []
    for index, value in enumerate((0, 0, 128, 128)):
        path = frames / f"frame_{index}.png"
        Image.new("RGB", (16, 16), color=(value, value, value)).save(path)
        samples.append(
            FrameSample(
                timestamp_seconds=float(index),
                sample_index=index,
                relative_path=path.relative_to(workspace).as_posix(),
                width=16,
                height=16,
            )
        )
    return FrameSamplingResult(work_directory=workspace, samples=tuple(samples))


class FakeProbeProvider:
    provider_id = "metadata"
    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    def probe(self, path: Path, config: StructuralFeatureConfig) -> VideoMetadata:
        del path, config
        self.calls += 1
        return make_metadata()


class FakeSceneProvider:
    provider_id = "scenes"
    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    def detect(
        self, path: Path, duration_seconds: float, config: StructuralFeatureConfig
    ) -> SceneDetectionResult:
        del path, duration_seconds, config
        self.calls += 1
        return make_scenes()


class FakeSamplingProvider:
    provider_id = "sampling"
    version = "1"

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls = 0

    def sample(
        self,
        path: Path,
        duration_seconds: float,
        workspace: Path,
        config: StructuralFeatureConfig,
    ) -> FrameSamplingResult:
        del path, duration_seconds, workspace, config
        self.calls += 1
        return make_sampling(self.workspace)


class FakeObservationProvider:
    version = "1"

    def __init__(self, provider_id: str, *, fail: bool = False) -> None:
        self.provider_id = provider_id
        self.fail = fail

    def observe(self, context: ContentFeatureContext) -> FeatureProviderResult:
        if self.fail:
            raise RuntimeError("C:/Users/private/source.mp4")
        return FeatureProviderResult(
            status=ContentProviderStatus.OK,
            observations=(
                ContentObservation.create(
                    input_hash="a" * 64,
                    signal_type=ContentSignalType.SCENE,
                    source_range=ContentTimeRange(
                        start_seconds=0.0, end_seconds=context.metadata.duration_seconds
                    ),
                    provider_id=self.provider_id,
                    provider_version=self.version,
                    measurements={"count": 1},
                ),
            ),
        )


def test_coordinator_probes_and_samples_once_and_isolates_optional_failure(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "视频 with spaces.mp4"
    input_path.write_bytes(b"fixture")
    probe = FakeProbeProvider()
    scenes = FakeSceneProvider()
    sampling = FakeSamplingProvider(tmp_path / "sample work")

    bundle = collect_content_features(
        input_path,
        input_hash="a" * 64,
        workspace=tmp_path / "workspace",
        config=StructuralFeatureConfig(),
        probe_provider=probe,
        scene_provider=scenes,
        sampling_provider=sampling,
        observation_providers=(
            FakeObservationProvider("zeta"),
            FakeObservationProvider("broken", fail=True),
            FakeObservationProvider("alpha"),
        ),
    )

    assert isinstance(bundle, ContentFeatureBundle)
    assert probe.calls == scenes.calls == sampling.calls == 1
    assert tuple(item.provider_id for item in bundle.executions) == (
        "metadata",
        "sampling",
        "scenes",
        "alpha",
        "broken",
        "zeta",
    )
    assert [
        item.provider_id for item in bundle.executions if item.status == "failed"
    ] == ["broken"]
    assert len(bundle.observations) == 2
    assert all("C:/Users" not in warning for warning in bundle.warnings)


def test_coordinator_honors_cancellation_between_core_stages(tmp_path: Path) -> None:
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"fixture")
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(Exception, match="cancelled"):
        collect_content_features(
            input_path,
            input_hash="a" * 64,
            workspace=tmp_path / "workspace",
            config=StructuralFeatureConfig(),
            probe_provider=FakeProbeProvider(),
            scene_provider=FakeSceneProvider(),
            sampling_provider=FakeSamplingProvider(tmp_path / "frames"),
            observation_providers=(),
            cancellation_callback=cancelled,
        )


def test_silence_command_is_shell_free_and_preserves_unicode_path() -> None:
    path = Path("中文 目录") / "input video.mp4"
    arguments = build_silence_command(
        path,
        ffmpeg="ffmpeg",
        noise_threshold_db=-35.0,
        minimum_duration_seconds=1.25,
    )

    assert isinstance(arguments, list)
    assert str(path) in arguments
    assert arguments[0] == "ffmpeg"
    assert "shell=True" not in " ".join(arguments)
    assert "silencedetect=noise=-35dB:d=1.25" in arguments


def test_silence_parser_produces_bounded_intervals_and_closes_open_tail() -> None:
    observations = parse_silence_output(
        [
            "[silencedetect] silence_start: 0.5",
            "[silencedetect] silence_end: 1.75 | silence_duration: 1.25",
            "[silencedetect] silence_start: 3.0",
        ],
        input_hash="a" * 64,
        duration_seconds=4.0,
        provider_id="silence",
        provider_version="1",
        parameters={"noise_threshold_db": -35.0},
        maximum_observations=4,
    )

    assert [
        (item.source_range.start_seconds, item.source_range.end_seconds)
        for item in observations
    ] == [
        (0.5, 1.75),
        (3.0, 4.0),
    ]
    assert all(item.signal_type is ContentSignalType.SILENCE for item in observations)


def test_silence_provider_skips_video_without_audio(tmp_path: Path) -> None:
    context = ContentFeatureContext(
        input_path=tmp_path / "silent.mp4",
        input_hash="a" * 64,
        metadata=make_metadata(has_audio=False),
        scenes=make_scenes().scenes,
        frame_samples=(),
        frame_workspace=tmp_path,
        workspace=tmp_path,
        config=StructuralFeatureConfig(),
    )

    result = FFmpegSilenceProvider().observe(context)

    assert result.status is ContentProviderStatus.SKIPPED
    assert result.observations == ()
    assert result.warnings == (
        "Audio stream unavailable; silence analysis was skipped.",
    )


def test_visual_provider_emits_observable_near_black_and_repeated_ranges(
    tmp_path: Path,
) -> None:
    sampling = make_sampling(tmp_path)
    context = ContentFeatureContext(
        input_path=tmp_path / "input.mp4",
        input_hash="a" * 64,
        metadata=make_metadata(),
        scenes=make_scenes().scenes,
        frame_samples=sampling.samples,
        frame_workspace=sampling.work_directory,
        workspace=tmp_path,
        config=StructuralFeatureConfig(
            minimum_observation_duration_seconds=1.0,
            near_black_mean_luma_threshold=0.05,
            near_black_dark_pixel_ratio=0.95,
            repeated_max_pixel_difference=0.01,
            repeated_max_hash_distance=0,
        ),
    )

    result = VisualStructureProvider().observe(context)

    kinds = {item.signal_type for item in result.observations}
    assert ContentSignalType.NEAR_BLACK in kinds
    assert ContentSignalType.REPEATED_FRAMES in kinds
