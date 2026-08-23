from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from pydantic import JsonValue, ValidationError

from videoscope.domain import VideoMetadata
from videoscope.rescue import serialization as rescue_serialization
from videoscope.rescue import verification as verification_module
from videoscope.rescue.commands import build_preview_commands
from videoscope.rescue.errors import RescueCancelledError, RescueMediaError
from videoscope.rescue.executor import (
    CommandResult,
    NativeRescueExecutor,
    RescuedSegment,
    RescueExecutionResult,
)
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    RescueVerificationStatus,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.planner import (
    _apply_tonal_encoded_qualification,
    build_rescue_plan,
)
from videoscope.rescue.qualification import (
    TonalVerificationControlHandle,
    TonalVerificationControlRecipeV1,
)
from videoscope.rescue.timeline import SourceMapping
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
    qualify_tonal_render_profiles,
    validate_plan_tonal_action_contracts,
)
from videoscope.rescue.tonal_qualification import (
    TONAL_ENCODED_QUALIFICATION_LIMITATION,
    NativeTonalCandidateQualifier,
    TonalAudioEncodeContractV2,
    TonalAudioTimelineV1,
    TonalAudioTopologyV2,
    TonalEncodedCandidateAttemptV2,
    TonalEncodedMetricsV2,
    TonalEncodedProfileQualificationV2,
    TonalEncodedQualificationEvidenceV2,
    TonalEncodedQualificationEvidenceV3,
    TonalEncodedThresholdsV2,
    TonalRangeMappingV2,
    audio_timeline_from_ffprobe_stdout,
    audio_topology_from_ffprobe_stdout,
    qualified_tonal_action_parameters,
    tonal_audio_timeline_probe_arguments,
    validate_tonal_runtime_candidate,
    validate_tonal_runtime_parent,
)
from videoscope.rescue.verification import (
    MediaVerificationSnapshot,
    NativeMediaMeasurementProvider,
    RescueVerifier,
)

_INPUT = "1" * 64


def _topology(*, sample_rate_hz: int = 48000) -> TonalAudioTopologyV2:
    raw = {
        "codec_name": "aac",
        "codec_tag_string": "mp4a",
        "profile": "LC",
        "sample_fmt": "fltp",
        "sample_rate_hz": sample_rate_hz,
        "channels": 2,
        "channel_layout": "stereo",
        "time_base": f"1/{sample_rate_hz}",
    }
    digest = hashlib.sha256(
        json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return TonalAudioTopologyV2.model_validate({**raw, "topology_sha256": digest})


def _thresholds() -> TonalEncodedThresholdsV2:
    return TonalEncodedThresholdsV2(
        minimum_target_reduction_db=24.0,
        maximum_non_target_attenuation_db=0.25,
        maximum_boundary_energy_jump_db=0.5,
        maximum_boundary_crest_jump_db=3.0,
        maximum_boundary_adjacent_delta=0.08,
    )


def _timeline() -> TonalAudioTimelineV1:
    tokens = ["0", "0.021333333", "0.042666667"]
    return TonalAudioTimelineV1(
        packet_count=3,
        first_normalized_pts_seconds=0.0,
        last_normalized_pts_seconds=0.042666667,
        normalized_pts_sha256=hashlib.sha256(
            json.dumps(tokens, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    )


def _metrics(reduction_db: float) -> TonalEncodedMetricsV2:
    return TonalEncodedMetricsV2(
        range_coverage_ratio=1.0,
        measured_windows=40,
        excluded_transition_windows=0,
        minimum_target_reduction_db=reduction_db,
        minimum_target_margin_db=reduction_db - 24.0,
        maximum_non_target_attenuation_db=0.1,
        maximum_boundary_energy_jump_db=0.0,
        maximum_boundary_crest_jump_db=0.0,
        maximum_boundary_adjacent_delta=0.01,
    )


def _profile(notch_q: float, metrics: TonalEncodedMetricsV2) -> InterferenceTone:
    return InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=880.0,
        confidence=0.99,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-30.0,
        local_peak_over_baseline_db=30.0,
        persistence_window_count=40,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=24.0,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=notch_q,
            complete_window_count=metrics.measured_windows,
            minimum_target_reduction_db=metrics.minimum_target_reduction_db,
            maximum_non_target_attenuation_db=(
                metrics.maximum_non_target_attenuation_db
            ),
            maximum_boundary_energy_jump_db=(metrics.maximum_boundary_energy_jump_db),
            maximum_boundary_crest_jump_db=metrics.maximum_boundary_crest_jump_db,
            maximum_boundary_adjacent_delta=metrics.maximum_boundary_adjacent_delta,
        ),
    )


def _qualified_action(
    *, bad_q_order: bool = False
) -> tuple[RescueAction, TonalInterferenceConfig]:
    config = TonalInterferenceConfig()
    raw_profile = _profile(8.0, _metrics(25.0))
    raw_parameters: dict[str, JsonValue] = {
        "algorithm_version": "1",
        "config": config.model_dump(mode="json"),
        "interference_profiles": [raw_profile.model_dump(mode="json")],
    }
    draft_id = make_rescue_action_id(
        kind=RescueActionKind.DENOISE_AUDIO,
        parameters=raw_parameters,
        source_ranges=((1.0, 2.0),),
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    passing = _metrics(25.0)
    attempts = (
        TonalEncodedCandidateAttemptV2(
            notch_q=(12.0 if bad_q_order else 18.0),
            candidate_sha256="3" * 64,
            candidate_audio_topology=_topology(),
            metrics=_metrics(23.0),
            thresholds=_thresholds(),
        ),
        TonalEncodedCandidateAttemptV2(
            notch_q=(8.0 if bad_q_order else 12.0),
            candidate_sha256="4" * 64,
            candidate_audio_topology=_topology(),
            metrics=passing,
            thresholds=_thresholds(),
        ),
    )
    final_profile = _profile(attempts[-1].notch_q, passing)
    evidence = TonalEncodedQualificationEvidenceV3(
        input_hash=_INPUT,
        draft_action_id=draft_id,
        draft_parameters=raw_parameters,
        source_ranges=((1.0, 2.0),),
        output_ranges=((1.0, 2.0),),
        range_mappings=(
            TonalRangeMappingV2(
                source_start=0.0,
                source_end=3.0,
                output_start=0.0,
                output_end=3.0,
            ),
        ),
        audio_encode_contract=TonalAudioEncodeContractV2(
            parent_bitrate_kbps=192,
            candidate_bitrate_kbps=192,
        ),
        parent_sha256="2" * 64,
        parent_audio_topology=_topology(),
        boundary_control_sha256=hashlib.sha256(b"control").hexdigest(),
        boundary_control_audio_topology=_topology(),
        boundary_control_audio_timeline=_timeline(),
        profile_candidate_audio_timelines=((_timeline(), _timeline()),),
        combined_audio_timeline=_timeline(),
        profile_qualifications=(
            TonalEncodedProfileQualificationV2(
                profile_index=0,
                attempts=attempts,
                selected_notch_q=attempts[-1].notch_q,
            ),
        ),
        combined_candidate_sha256="5" * 64,
        combined_audio_topology=_topology(),
        combined_metrics=(passing,),
        combined_thresholds=(_thresholds(),),
        selected_profiles=(final_profile,),
    )
    parameters = qualified_tonal_action_parameters(evidence)
    action_id = make_rescue_action_id(
        kind=RescueActionKind.DENOISE_AUDIO,
        parameters=parameters,
        source_ranges=((1.0, 2.0),),
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    return (
        RescueAction(
            id=action_id,
            version="1",
            kind=RescueActionKind.DENOISE_AUDIO,
            description="Encoded qualified tone.",
            source_ranges=((1.0, 2.0),),
            parameters=parameters,
            changes_content=True,
            requires_confirmation=True,
            strategy=RescueStrategy.BALANCED,
        ),
        config,
    )


def test_tonal_qualification_evidence_rejects_path_bearing_draft_parameters() -> None:
    action, _config = _qualified_action()
    payload = action.parameters["encoded_candidate_qualification"]
    assert isinstance(payload, dict)
    tampered = dict(payload)
    raw_draft_parameters = tampered["draft_parameters"]
    assert isinstance(raw_draft_parameters, dict)
    draft_parameters = dict(raw_draft_parameters)
    draft_parameters["private_control"] = "../control.mp4"
    tampered["draft_parameters"] = draft_parameters

    with pytest.raises(ValueError, match="path"):
        TonalEncodedQualificationEvidenceV3.model_validate(tampered)


def _draft_plan() -> SimpleNamespace:
    config = TonalInterferenceConfig()
    profile = _profile(8.0, _metrics(25.0))
    parameters: dict[str, JsonValue] = {
        "algorithm_version": "1",
        "config": config.model_dump(mode="json"),
        "interference_profiles": [profile.model_dump(mode="json")],
    }
    action = RescueAction(
        id=make_rescue_action_id(
            kind=RescueActionKind.DENOISE_AUDIO,
            parameters=parameters,
            source_ranges=((1.0, 2.0),),
            strategy=RescueStrategy.BALANCED,
            version="1",
        ),
        version="1",
        kind=RescueActionKind.DENOISE_AUDIO,
        description="Raw-qualified tonal draft.",
        source_ranges=((1.0, 2.0),),
        parameters=parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    return SimpleNamespace(
        input_hash=_INPUT,
        actions=(action,),
        effective_config=SimpleNamespace(improved_audio_bitrate_kbps=192),
    )


def _planner_inputs() -> dict[str, Any]:
    config = TonalInterferenceConfig()
    profile = _profile(8.0, _metrics(25.0))
    metadata = VideoMetadata(
        filename="source.mp4",
        container_format="mp4",
        codec="h264",
        width=1280,
        height=720,
        duration_seconds=3.0,
        average_frame_rate=24.0,
        estimated_frame_count=72,
        has_audio=True,
        file_size_bytes=6,
    )
    damage = DamageInterval(
        id=make_damage_id(_INPUT, "audio:0", DamageKind.AUDIO_NOISE, 1.0, 2.0),
        stream_id="audio:0",
        kind=DamageKind.AUDIO_NOISE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    return {
        "metadata": metadata,
        "damage_map": MediaDamageMap(
            input_hash=_INPUT,
            duration_seconds=3.0,
            scan_coverage=((0.0, 3.0),),
            intervals=(damage,),
        ),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
        "assessment_parameters": {
            "tonal_interference_measurements": [
                {
                    "source_ranges": [[1.0, 2.0]],
                    "algorithm_version": "1",
                    "interference_profiles": [profile.model_dump(mode="json")],
                    "config": config.model_dump(mode="json"),
                }
            ]
        },
    }


class _FakeExecutor:
    def __init__(self) -> None:
        self.rendered_q: list[tuple[float, ...]] = []
        self.identity_render_count = 0

    def execute_faithful(
        self,
        plan: Any,
        source: Any,
        work_root: Any,
        cancellation_callback: Any,
        **kwargs: Any,
    ) -> SimpleNamespace:
        del plan, source, cancellation_callback, kwargs
        parent = work_root / "staging" / "faithful.mp4"
        parent.parent.mkdir(parents=True)
        parent.write_bytes(b"encoded-parent")
        return SimpleNamespace(
            output_path=parent,
            source_mappings=(
                SourceMapping(0.0, 3.0, 0.0, 3.0, "staging/faithful.mp4"),
            ),
        )

    def execute_tonal_reduced(
        self,
        *,
        source: Any,
        output: Any,
        tones: tuple[InterferenceTone, ...],
        config: TonalInterferenceConfig,
        cancellation_callback: Any,
    ) -> None:
        del source, config, cancellation_callback
        q_values = tuple(
            float(tone.render_qualification.notch_q)
            for tone in tones
            if tone.render_qualification is not None
        )
        self.rendered_q.append(q_values)
        output.write_bytes(("encoded:" + ",".join(map(str, q_values))).encode())

    def execute_tonal_identity(
        self,
        *,
        source: Any,
        output: Any,
        config: TonalInterferenceConfig,
        cancellation_callback: Any,
    ) -> None:
        del source, config, cancellation_callback
        self.identity_render_count += 1
        output.write_bytes(b"encoded-identity-control")


class _FakeMeasurementProvider:
    def __init__(
        self,
        *,
        passing_q: float | None,
        combined_reduction_db: float | None = None,
        require_boundary_reference: bool = False,
    ) -> None:
        self.passing_q = passing_q
        self.combined_reduction_db = combined_reduction_db
        self.require_boundary_reference = require_boundary_reference
        self.boundary_references: list[Path] = []

    def inspect_tonal_audio_topology(
        self, path: Any, cancellation_callback: Any
    ) -> dict[str, Any]:
        del path, cancellation_callback
        return _topology().model_dump(mode="json")

    def inspect_tonal_audio_timeline(
        self, path: Any, cancellation_callback: Any
    ) -> dict[str, Any]:
        del path, cancellation_callback
        return _timeline().model_dump(mode="json")

    def measure_perceptual_restoration(
        self,
        kind: RescueActionKind,
        source: Any,
        candidate: Any,
        source_ranges: Any,
        output_ranges: Any,
        parameters: dict[str, Any],
        cancellation_callback: Any,
        *,
        boundary_reference: Any = None,
    ) -> dict[str, float]:
        del kind, source, source_ranges, output_ranges, cancellation_callback
        if self.require_boundary_reference:
            assert boundary_reference is not None
            self.boundary_references.append(Path(boundary_reference))
        profile = InterferenceTone.model_validate_json(
            json.dumps(parameters["interference_profiles"][0])
        )
        assert profile.render_qualification is not None
        q_value = profile.render_qualification.notch_q
        reduction = 25.0 if q_value == self.passing_q else 23.0
        if candidate.name == "combined.mp4" and self.combined_reduction_db is not None:
            reduction = self.combined_reduction_db
        raw = {
            "range_coverage_ratio": 1.0,
            "measured_windows": 40.0,
            "excluded_transition_windows": 0.0,
            "minimum_target_reduction_db": reduction,
            "minimum_target_margin_db": reduction - 24.0,
            "maximum_non_target_attenuation_db": 0.1,
            "maximum_boundary_energy_jump_db": 0.0,
            "maximum_boundary_crest_jump_db": 0.0,
            "maximum_boundary_adjacent_delta": 0.01,
        }
        for index, _item in enumerate(parameters["interference_profiles"]):
            raw.update(
                {
                    f"profile_{index}_range_coverage_ratio": 1.0,
                    f"profile_{index}_measured_windows": 40.0,
                    f"profile_{index}_excluded_transition_windows": 0.0,
                    f"profile_{index}_minimum_target_reduction_db": reduction,
                    f"profile_{index}_minimum_target_margin_db": reduction - 24.0,
                    f"profile_{index}_maximum_non_target_attenuation_db": 0.1,
                    f"profile_{index}_maximum_boundary_energy_jump_db": 0.0,
                    f"profile_{index}_maximum_boundary_crest_jump_db": 0.0,
                    f"profile_{index}_maximum_boundary_adjacent_delta": 0.01,
                }
            )
        return raw


def test_native_qualifier_uses_one_same_generation_boundary_control(
    tmp_path: Path,
) -> None:
    executor = _FakeExecutor()
    provider = _FakeMeasurementProvider(
        passing_q=12.0,
        require_boundary_reference=True,
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    evidence = NativeTonalCandidateQualifier(
        executor=executor,
        measurement_provider=provider,
    ).qualify(_draft_plan(), source, tmp_path / "qualification", lambda: False)

    assert evidence.passed
    assert executor.identity_render_count == 1
    assert provider.boundary_references
    assert len(set(provider.boundary_references)) == 1
    assert (
        evidence.boundary_control_sha256
        == hashlib.sha256(b"encoded-identity-control").hexdigest()
    )
    assert evidence.boundary_control_audio_topology == _topology()


def test_encoded_qualifier_rejects_candidate_with_missing_complete_window(
    tmp_path: Path,
) -> None:
    class MissingWindowProvider(_FakeMeasurementProvider):
        def measure_perceptual_restoration(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, float]:
            measured = super().measure_perceptual_restoration(*args, **kwargs)
            parameters = cast(dict[str, Any], args[5])
            profile = InterferenceTone.model_validate_json(
                json.dumps(parameters["interference_profiles"][0])
            )
            assert profile.render_qualification is not None
            if profile.render_qualification.notch_q == 12.0:
                measured["measured_windows"] = 39.0
                if Path(cast(Path, args[2])).name == "combined.mp4":
                    measured["profile_0_measured_windows"] = 39.0
            return measured

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_root = tmp_path / "qualification"

    with pytest.raises(RescueMediaError):
        NativeTonalCandidateQualifier(
            executor=_FakeExecutor(),
            measurement_provider=MissingWindowProvider(passing_q=12.0),
        ).qualify(_draft_plan(), source, work_root, lambda: False)
    assert not work_root.exists()


def test_encoded_qualifier_rejects_combined_missing_complete_window(
    tmp_path: Path,
) -> None:
    class MissingCombinedWindowProvider(_FakeMeasurementProvider):
        def measure_perceptual_restoration(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, float]:
            measured = super().measure_perceptual_restoration(*args, **kwargs)
            if Path(cast(Path, args[2])).name == "combined.mp4":
                measured["measured_windows"] = 39.0
                measured["profile_0_measured_windows"] = 39.0
            return measured

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_root = tmp_path / "qualification"

    with pytest.raises(RescueMediaError):
        NativeTonalCandidateQualifier(
            executor=_FakeExecutor(),
            measurement_provider=MissingCombinedWindowProvider(passing_q=12.0),
        ).qualify(_draft_plan(), source, work_root, lambda: False)
    assert not work_root.exists()


def test_final_tonal_verifier_requires_and_consumes_bound_runtime_control(
    tmp_path: Path,
) -> None:
    action, _config = _qualified_action()
    evidence = TonalEncodedQualificationEvidenceV3.model_validate_json(
        json.dumps(action.parameters["encoded_candidate_qualification"])
    )
    source_path = tmp_path / "source.mp4"
    candidate_path = tmp_path / "faithful-rescue.mp4"
    control_path = tmp_path / "tonal-control.private.mp4"
    source_path.write_bytes(b"source")
    candidate_path.write_bytes(b"candidate")
    control_path.write_bytes(b"control")
    source = MediaVerificationSnapshot(
        path=source_path,
        relative_path="source.mp4",
        duration_seconds=3.0,
        video_stream_count=1,
        audio_stream_count=1,
        complete_decode=True,
        sha256=hashlib.sha256(b"source").hexdigest(),
    )
    candidate = MediaVerificationSnapshot(
        path=candidate_path,
        relative_path="faithful-rescue.mp4",
        duration_seconds=3.0,
        video_stream_count=1,
        audio_stream_count=1,
        complete_decode=True,
        sha256=hashlib.sha256(b"candidate").hexdigest(),
    )
    plan = cast(
        RescuePlan,
        SimpleNamespace(plan_digest="7" * 64, actions=(action,)),
    )
    provider = _FakeMeasurementProvider(
        passing_q=12.0,
        require_boundary_reference=True,
    )
    control = TonalVerificationControlHandle(
        path=control_path,
        recipe=TonalVerificationControlRecipeV1(
            plan_digest=plan.plan_digest,
            action_id=action.id,
            parent_sha256=evidence.parent_sha256,
            control_sha256=evidence.boundary_control_sha256,
            qualified_candidate_sha256=str(evidence.combined_candidate_sha256),
            source_ranges=action.source_ranges,
            output_ranges=evidence.output_ranges,
            encode_contract=evidence.audio_encode_contract.model_dump(mode="json"),
            control_audio_topology=(
                evidence.boundary_control_audio_topology.model_dump(mode="json")
            ),
            candidate_audio_topology=(
                evidence.combined_audio_topology.model_dump(mode="json")
                if evidence.combined_audio_topology is not None
                else {}
            ),
            control_audio_timeline=(
                evidence.boundary_control_audio_timeline.model_dump(mode="json")
            ),
            candidate_audio_timeline=(
                evidence.combined_audio_timeline.model_dump(mode="json")
                if evidence.combined_audio_timeline is not None
                else {}
            ),
        ),
    )
    verifier = RescueVerifier(measurement_provider=cast(Any, provider))
    mappings = (SourceMapping(0.0, 3.0, 0.0, 3.0, "faithful-rescue.mp4"),)

    checks = verifier._perceptual_restoration_checks(
        source,
        candidate,
        "faithful",
        plan,
        mappings,
        lambda: False,
        verification_controls=(control,),
    )

    assert provider.boundary_references == [control_path]
    assert all(check.status is RescueVerificationStatus.PASSED for check in checks)
    missing = verifier._perceptual_restoration_checks(
        source,
        candidate,
        "faithful",
        plan,
        mappings,
        lambda: False,
        verification_controls=(),
    )
    assert all(
        check.status is RescueVerificationStatus.NEEDS_REVIEW for check in missing
    )


def test_tonal_provider_splits_spectral_source_from_boundary_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_rate = 4000
    timeline = np.arange(2800, dtype=np.float64) / sample_rate + 0.15
    voice = 0.08 * np.sin(2.0 * np.pi * 120.0 * timeline)
    event = (timeline >= 0.2) & (timeline < 0.8)
    source_pcm = voice.copy()
    source_pcm[event] += 0.5 * np.sin(2.0 * np.pi * 880.0 * timeline[event])
    candidate_pcm = voice.copy()
    candidate_pcm[timeline >= 0.2] += 0.2
    control_pcm = candidate_pcm.copy()
    decoded = {
        "source": source_pcm[:, None],
        "candidate": candidate_pcm[:, None],
        "control": control_pcm[:, None],
    }

    def decode(path: Path, *_args: Any, **_kwargs: Any) -> tuple[Any, int]:
        return decoded[path.name], sample_rate

    monkeypatch.setattr(verification_module, "_decode_audio_segment", decode)
    metrics = _metrics(25.0)
    raw_profile = _profile(8.0, metrics)
    assert raw_profile.render_qualification is not None
    profile = raw_profile.model_copy(
        update={
            "start_seconds": 0.2,
            "end_seconds": 0.8,
            "channel_indices": (0,),
            "persistence_window_count": 12,
            "render_qualification": raw_profile.render_qualification.model_copy(
                update={"complete_window_count": 12}
            ),
        }
    )
    parameters: dict[str, JsonValue] = {
        "config": TonalInterferenceConfig().model_dump(mode="json"),
        "interference_profiles": [profile.model_dump(mode="json")],
    }
    raw = verification_module._measure_tonal_outcome(
        Path("source"),
        Path("candidate"),
        ((0.2, 0.8),),
        ((0.2, 0.8),),
        parameters,
        "ffmpeg",
        cast(Any, lambda *_args, **_kwargs: None),
        1.0,
        lambda: False,
    )
    controlled = verification_module._measure_tonal_outcome(
        Path("source"),
        Path("candidate"),
        ((0.2, 0.8),),
        ((0.2, 0.8),),
        parameters,
        "ffmpeg",
        cast(Any, lambda *_args, **_kwargs: None),
        1.0,
        lambda: False,
        boundary_reference=Path("control"),
    )

    assert raw["minimum_target_reduction_db"] >= 24.0
    assert controlled["minimum_target_reduction_db"] == pytest.approx(
        raw["minimum_target_reduction_db"]
    )
    assert raw["maximum_boundary_adjacent_delta"] > 0.08
    assert controlled["maximum_boundary_adjacent_delta"] <= 0.08


def test_executor_retains_exact_qualified_tonal_runtime_control(
    tmp_path: Path,
) -> None:
    qualifier = NativeTonalCandidateQualifier(
        executor=_FakeExecutor(),
        measurement_provider=_FakeMeasurementProvider(passing_q=12.0),
    )
    qualification_source = tmp_path / "qualification-source.mp4"
    qualification_source.write_bytes(b"source")
    draft = build_rescue_plan(**_planner_inputs())
    evidence = qualifier.qualify(
        draft,
        qualification_source,
        tmp_path / "qualification",
        lambda: False,
    )
    plan = build_rescue_plan(
        **_planner_inputs(),
        tonal_qualification=evidence,
        require_tonal_qualification=True,
    )
    work_root = tmp_path / "execution"
    faithful = work_root / "staging" / "faithful-rescue.mp4"
    faithful.parent.mkdir(parents=True)
    faithful.write_bytes(b"encoded-parent")
    segment = RescuedSegment(0.0, 3.0, 0.0, 3.0, "staging/faithful-rescue.mp4")
    execution = RescueExecutionResult(
        output_path=faithful,
        output_relative_path="staging/faithful-rescue.mp4",
        segments=(segment,),
        source_mappings=(segment.source_mapping,),
    )
    topology_stdout = json.dumps(
        {
            "streams": [
                {
                    "codec_name": "aac",
                    "codec_tag_string": "mp4a",
                    "profile": "LC",
                    "sample_fmt": "fltp",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "time_base": "1/48000",
                }
            ]
        }
    )
    media_stdout = json.dumps(
        {
            "format": {"duration": "3.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "start_time": "0.0",
                    "duration": "3.0",
                    "avg_frame_rate": "10/1",
                    "r_frame_rate": "10/1",
                    "nb_frames": "30",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "start_time": "0.0",
                    "duration": "3.0",
                    "sample_rate": "48000",
                },
            ],
        }
    )
    timeline_stdout = json.dumps(
        {
            "streams": [{"start_time": "0.0"}],
            "packets": [
                {"pts_time": "0.0"},
                {"pts_time": "0.021333333"},
                {"pts_time": "0.042666667"},
            ],
        }
    )

    def runner(arguments: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        if "stream=start_time:packet=pts_time" in arguments:
            return CommandResult(0, "", timeline_stdout)
        if any("codec_name,codec_tag_string" in item for item in arguments):
            return CommandResult(0, "", topology_stdout)
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", media_stdout)
        if "null" in arguments:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {arguments}")

    executor = NativeRescueExecutor(
        runner=runner,
        tonal_renderer=(
            lambda source, output, tones, config, **_kwargs: output.write_bytes(
                b"encoded:12.0"
            )
        ),
        tonal_identity_renderer=(
            lambda source, output, config, **_kwargs: output.write_bytes(
                b"encoded-identity-control"
            )
        ),
    )

    restored = executor.execute_faithful_restoration(
        plan, execution, work_root, lambda: False
    )

    tonal_controls = tuple(
        item
        for item in restored.verification_controls
        if isinstance(item, TonalVerificationControlHandle)
    )
    assert len(tonal_controls) == 1
    assert tonal_controls[0].path.read_bytes() == b"encoded-identity-control"
    assert tonal_controls[0].recipe.control_sha256 == (evidence.boundary_control_sha256)
    assert faithful.read_bytes() == b"encoded:12.0"
    tonal_controls[0].path.unlink()


def test_encoded_tonal_qualification_round_trips_and_selects_first_pass() -> None:
    action, _config = _qualified_action()
    plan = SimpleNamespace(
        input_hash=_INPUT,
        actions=(action,),
        effective_config=SimpleNamespace(
            tonal_algorithm_version="1", improved_audio_bitrate_kbps=192
        ),
    )

    validate_plan_tonal_action_contracts(cast(RescuePlan, plan))


def test_failed_encoded_qualification_has_canonical_path_free_private_json(
    tmp_path: Path,
) -> None:
    action, _config = _qualified_action()
    payload = json.loads(
        json.dumps(action.parameters["encoded_candidate_qualification"])
    )
    payload["profile_qualifications"][0]["attempts"] = payload[
        "profile_qualifications"
    ][0]["attempts"][:1]
    payload["profile_candidate_audio_timelines"][0] = payload[
        "profile_candidate_audio_timelines"
    ][0][:1]
    payload["profile_qualifications"][0]["selected_notch_q"] = None
    payload["combined_candidate_sha256"] = None
    payload["combined_audio_topology"] = None
    payload["combined_metrics"] = []
    payload["combined_thresholds"] = []
    payload["combined_audio_timeline"] = None
    payload["selected_profiles"] = []
    payload["limitation"] = TONAL_ENCODED_QUALIFICATION_LIMITATION
    payload["schema_version"] = "3"
    payload["boundary_control_sha256"] = "6" * 64
    payload["boundary_control_audio_topology"] = _topology().model_dump(mode="json")
    evidence = TonalEncodedQualificationEvidenceV3.model_validate(payload)
    destination = tmp_path / "tonal-qualification-evidence-private.json"

    rescue_serialization.write_tonal_encoded_qualification_json(evidence, destination)

    content = destination.read_text(encoding="utf-8")
    assert content.endswith("\n")
    assert str(tmp_path) not in content
    assert (
        rescue_serialization.read_tonal_encoded_qualification_json(destination)
        == evidence
    )


def test_encoded_tonal_qualification_rejects_noncanonical_q_order() -> None:
    action, _config = _qualified_action(bad_q_order=True)
    plan = SimpleNamespace(
        input_hash=_INPUT,
        actions=(action,),
        effective_config=SimpleNamespace(
            tonal_algorithm_version="1", improved_audio_bitrate_kbps=192
        ),
    )

    with pytest.raises(ValueError, match="Q order"):
        validate_plan_tonal_action_contracts(cast(RescuePlan, plan))


def test_encoded_tonal_qualification_rejects_missing_attempt_window() -> None:
    action, _config = _qualified_action()
    parameters = cast(
        dict[str, JsonValue],
        json.loads(json.dumps(action.parameters, ensure_ascii=False)),
    )
    evidence = cast(dict[str, Any], parameters["encoded_candidate_qualification"])
    evidence["profile_qualifications"][0]["attempts"][0]["metrics"][
        "measured_windows"
    ] = 39
    tampered = action.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    plan = cast(
        RescuePlan,
        SimpleNamespace(
            input_hash=_INPUT,
            actions=(tampered,),
            effective_config=SimpleNamespace(
                tonal_algorithm_version="1", improved_audio_bitrate_kbps=192
            ),
        ),
    )

    with pytest.raises(ValueError, match="window inventory"):
        validate_plan_tonal_action_contracts(plan)


def test_canonical_plan_rejects_combined_excluded_window_after_digest_recompute(
    tmp_path: Path,
) -> None:
    inputs = _planner_inputs()
    draft = build_rescue_plan(**inputs)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    evidence = NativeTonalCandidateQualifier(
        executor=_FakeExecutor(),
        measurement_provider=_FakeMeasurementProvider(passing_q=12.0),
    ).qualify(draft, source, tmp_path / "qualification", lambda: False)
    final = build_rescue_plan(
        **inputs,
        tonal_qualification=evidence,
        require_tonal_qualification=True,
    )
    action = next(
        item for item in final.actions if item.kind is RescueActionKind.DENOISE_AUDIO
    )
    parameters = cast(
        dict[str, JsonValue],
        json.loads(json.dumps(action.parameters, ensure_ascii=False)),
    )
    qualification = cast(dict[str, Any], parameters["encoded_candidate_qualification"])
    qualification["combined_metrics"][0]["excluded_transition_windows"] = 1
    tampered_action = action.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    plan_payload = cast(
        dict[str, JsonValue], final.model_dump(mode="json", exclude={"plan_digest"})
    )
    plan_payload["actions"] = [
        tampered_action.model_dump(mode="json")
        if item.id == action.id
        else item.model_dump(mode="json")
        for item in final.actions
    ]
    plan_payload["plan_digest"] = make_rescue_plan_digest(plan_payload)

    with pytest.raises(ValidationError, match="window inventory"):
        RescuePlan.model_validate(plan_payload)


@pytest.mark.parametrize(
    "mutation",
    (
        "attempt_threshold",
        "combined_threshold",
        "metric_margin",
        "mapping",
        "candidate_timeline",
    ),
)
def test_encoded_tonal_qualification_rejects_recomputed_semantic_tamper(
    mutation: str,
) -> None:
    action, _config = _qualified_action()
    parameters = cast(
        dict[str, JsonValue],
        json.loads(json.dumps(action.parameters, ensure_ascii=False)),
    )
    evidence = cast(dict[str, Any], parameters["encoded_candidate_qualification"])
    if mutation == "attempt_threshold":
        evidence["profile_qualifications"][0]["attempts"][0]["thresholds"][
            "maximum_non_target_attenuation_db"
        ] = 0.3
    elif mutation == "combined_threshold":
        evidence["combined_thresholds"][0]["maximum_boundary_adjacent_delta"] = 0.09
    elif mutation == "metric_margin":
        evidence["combined_metrics"][0]["minimum_target_margin_db"] = 2.0
    elif mutation == "mapping":
        evidence["range_mappings"][0]["output_start"] = 0.1
        evidence["range_mappings"][0]["output_end"] = 3.1
    else:
        evidence["profile_candidate_audio_timelines"][0][0]["normalized_pts_sha256"] = (
            "9" * 64
        )
    tampered = action.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    plan = cast(
        RescuePlan,
        SimpleNamespace(
            input_hash=_INPUT,
            actions=(tampered,),
            effective_config=SimpleNamespace(
                tonal_algorithm_version="1", improved_audio_bitrate_kbps=192
            ),
        ),
    )

    with pytest.raises(ValueError):
        validate_plan_tonal_action_contracts(plan)


def test_native_qualifier_selects_first_full_range_encoded_pass_and_cleans(
    tmp_path: Path,
) -> None:
    executor = _FakeExecutor()
    qualifier = NativeTonalCandidateQualifier(
        executor=executor,
        measurement_provider=_FakeMeasurementProvider(passing_q=12.0),
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_root = tmp_path / "qualification"

    evidence = qualifier.qualify(_draft_plan(), source, work_root, lambda: False)

    assert evidence.passed
    assert evidence.profile_qualifications[0].selected_notch_q == 12.0
    assert tuple(
        attempt.notch_q for attempt in evidence.profile_qualifications[0].attempts
    ) == (18.0, 12.0)
    assert evidence.output_ranges == ((1.0, 2.0),)
    assert evidence.range_mappings[0].output_end == pytest.approx(3.0)
    assert executor.rendered_q == [(18.0,), (12.0,), (12.0,)]
    final_actions = _apply_tonal_encoded_qualification(
        _draft_plan().actions,
        evidence,
        input_hash=_INPUT,
        required=True,
    )
    assert len(final_actions) == 1
    assert final_actions[0].id != evidence.draft_action_id
    validate_plan_tonal_action_contracts(
        cast(
            RescuePlan,
            SimpleNamespace(
                input_hash=_INPUT,
                actions=final_actions,
                effective_config=SimpleNamespace(
                    tonal_algorithm_version="1", improved_audio_bitrate_kbps=192
                ),
            ),
        )
    )
    assert not work_root.exists()


def test_native_qualifier_preserves_all_profile_output_ranges(
    tmp_path: Path,
) -> None:
    draft = _draft_plan()
    action = next(
        item for item in draft.actions if item.kind is RescueActionKind.DENOISE_AUDIO
    )
    first = InterferenceTone.model_validate_json(
        json.dumps(action.parameters["interference_profiles"][0])
    )
    second = first.model_copy(
        update={
            "frequency_hz": 880.0,
            "start_seconds": 2.0,
            "end_seconds": 3.0,
        }
    )
    parameters = dict(action.parameters)
    parameters["interference_profiles"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]
    parameters["affected_ranges"] = [[1.0, 2.0], [2.0, 3.0]]
    updated_action = action.model_copy(
        update={
            "parameters": parameters,
            "source_ranges": ((1.0, 2.0), (2.0, 3.0)),
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=((1.0, 2.0), (2.0, 3.0)),
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    draft = SimpleNamespace(
        input_hash=draft.input_hash,
        actions=tuple(
            updated_action if item is action else item for item in draft.actions
        ),
        effective_config=draft.effective_config,
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    evidence = NativeTonalCandidateQualifier(
        executor=_FakeExecutor(),
        measurement_provider=_FakeMeasurementProvider(passing_q=12.0),
    ).qualify(draft, source, tmp_path / "qualification", lambda: False)

    assert evidence.output_ranges == ((1.0, 2.0), (2.0, 3.0))
    validate_plan_tonal_action_contracts(
        cast(
            RescuePlan,
            SimpleNamespace(
                input_hash=_INPUT,
                actions=_apply_tonal_encoded_qualification(
                    draft.actions,
                    evidence,
                    input_hash=_INPUT,
                    required=True,
                ),
                effective_config=SimpleNamespace(
                    tonal_algorithm_version="1", improved_audio_bitrate_kbps=192
                ),
            ),
        )
    )


def test_planner_binds_encoded_qualification_into_stable_public_plan(
    tmp_path: Path,
) -> None:
    inputs = _planner_inputs()
    draft = build_rescue_plan(**inputs)
    draft_action = next(
        action
        for action in draft.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    evidence = NativeTonalCandidateQualifier(
        executor=_FakeExecutor(),
        measurement_provider=_FakeMeasurementProvider(passing_q=12.0),
    ).qualify(draft, source, tmp_path / "qualification", lambda: False)

    final = build_rescue_plan(
        **inputs,
        tonal_qualification=evidence,
        require_tonal_qualification=True,
    )
    repeated = build_rescue_plan(
        **inputs,
        tonal_qualification=evidence,
        require_tonal_qualification=True,
    )
    final_action = next(
        action
        for action in final.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )

    assert final_action.id != draft_action.id
    assert final == repeated
    assert final.plan_digest == repeated.plan_digest
    assert RescuePlan.model_validate_json(final.model_dump_json()) == final


def test_preview_and_executor_reject_unqualified_tonal_draft_before_runner(
    tmp_path: Path,
) -> None:
    draft = build_rescue_plan(**_planner_inputs())
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    with pytest.raises(ValueError, match="encoded candidate qualification"):
        build_preview_commands(draft, source, tmp_path / "previews")

    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError, match="could not be processed locally"):
        NativeRescueExecutor(runner=runner).execute_faithful(
            draft,
            source,
            tmp_path / "execution",
            lambda: False,
        )
    assert runner_calls == []


def test_native_qualifier_omits_raw_pcm_pass_when_every_encoded_q_fails(
    tmp_path: Path,
) -> None:
    executor = _FakeExecutor()
    qualifier = NativeTonalCandidateQualifier(
        executor=executor,
        measurement_provider=_FakeMeasurementProvider(passing_q=None),
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_root = tmp_path / "qualification"

    evidence = qualifier.qualify(_draft_plan(), source, work_root, lambda: False)

    assert not evidence.passed
    assert evidence.selected_profiles == ()
    assert evidence.combined_candidate_sha256 is None
    assert (
        tuple(
            attempt.notch_q for attempt in evidence.profile_qualifications[0].attempts
        )
        == TonalInterferenceConfig().render_qualification_notch_q_values
    )
    assert (
        _apply_tonal_encoded_qualification(
            _draft_plan().actions,
            evidence,
            input_hash=_INPUT,
            required=True,
        )
        == ()
    )
    assert not work_root.exists()


def test_native_qualifier_omits_prefix_pass_when_combined_full_range_fails(
    tmp_path: Path,
) -> None:
    qualifier = NativeTonalCandidateQualifier(
        executor=_FakeExecutor(),
        measurement_provider=_FakeMeasurementProvider(
            passing_q=12.0,
            combined_reduction_db=23.0,
        ),
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    evidence = qualifier.qualify(
        _draft_plan(), source, tmp_path / "qualification", lambda: False
    )

    assert not evidence.passed
    assert evidence.profile_qualifications[0].selected_notch_q == 12.0
    assert evidence.combined_metrics == ()
    assert evidence.combined_candidate_sha256 is None


def test_native_qualifier_cleans_private_media_on_cancel_and_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cancelled_root = tmp_path / "cancelled"
    qualifier = NativeTonalCandidateQualifier(
        executor=_FakeExecutor(),
        measurement_provider=_FakeMeasurementProvider(passing_q=12.0),
    )

    with pytest.raises(RescueCancelledError):
        qualifier.qualify(_draft_plan(), source, cancelled_root, lambda: True)
    assert not cancelled_root.exists()

    class FailingProvider(_FakeMeasurementProvider):
        def measure_perceptual_restoration(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("injected measurement failure")

    failed_root = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="injected measurement failure"):
        NativeTonalCandidateQualifier(
            executor=_FakeExecutor(),
            measurement_provider=FailingProvider(passing_q=12.0),
        ).qualify(_draft_plan(), source, failed_root, lambda: False)
    assert not failed_root.exists()


def test_audio_topology_parser_is_strict_and_digest_bound() -> None:
    stdout = json.dumps(
        {
            "streams": [
                {
                    "codec_name": "aac",
                    "codec_tag_string": "mp4a",
                    "profile": "LC",
                    "sample_fmt": "fltp",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "time_base": "1/48000",
                }
            ]
        }
    )

    assert audio_topology_from_ffprobe_stdout(stdout) == _topology()
    for mutation in cast(
        tuple[dict[str, object], ...],
        (
            {"streams": []},
            {"streams": [{"codec_name": "aac"}]},
            {"streams": [json.loads(stdout)["streams"][0], {}]},
        ),
    ):
        with pytest.raises(ValueError, match="topology probe is incomplete"):
            audio_topology_from_ffprobe_stdout(json.dumps(mutation))


def test_audio_timeline_parser_normalizes_only_stream_start_and_is_strict() -> None:
    valid = {
        "streams": [{"start_time": "0.021"}],
        "packets": [
            {"pts_time": "0.021"},
            {"pts_time": "0.042333333"},
            {"pts_time": "0.063666667"},
        ],
    }

    observed = audio_timeline_from_ffprobe_stdout(json.dumps(valid))
    assert observed.packet_count == _timeline().packet_count
    assert observed.first_normalized_pts_seconds == pytest.approx(0.0, abs=1e-12)
    assert observed.last_normalized_pts_seconds == pytest.approx(
        _timeline().last_normalized_pts_seconds,
        abs=1e-12,
    )
    assert observed.normalized_pts_sha256 == _timeline().normalized_pts_sha256
    for invalid in (
        {"streams": [], "packets": valid["packets"]},
        {"streams": valid["streams"], "packets": []},
        {
            "streams": valid["streams"],
            "packets": [{"pts_time": "0.021"}, {"pts_time": "0.021"}],
        },
        {
            "streams": valid["streams"],
            "packets": [{"pts_time": "NaN"}],
        },
    ):
        with pytest.raises(ValueError, match="timeline probe is incomplete"):
            audio_timeline_from_ffprobe_stdout(json.dumps(invalid))


def test_audio_timeline_compact_probe_stays_bounded_and_strict() -> None:
    stdout = (
        "packet|0.021000|\npacket|0.042333333\npacket|0.063666667\nstream|0.021000\n"
    )

    observed = audio_timeline_from_ffprobe_stdout(stdout)
    assert observed == _timeline()
    command = tonal_audio_timeline_probe_arguments(Path("source.mp4"))
    assert command[command.index("-of") + 1] == "compact=p=1:nk=1"
    for invalid in (
        stdout.rstrip("\n"),
        stdout + "packet|0.085\n",
        stdout.replace("stream|0.021000", "stream|0.021000|extra"),
        stdout.replace("packet|0.042333333", "unknown|0.042333333"),
    ):
        with pytest.raises(ValueError, match="timeline probe is incomplete"):
            audio_timeline_from_ffprobe_stdout(invalid)


def test_audio_timeline_digest_preserves_subnanosecond_packet_identity() -> None:
    def parse(middle: str) -> TonalAudioTimelineV1:
        return audio_timeline_from_ffprobe_stdout(
            json.dumps(
                {
                    "streams": [{"start_time": "0"}],
                    "packets": [
                        {"pts_time": "0"},
                        {"pts_time": middle},
                        {"pts_time": "0.042666667"},
                    ],
                }
            )
        )

    first = parse("0.0213333331")
    second = parse("0.0213333332")
    equivalent = parse("0.02133333310")

    assert first.first_normalized_pts_seconds == second.first_normalized_pts_seconds
    assert first.last_normalized_pts_seconds == second.last_normalized_pts_seconds
    assert first.normalized_pts_sha256 != second.normalized_pts_sha256
    assert first.normalized_pts_sha256 == equivalent.normalized_pts_sha256


def test_v2_evidence_cannot_claim_v3_without_boundary_control_fields() -> None:
    action, _config = _qualified_action()
    payload = dict(
        cast(dict[str, JsonValue], action.parameters["encoded_candidate_qualification"])
    )
    for field in (
        "boundary_control_sha256",
        "boundary_control_audio_topology",
        "boundary_control_audio_timeline",
        "profile_candidate_audio_timelines",
        "combined_audio_timeline",
    ):
        payload.pop(field)

    with pytest.raises(ValueError):
        TonalEncodedQualificationEvidenceV2.model_validate(payload)

    payload["schema_version"] = "2"
    legacy = TonalEncodedQualificationEvidenceV2.model_validate(payload)
    assert legacy.schema_version == "2"


def test_native_provider_probes_one_strict_audio_topology_without_shell() -> None:
    commands: list[tuple[str, ...]] = []
    stdout = json.dumps(
        {
            "streams": [
                {
                    "codec_name": "aac",
                    "codec_tag_string": "mp4a",
                    "profile": "LC",
                    "sample_fmt": "fltp",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "time_base": "1/48000",
                }
            ]
        }
    )

    def runner(arguments: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        commands.append(arguments)
        return CommandResult(0, "", stdout)

    provider = NativeMediaMeasurementProvider(
        ffprobe="ffprobe-8.1.2",
        command_runner=runner,
    )

    measured = provider.inspect_tonal_audio_topology(
        Path("中文 output.mp4"), lambda: False
    )

    assert TonalAudioTopologyV2.model_validate(measured) == _topology()
    assert commands == [
        (
            "ffprobe-8.1.2",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            (
                "stream=codec_name,codec_tag_string,profile,sample_fmt,"
                "sample_rate,channels,channel_layout,time_base"
            ),
            "-of",
            "json",
            "中文 output.mp4",
        )
    ]


def test_runtime_binding_rejects_hash_topology_and_mapping_drift() -> None:
    action, _config = _qualified_action()
    evidence = TonalEncodedQualificationEvidenceV3.model_validate_json(
        json.dumps(action.parameters["encoded_candidate_qualification"])
    )
    mappings = (SourceMapping(0.0, 3.0, 0.0, 3.0, "staging/faithful.mp4"),)

    validate_tonal_runtime_parent(
        evidence,
        mappings,
        parent_sha256=evidence.parent_sha256,
        parent_audio_topology=evidence.parent_audio_topology,
    )
    validate_tonal_runtime_candidate(
        evidence,
        candidate_sha256=str(evidence.combined_candidate_sha256),
        candidate_audio_topology=evidence.combined_audio_topology or _topology(),
    )

    with pytest.raises(ValueError, match="parent differs"):
        validate_tonal_runtime_parent(
            evidence,
            mappings,
            parent_sha256="9" * 64,
            parent_audio_topology=evidence.parent_audio_topology,
        )
    with pytest.raises(ValueError, match="parent differs"):
        validate_tonal_runtime_parent(
            evidence,
            (SourceMapping(0.0, 3.0, 0.1, 3.1, "staging/faithful.mp4"),),
            parent_sha256=evidence.parent_sha256,
            parent_audio_topology=evidence.parent_audio_topology,
        )
    with pytest.raises(ValueError, match="candidate differs"):
        validate_tonal_runtime_candidate(
            evidence,
            candidate_sha256="8" * 64,
            candidate_audio_topology=evidence.combined_audio_topology or _topology(),
        )
    altered_topology = _topology(sample_rate_hz=44100)
    with pytest.raises(ValueError, match="candidate differs"):
        validate_tonal_runtime_candidate(
            evidence,
            candidate_sha256=str(evidence.combined_candidate_sha256),
            candidate_audio_topology=altered_topology,
        )


def test_full_interval_raw_qualification_rejects_infeasible_high_amplitude() -> None:
    sample_rate_hz = 48_000
    times = np.arange(3 * sample_rate_hz, dtype=np.float64) / sample_rate_hz
    event = (times >= 1.0) & (times < 2.0)
    config = TonalInterferenceConfig()
    tone = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=880.0,
        confidence=0.99,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-3.0,
        local_peak_over_baseline_db=57.0,
        persistence_window_count=40,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )

    def qualify(amplitude: float) -> tuple[InterferenceTone, ...]:
        mono = 0.08 * np.sin(2.0 * np.pi * 220.0 * times)
        mono[event] += amplitude * np.sin(2.0 * np.pi * 880.0 * times[event])
        samples = np.column_stack((mono, mono))
        return qualify_tonal_render_profiles(
            samples,
            sample_rate_hz,
            (tone,),
            config,
        )

    feasible = qualify(0.16)
    assert len(feasible) == 1
    qualification = feasible[0].render_qualification
    assert qualification is not None
    assert qualification.boundary_mode == "full_interval_v1"
    assert qualification.notch_q == 8.0
    assert qualification.minimum_target_reduction_db >= config.attenuation_db
    assert qualification.maximum_boundary_adjacent_delta <= (
        config.max_boundary_adjacent_delta
    )
    assert qualify(0.7) == ()


def test_fixed_native_tonal_tools_come_from_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tool_root = tmp_path / "arbitrary checkout depth" / "fixed tools"
    tool_root.mkdir(parents=True)
    ffmpeg = tool_root / "ffmpeg.exe"
    ffprobe = tool_root / "ffprobe.exe"
    ffmpeg.write_bytes(b"fixed ffmpeg")
    ffprobe.write_bytes(b"fixed ffprobe")
    monkeypatch.setenv("VIDEOSCOPE_TEST_FFMPEG", str(ffmpeg))
    monkeypatch.setenv("VIDEOSCOPE_TEST_FFPROBE", str(ffprobe))

    assert _fixed_native_tonal_tools_from_environment() == (ffmpeg, ffprobe)


def _fixed_native_tonal_tools_from_environment() -> tuple[Path, Path]:
    raw_ffmpeg = os.environ.get("VIDEOSCOPE_TEST_FFMPEG")
    raw_ffprobe = os.environ.get("VIDEOSCOPE_TEST_FFPROBE")
    assert raw_ffmpeg, "VIDEOSCOPE_TEST_FFMPEG must name fixed FFmpeg 8.1.2"
    assert raw_ffprobe, "VIDEOSCOPE_TEST_FFPROBE must name fixed ffprobe 8.1.2"
    ffmpeg = Path(raw_ffmpeg)
    ffprobe = Path(raw_ffprobe)
    assert ffmpeg.is_file(), "VIDEOSCOPE_TEST_FFMPEG is not a regular file"
    assert ffprobe.is_file(), "VIDEOSCOPE_TEST_FFPROBE is not a regular file"
    return ffmpeg, ffprobe


def test_native_fixed_8_1_2_encoded_qualification_matches_final_verifier(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _fixed_native_tonal_tools_from_environment()
    version = subprocess.run(
        [str(ffmpeg), "-version"],
        shell=False,
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout.decode("utf-8", "replace")
    assert "ffmpeg version 8.1.2" in version

    source = tmp_path / "中文 encoded qualification source.mp4"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x48:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            (
                "aevalsrc=0.08*sin(2*PI*220*t)+"
                "if(between(t\\,1\\,2)\\,0.16*sin(2*PI*880*t)\\,0):"
                "s=48000:d=3"
            ),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-shortest",
            str(source),
        ],
        shell=False,
        check=True,
        capture_output=True,
        timeout=60,
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    tonal_config = TonalInterferenceConfig()
    metadata = VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="h264",
        width=64,
        height=48,
        duration_seconds=3.0,
        average_frame_rate=10.0,
        estimated_frame_count=30,
        has_audio=True,
        file_size_bytes=source.stat().st_size,
    )
    executor = NativeRescueExecutor(ffmpeg=str(ffmpeg), ffprobe=str(ffprobe))
    measured_profiles = executor.measure_tonal_interference(
        source,
        tmp_path / "assessment",
        metadata,
        tonal_config,
        lambda: False,
    )
    qualified_profiles = tuple(
        measured
        for measured in measured_profiles
        if measured.render_qualification is not None
    )
    assert len(qualified_profiles) == 1
    profile = qualified_profiles[0]
    damage = DamageInterval(
        id=make_damage_id(
            source_hash,
            "audio:0",
            DamageKind.AUDIO_NOISE,
            profile.start_seconds,
            profile.end_seconds,
        ),
        stream_id="audio:0",
        kind=DamageKind.AUDIO_NOISE,
        start_seconds=profile.start_seconds,
        end_seconds=profile.end_seconds,
    )
    planner_inputs: dict[str, Any] = {
        "metadata": metadata,
        "damage_map": MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=3.0,
            scan_coverage=((0.0, 3.0),),
            intervals=(damage,),
        ),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
        "assessment_parameters": {
            "tonal_interference_measurements": [
                {
                    "source_ranges": [[profile.start_seconds, profile.end_seconds]],
                    "algorithm_version": "1",
                    "interference_profiles": [profile.model_dump(mode="json")],
                    "config": tonal_config.model_dump(mode="json"),
                }
            ]
        },
    }
    draft = build_rescue_plan(**planner_inputs)
    provider = NativeMediaMeasurementProvider(ffmpeg=str(ffmpeg), ffprobe=str(ffprobe))
    evidence = NativeTonalCandidateQualifier(
        executor=executor,
        measurement_provider=provider,
    ).qualify(draft, source, tmp_path / "qualification", lambda: False)
    rescue_serialization.write_tonal_encoded_qualification_json(
        evidence, tmp_path / "tonal-qualification-evidence-private.json"
    )
    assert evidence.passed
    final = build_rescue_plan(
        **planner_inputs,
        tonal_qualification=evidence,
        require_tonal_qualification=True,
    )
    execution = executor.execute_faithful(
        final, source, tmp_path / "execution", lambda: False
    )
    restored = executor.execute_faithful_restoration(
        final, execution, tmp_path / "execution", lambda: False
    )
    output = Path(restored.output_path)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == (
        evidence.combined_candidate_sha256
    )
    report = RescueVerifier(measurement_provider=provider).verify(
        source,
        output,
        None,
        final,
        restored.source_mappings,
        faithful_render_mode=restored.render_mode,
        verification_controls=restored.verification_controls,
    )
    tonal_checks = {
        check.check_id: check
        for check in report.checks
        if check.artifact == "faithful"
        and check.check_id
        in {"tonal_interference_reduction", "tonal_boundary_transient"}
    }
    assert set(tonal_checks) == {
        "tonal_interference_reduction",
        "tonal_boundary_transient",
    }
    assert all(
        check.status is RescueVerificationStatus.PASSED
        and check.required
        and check.measured.get("measurement_valid") is True
        for check in tonal_checks.values()
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
