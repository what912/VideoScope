"""Cross-track pure/fake lifecycle contracts for bounded V15 qualification."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from tests.rescue.test_artifacts import (
    _publish as _publish_fake_artifacts,
)
from tests.rescue.test_artifacts import (
    _report as _fake_verification_report,
)
from tests.rescue.test_artifacts import (
    _stage as _stage_fake_artifact,
)
from tests.rescue.test_pipeline import (
    _CandidateQualifier,
    _NoPassStabilizationQualifier,
    _pipeline,
    _prepare_confirm_execute,
    _SharpenAssessmentService,
    _StabilizationParentProvider,
    _TonalAssessmentService,
    _transition_stabilization_planner,
    _Verifier,
)
from tests.rescue.test_tonal_qualification import (
    _FakeExecutor as _FakeTonalExecutor,
)
from tests.rescue.test_tonal_qualification import (
    _FakeMeasurementProvider as _FakeTonalMeasurementProvider,
)
from videoscope.rescue.artifacts import RescueArtifactLayout
from videoscope.rescue.assessment import RescueAssessmentBundle
from videoscope.rescue.errors import RescueArtifactError
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    RescueActionKind,
    RescuePlan,
    RescueVerificationStatus,
    make_damage_id,
)
from videoscope.rescue.qualification import (
    SHARPEN_QUALIFICATION_LIMITATION,
    SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION,
    SharpenQualificationEvidenceV1,
)
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
    StabilizationImmediateParentHandle,
    StabilizationProfileMeasurementV1,
    StabilizationQualificationMetricsV1,
    build_stabilization_qualification_evidence,
    stabilization_qualification_thresholds,
)
from videoscope.rescue.tonal import InterferenceTone
from videoscope.rescue.tonal_qualification import (
    TONAL_ENCODED_QUALIFICATION_LIMITATION,
    TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION,
    NativeTonalCandidateQualifier,
)


class _CombinedQualificationAssessmentService:
    def assess(
        self,
        _source: Path,
        source_hash: str,
        *_args: object,
    ) -> RescueAssessmentBundle:
        sharpen = _SharpenAssessmentService().assess()
        tonal = _TonalAssessmentService().assess()
        actual_pts = tuple(0.5 + index / 96.0 for index in range(96))
        transforms = tuple(
            MotionTransform(
                timestamp_seconds=timestamp,
                translation_x=0.25 if index % 2 else -0.25,
                translation_y=0.1,
                rotation_degrees=0.0,
                scale=1.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
                semantics="frame_correction",
            )
            for index, timestamp in enumerate(actual_pts)
        )
        stabilization_config = StabilizationConfig(
            frame_width=16,
            frame_height=16,
            accepted_ranges=((0.5, 1.5),),
        )
        intervals = tuple(
            DamageInterval(
                id=make_damage_id(source_hash, stream_id, kind, start, end),
                stream_id=stream_id,
                kind=kind,
                start_seconds=start,
                end_seconds=end,
                description="Observable integration-test interval.",
            )
            for stream_id, kind, start, end in (
                ("video:0", DamageKind.SOFT_DETAIL, 0.5, 1.0),
                ("audio:0", DamageKind.AUDIO_NOISE, 0.5, 1.0),
                ("video:0", DamageKind.SHAKE, 0.5, 1.5),
            )
        )
        return RescueAssessmentBundle(
            visual_assessment=sharpen.visual_assessment,
            stabilization_assessment=StabilizationAssessment(
                recommended=True,
                reason="measured_transition_anchor_motion",
                crop_ratio=0.02,
                parameters={
                    "affected_ranges": [[0.5, 1.5]],
                    "method": "transition_anchor_v1",
                    "algorithm_version": "1",
                    "estimator_algorithm_version": "transition_anchor_v1",
                    "transition_range": [0.5, 1.0],
                    "following_anchor_range": [1.0, 1.5],
                    "transition_correction_count": 96,
                    "config": stabilization_config.model_dump(mode="json"),
                },
                transforms=transforms,
            ),
            evidence_intervals=intervals,
            parameters=tonal.parameters,
        )


class _CompleteWindowTonalMeasurementProvider:
    def __init__(
        self,
        *,
        passing_q: float | None,
        combined_reduction_db: float | None = None,
    ) -> None:
        self._delegate = _FakeTonalMeasurementProvider(
            passing_q=passing_q,
            combined_reduction_db=combined_reduction_db,
        )

    def inspect_tonal_audio_topology(
        self, path: Any, cancellation_callback: Any
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._delegate.inspect_tonal_audio_topology(path, cancellation_callback),
        )

    def inspect_tonal_audio_timeline(
        self, path: Any, cancellation_callback: Any
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._delegate.inspect_tonal_audio_timeline(path, cancellation_callback),
        )

    def measure_perceptual_restoration(
        self, *args: Any, **kwargs: Any
    ) -> dict[str, float]:
        measured = cast(
            dict[str, float],
            self._delegate.measure_perceptual_restoration(*args, **kwargs),
        )
        parameters = cast(dict[str, Any], args[5])
        profiles = tuple(
            InterferenceTone.model_validate_json(json.dumps(item))
            for item in parameters["interference_profiles"]
        )
        measured["measured_windows"] = float(profiles[0].persistence_window_count)
        if Path(cast(Path, args[2])).name == "combined.mp4":
            for index, profile in enumerate(profiles):
                measured[f"profile_{index}_measured_windows"] = float(
                    profile.persistence_window_count
                )
        return measured


class _PassingStabilizationQualifier:
    def __init__(self, *, candidate_sha256: str = "3" * 64) -> None:
        self.candidate_sha256 = candidate_sha256

    def qualify(
        self,
        draft_plan: RescuePlan,
        parent: StabilizationImmediateParentHandle,
        _work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> object:
        assert not cancellation_callback()
        action = next(
            item
            for item in draft_plan.actions
            if item.kind is RescueActionKind.STABILIZE
        )
        profile = draft_plan.effective_config.stabilization_qualification_profiles[0]
        config = StabilizationConfig.model_validate_json(
            json.dumps(action.parameters["config"])
        )
        transition_frames = float(
            sum(0.5 <= value < 1.0 for value in parent.actual_pts)
        )
        measurement = StabilizationProfileMeasurementV1(
            profile=profile,
            parent_sha256=parent.sha256,
            control_sha256="1" * 64,
            candidate_sha256=self.candidate_sha256,
            encode_contract=parent.encode_contract,
            source_ranges=action.source_ranges,
            actual_pts=parent.actual_pts,
            parent_normalized_pts_digest=parent.normalized_pts_digest,
            control_normalized_pts_digest=parent.normalized_pts_digest,
            candidate_normalized_pts_digest=parent.normalized_pts_digest,
            parent_stream_topology_digest=parent.stream_topology_digest,
            control_stream_topology_digest=parent.stream_topology_digest,
            candidate_stream_topology_digest=parent.stream_topology_digest,
            parent_frame_count=parent.frame_count,
            control_frame_count=parent.frame_count,
            candidate_frame_count=parent.frame_count,
            action_parameters=action.parameters,
            metrics=StabilizationQualificationMetricsV1(
                range_coverage_ratio=1.0,
                expected_frames=float(parent.frame_count),
                reliable_transforms=float(parent.frame_count),
                residual_median_pixels=0.1,
                residual_p90_pixels=0.2,
                crop_ratio=0.02,
                transition_consensus_coverage_ratio=1.0,
                transition_consensus_p90_pixels=0.1,
                transition_seam_residual_pixels=0.1,
                transition_expected_frames=transition_frames,
                transition_reliable_frames=transition_frames,
                transition_boundary_path_residual_pixels=0.1,
            ),
            thresholds=stabilization_qualification_thresholds(config),
        )
        return build_stabilization_qualification_evidence(
            draft_plan,
            (measurement,),
            parent=parent,
        )


class _ChangedSharpenEvidenceQualifier:
    def __init__(self) -> None:
        self._delegate = _CandidateQualifier()

    def qualify(self, *args: Any, **kwargs: Any) -> object:
        evidence = SharpenQualificationEvidenceV1.model_validate(
            self._delegate.qualify(*args, **kwargs)
        )
        payload = evidence.model_dump(mode="python")
        measurements = cast(list[dict[str, Any]], payload["profile_measurements"])
        measurements[0]["candidate_sha256"] = "f" * 64
        return SharpenQualificationEvidenceV1.model_validate(payload)


def _qualified_plan(
    tmp_path: Path,
    *,
    clarity_changed: bool = False,
    tonal_reduction_db: float = 25.0,
    stabilization_candidate_sha256: str | None = None,
) -> tuple[RescuePlan, Path, bytes, Path, bytes]:
    downloads = tmp_path / "Downloads"
    downloads.mkdir(parents=True)
    download_sentinel = downloads / "existing-download.bin"
    download_sentinel.write_bytes(b"leave downloads unchanged")
    tonal_provider = _CompleteWindowTonalMeasurementProvider(
        passing_q=12.0,
        combined_reduction_db=tonal_reduction_db,
    )
    pipeline_inputs: dict[str, Any] = {
        "strategy": "balanced",
        "assessment_service": _CombinedQualificationAssessmentService(),
        "candidate_qualifier": (
            _ChangedSharpenEvidenceQualifier()
            if clarity_changed
            else _CandidateQualifier()
        ),
        "tonal_candidate_qualifier": NativeTonalCandidateQualifier(
            executor=_FakeTonalExecutor(),
            measurement_provider=tonal_provider,
        ),
    }
    if stabilization_candidate_sha256 is not None:
        pipeline_inputs.update(
            {
                "stabilization_candidate_qualifier": (
                    _PassingStabilizationQualifier(
                        candidate_sha256=stabilization_candidate_sha256
                    )
                ),
                "stabilization_parent_provider": _StabilizationParentProvider(),
            }
        )
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path / "job",
        **pipeline_inputs,
    )
    source_bytes = source.read_bytes()
    preparation = pipeline.prepare(source)
    assert source.read_bytes() == source_bytes
    assert download_sentinel.read_bytes() == b"leave downloads unchanged"
    assert not (tmp_path / "job" / "输出 job" / "rescue-output").exists()
    pipeline.abort(preparation)
    return (
        preparation.plan,
        source,
        source_bytes,
        download_sentinel,
        download_sentinel.read_bytes(),
    )


@pytest.mark.parametrize(
    ("changed_track", "expected_kind"),
    [
        ("clarity", RescueActionKind.SHARPEN),
        ("tonal", RescueActionKind.DENOISE_AUDIO),
        ("stabilization", RescueActionKind.STABILIZE),
    ],
)
def test_cross_track_evidence_changes_only_its_action_id_and_plan_digest(
    tmp_path: Path,
    changed_track: str,
    expected_kind: RescueActionKind,
) -> None:
    """Catches a track-local evidence change drifting unrelated actions."""
    baseline, *_ = _qualified_plan(
        tmp_path / "baseline",
        stabilization_candidate_sha256=(
            "3" * 64 if changed_track == "stabilization" else None
        ),
    )
    changed, *_ = _qualified_plan(
        tmp_path / changed_track,
        clarity_changed=changed_track == "clarity",
        tonal_reduction_db=25.1 if changed_track == "tonal" else 25.0,
        stabilization_candidate_sha256=(
            "4" * 64 if changed_track == "stabilization" else None
        ),
    )
    baseline_ids = {action.kind: action.id for action in baseline.actions}
    changed_ids = {action.kind: action.id for action in changed.actions}

    assert baseline.plan_digest != changed.plan_digest
    assert baseline_ids.keys() == changed_ids.keys()
    assert {
        kind for kind in baseline_ids if baseline_ids[kind] != changed_ids[kind]
    } == {expected_kind}
    assert RescueActionKind.REMUX in baseline_ids
    assert RescueActionKind.VERIFY in baseline_ids
    assert RescueActionKind.DEBLUR not in baseline_ids


class _RecordingPreviewBuilder:
    def __init__(self) -> None:
        self.plans: list[RescuePlan] = []

    def build(self, plan: RescuePlan, *_args: object) -> None:
        self.plans.append(plan)


@pytest.mark.parametrize(
    ("case", "expected_kind", "expected_limitation"),
    [
        (
            "clarity_no_pass",
            RescueActionKind.SHARPEN,
            SHARPEN_QUALIFICATION_LIMITATION,
        ),
        (
            "clarity_unavailable",
            RescueActionKind.SHARPEN,
            SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION,
        ),
        (
            "tonal_no_pass",
            RescueActionKind.DENOISE_AUDIO,
            TONAL_ENCODED_QUALIFICATION_LIMITATION,
        ),
        (
            "tonal_unavailable",
            RescueActionKind.DENOISE_AUDIO,
            TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION,
        ),
    ],
)
def test_no_pass_and_unavailable_tracks_are_omitted_before_preview(
    tmp_path: Path,
    case: str,
    expected_kind: RescueActionKind,
    expected_limitation: str,
) -> None:
    """Catches an omitted qualification being previewed as selected."""

    class UnavailableQualifier:
        def qualify(self, *_args: object) -> object:
            raise RuntimeError("qualification provider unavailable")

    clarity = case.startswith("clarity")
    if clarity:
        qualifier_inputs: dict[str, Any] = {
            "candidate_qualifier": (
                _CandidateQualifier(pass_profiles=False)
                if case.endswith("no_pass")
                else UnavailableQualifier()
            )
        }
    else:
        qualifier_inputs = {
            "tonal_candidate_qualifier": (
                NativeTonalCandidateQualifier(
                    executor=_FakeTonalExecutor(),
                    measurement_provider=_CompleteWindowTonalMeasurementProvider(
                        passing_q=None
                    ),
                )
                if case.endswith("no_pass")
                else UnavailableQualifier()
            )
        }
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path / case,
        strategy="balanced",
        damage_kind=(DamageKind.SOFT_DETAIL if clarity else DamageKind.AUDIO_NOISE),
        assessment_service=(
            _SharpenAssessmentService() if clarity else _TonalAssessmentService()
        ),
        **qualifier_inputs,
    )
    preview = _RecordingPreviewBuilder()
    pipeline._dependencies.preview_builder = preview
    source_bytes = source.read_bytes()

    preparation = pipeline.prepare(source)

    assert all(action.kind is not expected_kind for action in preparation.plan.actions)
    assert preparation.plan.assessment_limitations.count(expected_limitation) == 1
    assert preview.plans == [preparation.plan]
    assert all(action.kind is not expected_kind for action in preview.plans[0].actions)
    assert source.read_bytes() == source_bytes
    assert not (tmp_path / case / "输出 job" / "rescue-output").exists()
    pipeline.abort(preparation)


def test_optional_stabilization_no_pass_and_unavailable_are_identity_stable(
    tmp_path: Path,
) -> None:
    """Catches optional no-pass evidence rewriting the existing GREEN fallback."""
    plans: list[RescuePlan] = []
    for case, qualifier, parent in (
        ("unavailable", None, None),
        (
            "no-pass",
            _NoPassStabilizationQualifier(),
            _StabilizationParentProvider(),
        ),
    ):
        inputs: dict[str, Any] = {}
        if qualifier is not None and parent is not None:
            inputs.update(
                {
                    "stabilization_candidate_qualifier": qualifier,
                    "stabilization_parent_provider": parent,
                }
            )
        pipeline, source, _executor, _verifier, _damage = _pipeline(
            tmp_path / case,
            strategy="balanced",
            planner=_transition_stabilization_planner,
            **inputs,
        )
        preview = _RecordingPreviewBuilder()
        pipeline._dependencies.preview_builder = preview
        source_bytes = source.read_bytes()
        preparation = pipeline.prepare(source)
        plans.append(preparation.plan)
        action = next(
            item
            for item in preparation.plan.actions
            if item.kind is RescueActionKind.STABILIZE
        )
        assert "stabilization_qualification" not in action.parameters
        assert preview.plans == [preparation.plan]
        assert source.read_bytes() == source_bytes
        assert not (tmp_path / case / "输出 job" / "rescue-output").exists()
        pipeline.abort(preparation)

    assert plans[0] == plans[1]


def test_selected_track_evidence_stays_in_its_own_action_parameters(
    tmp_path: Path,
) -> None:
    """Catches one track's evidence satisfying or substituting another track."""
    plan, *_ = _qualified_plan(
        tmp_path,
        stabilization_candidate_sha256="3" * 64,
    )
    by_kind = {action.kind: action for action in plan.actions}
    sharpen = by_kind[RescueActionKind.SHARPEN].parameters
    tonal = by_kind[RescueActionKind.DENOISE_AUDIO].parameters
    stabilization = by_kind[RescueActionKind.STABILIZE].parameters

    assert "qualification" in sharpen
    assert "encoded_candidate_qualification" not in sharpen
    assert "stabilization_qualification" not in sharpen
    assert "encoded_candidate_qualification" in tonal
    assert "qualification" not in tonal
    assert "stabilization_qualification" not in tonal
    assert "stabilization_qualification" in stabilization
    assert "qualification" not in stabilization
    assert "encoded_candidate_qualification" not in stabilization
    for kind in (RescueActionKind.REMUX, RescueActionKind.VERIFY):
        assert not any("qualification" in key for key in by_kind[kind].parameters)


def test_optional_needs_review_check_stays_visible_and_blocks_its_artifact(
    tmp_path: Path,
) -> None:
    """Catches optional review evidence disappearing or publishing as passed."""
    source = tmp_path / "source.mp4"
    downloads = tmp_path / "Downloads" / "existing.bin"
    source.write_bytes(b"source")
    downloads.parent.mkdir()
    downloads.write_bytes(b"existing download")
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage_fake_artifact(layout, "faithful-rescue.mp4", b"faithful")
    _stage_fake_artifact(layout, "improved-viewing.mp4", b"needs review")
    report = _fake_verification_report(
        layout,
        improved_status=RescueVerificationStatus.NEEDS_REVIEW,
    )

    optional = next(check for check in report.checks if not check.required)
    published = _publish_fake_artifacts(layout, report)

    assert optional.check_id == "side_effect_review"
    assert optional.status is RescueVerificationStatus.NEEDS_REVIEW
    assert [artifact.relative_path for artifact in published] == ["faithful-rescue.mp4"]
    assert not (layout.public_root / "improved-viewing.mp4").exists()
    assert source.read_bytes() == b"source"
    assert downloads.read_bytes() == b"existing download"


@pytest.mark.parametrize(
    ("improved_status", "expected_public"),
    [
        (RescueVerificationStatus.PASSED, True),
        (RescueVerificationStatus.NEEDS_REVIEW, False),
        (RescueVerificationStatus.FAILED, False),
    ],
)
def test_public_documents_project_only_physically_published_media(
    tmp_path: Path,
    improved_status: RescueVerificationStatus,
    expected_public: bool,
) -> None:
    """Catches public inventories or links advertising a private candidate."""
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.DARK,
        verifier=_Verifier(improved_status=improved_status),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.public_root is not None
    public_root = result.public_root
    physical_media = sorted(path.name for path in public_root.glob("*.mp4"))
    expected_media = (
        ["faithful-rescue.mp4", "improved-viewing.mp4"]
        if expected_public
        else ["faithful-rescue.mp4"]
    )
    assert physical_media == expected_media
    changes = json.loads((public_root / "changes.json").read_text(encoding="utf-8"))
    technical = json.loads(
        (public_root / "technical-report.json").read_text(encoding="utf-8")
    )
    verification = json.loads(
        (public_root / "verification-report.json").read_text(encoding="utf-8")
    )
    html = (public_root / "report.html").read_text(encoding="utf-8")
    for artifact_inventory in (
        changes["artifacts"],
        technical["artifacts"],
        technical["verification"]["artifacts"],
        verification["artifacts"],
    ):
        assert [item["relative_path"] for item in artifact_inventory] == expected_media
    improved_checks = [
        check for check in verification["checks"] if check["artifact"] == "improved"
    ]
    assert improved_checks
    assert verification["improved_status"] == improved_status.value
    assert any(check["status"] == improved_status.value for check in improved_checks)
    assert ('href="improved-viewing.mp4"' in html) is expected_public
    if not expected_public:
        assert "improved-viewing.mp4" not in html


@pytest.mark.parametrize("track", ["clarity", "tonal"])
@pytest.mark.parametrize(
    "failure",
    [
        RescueArtifactError("private qualification path escaped"),
        OSError("private qualification cleanup failed"),
    ],
    ids=["path-escape", "cleanup"],
)
def test_qualification_artifact_failure_fails_closed_instead_of_previewing(
    tmp_path: Path,
    track: str,
    failure: Exception,
) -> None:
    """Catches integrity/cleanup failures being mislabeled as unavailable."""

    class ArtifactFailingQualifier:
        def qualify(self, *_args: object) -> object:
            raise failure

    inputs = (
        {
            "damage_kind": DamageKind.SOFT_DETAIL,
            "assessment_service": _SharpenAssessmentService(),
            "candidate_qualifier": ArtifactFailingQualifier(),
        }
        if track == "clarity"
        else {
            "damage_kind": DamageKind.AUDIO_NOISE,
            "assessment_service": _TonalAssessmentService(),
            "tonal_candidate_qualifier": ArtifactFailingQualifier(),
        }
    )
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path / track,
        strategy="balanced",
        **inputs,
    )

    with pytest.raises(
        RescueArtifactError,
        match="A Video Rescue artifact could not be handled safely",
    ):
        pipeline.prepare(source)
