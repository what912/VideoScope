"""Private/public Rescue artifact layout and publication tests."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import JsonValue

import videoscope.rescue.artifacts as artifact_module
from videoscope.rescue.artifacts import (
    RescueArtifactLayout,
    build_damaged_segments_manifest,
    deterministic_segment_name,
    publish_verified_rescue,
)
from videoscope.rescue.errors import RescueArtifactError, RescueCancelledError
from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    RescueAction,
    RescueActionKind,
    RescueArtifact,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    canonical_video_encode_contract,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)
from videoscope.rescue.tonal_qualification import (
    TonalAudioEncodeContractV2,
    TonalAudioTimelineV1,
    TonalEncodedCandidateAttemptV2,
    TonalEncodedMetricsV2,
    TonalEncodedProfileQualificationV2,
    TonalEncodedQualificationEvidenceV3,
    TonalEncodedThresholdsV2,
    TonalRangeMappingV2,
    _qualification_for_q,
    audio_topology_from_ffprobe_stdout,
    qualified_tonal_action_parameters,
)


def _stage(layout: RescueArtifactLayout, name: str, content: bytes) -> Path:
    path = layout.private_root / "staging" / name
    path.write_bytes(content)
    return path


_REQUIRED_DOCUMENTS = (
    "rescue-plan.json",
    "damaged-segments.json",
    "changes.json",
    "verification-report.json",
    "technical-report.json",
    "report.html",
)


def _public_artifacts(*, include_improved: bool = False) -> tuple[str, ...]:
    media = (
        ("faithful-rescue.mp4", "improved-viewing.mp4")
        if include_improved
        else ("faithful-rescue.mp4",)
    )
    return (*_REQUIRED_DOCUMENTS, *media)


def _plan(
    *,
    include_improved: bool = False,
    public_artifacts: tuple[str, ...] | None = None,
) -> RescuePlan:
    action = RescueAction(
        id="remux",
        version="1",
        kind=RescueActionKind.REMUX,
        description="Write a faithful local copy.",
        source_ranges=((0.0, 4.0),),
        changes_content=False,
        requires_confirmation=False,
        strategy=RescueStrategy.BALANCED,
    )
    payload: dict[str, JsonValue] = {
        "input_hash": sha256(b"source").hexdigest(),
        "strategy": RescueStrategy.BALANCED,
        "effective_config": RescueEffectiveConfig().model_dump(mode="json"),
        "actions": [action.model_dump(mode="json")],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": list(
            public_artifacts
            if public_artifacts is not None
            else _public_artifacts(include_improved=include_improved)
        ),
        "damage_intervals": [],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _perceptual_action(
    kind: RescueActionKind,
    index: int,
    effective_config: RescueEffectiveConfig,
) -> RescueAction:
    parameters: dict[str, JsonValue]
    if kind is RescueActionKind.DEBLUR:
        parameters = {"algorithm_version": "1", "operations": [{}]}
    elif kind is RescueActionKind.DENOISE_AUDIO:
        tonal_config = TonalInterferenceConfig()
        tone = InterferenceTone(
            start_seconds=0.0,
            end_seconds=4.0,
            center_frequency_hz=880.0,
            confidence=0.95,
            baseline_before_dbfs=-60.0,
            baseline_after_dbfs=-60.0,
            peak_dbfs=-12.0,
            local_peak_over_baseline_db=48.0,
            persistence_window_count=80,
            frequency_standard_deviation_hz=0.05,
            channel_indices=(0,),
            attenuation_target_db=tonal_config.attenuation_db,
            render_qualification=TonalRenderQualification(
                boundary_mode="full_interval_v1",
                notch_q=8.0,
                complete_window_count=80,
                minimum_target_reduction_db=25.0,
                maximum_non_target_attenuation_db=0.1,
                maximum_boundary_energy_jump_db=0.1,
                maximum_boundary_crest_jump_db=0.1,
                maximum_boundary_adjacent_delta=0.01,
            ),
        )
        parameters = {
            "algorithm_version": effective_config.tonal_algorithm_version,
            "interference_profiles": [tone.model_dump(mode="json")],
            "config": tonal_config.model_dump(mode="json"),
        }
    else:
        parameters = {"algorithm_version": "1", "method": "anchor_v1"}
    parameters["video_encode_contract"] = canonical_video_encode_contract(
        effective_config
    ).model_dump(mode="json")
    return RescueAction(
        id=make_rescue_action_id(
            kind=kind,
            parameters=parameters,
            source_ranges=((0.0, 4.0),),
            strategy=RescueStrategy.BALANCED,
            version="1",
        ),
        version="1",
        kind=kind,
        description="Apply one confirmed perceptual restoration.",
        source_ranges=((0.0, 4.0),),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
        parameters=parameters,
    )


def _encoded_qualified_tonal_action(
    action: RescueAction,
    *,
    input_hash: str,
    effective_config: RescueEffectiveConfig,
) -> RescueAction:
    config = TonalInterferenceConfig()
    raw_profiles = cast(Any, action.parameters["interference_profiles"])
    raw_profile = InterferenceTone.model_validate_json(json.dumps(raw_profiles[0]))
    thresholds = TonalEncodedThresholdsV2(
        minimum_target_reduction_db=raw_profile.attenuation_target_db,
        maximum_non_target_attenuation_db=(config.max_non_target_band_attenuation_db),
        maximum_boundary_energy_jump_db=config.max_boundary_energy_jump_db,
        maximum_boundary_crest_jump_db=config.max_boundary_crest_jump_db,
        maximum_boundary_adjacent_delta=config.max_boundary_adjacent_delta,
    )
    metrics = TonalEncodedMetricsV2(
        range_coverage_ratio=1.0,
        measured_windows=80,
        excluded_transition_windows=0,
        minimum_target_reduction_db=25.0,
        minimum_target_margin_db=1.0,
        maximum_non_target_attenuation_db=0.1,
        maximum_boundary_energy_jump_db=0.1,
        maximum_boundary_crest_jump_db=0.1,
        maximum_boundary_adjacent_delta=0.01,
    )
    topology = audio_topology_from_ffprobe_stdout(
        json.dumps(
            {
                "streams": [
                    {
                        "codec_name": "aac",
                        "codec_tag_string": "mp4a",
                        "profile": "LC",
                        "sample_fmt": "fltp",
                        "sample_rate": "48000",
                        "channels": 1,
                        "channel_layout": "mono",
                        "time_base": "1/48000",
                    }
                ]
            }
        )
    )
    notch_q = config.render_qualification_notch_q_values[0]
    attempt = TonalEncodedCandidateAttemptV2(
        notch_q=notch_q,
        candidate_sha256="2" * 64,
        candidate_audio_topology=topology,
        metrics=metrics,
        thresholds=thresholds,
    )
    timeline_tokens = ["0", "0.021333333"]
    timeline = TonalAudioTimelineV1(
        packet_count=2,
        first_normalized_pts_seconds=0.0,
        last_normalized_pts_seconds=0.021333333,
        normalized_pts_sha256=hashlib.sha256(
            json.dumps(timeline_tokens, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    )
    evidence = TonalEncodedQualificationEvidenceV3(
        input_hash=input_hash,
        draft_action_id=action.id,
        draft_parameters=dict(action.parameters),
        source_ranges=action.source_ranges,
        output_ranges=action.source_ranges,
        range_mappings=(
            TonalRangeMappingV2(
                source_start=0.0,
                source_end=4.0,
                output_start=0.0,
                output_end=4.0,
            ),
        ),
        audio_encode_contract=TonalAudioEncodeContractV2(
            parent_bitrate_kbps=effective_config.improved_audio_bitrate_kbps,
            candidate_bitrate_kbps=config.audio_bitrate_kbps,
        ),
        parent_sha256="1" * 64,
        parent_audio_topology=topology,
        boundary_control_sha256="4" * 64,
        boundary_control_audio_topology=topology,
        boundary_control_audio_timeline=timeline,
        profile_candidate_audio_timelines=((timeline,),),
        combined_audio_timeline=timeline,
        profile_qualifications=(
            TonalEncodedProfileQualificationV2(
                profile_index=0,
                attempts=(attempt,),
                selected_notch_q=notch_q,
            ),
        ),
        combined_candidate_sha256="3" * 64,
        combined_audio_topology=topology,
        combined_metrics=(metrics,),
        combined_thresholds=(thresholds,),
        selected_profiles=(_qualification_for_q(raw_profile, notch_q, metrics),),
    )
    parameters = qualified_tonal_action_parameters(evidence)
    return action.model_copy(
        update={
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
            "parameters": parameters,
        }
    )


def _plan_with_perceptual_actions(
    kinds: tuple[RescueActionKind, ...],
) -> RescuePlan:
    payload = _plan().model_dump(mode="json")
    effective_config = RescueEffectiveConfig.model_validate(payload["effective_config"])
    order = {kind: index for index, kind in enumerate(RescueActionKind)}
    perceptual = sorted(
        (
            _perceptual_action(kind, index, effective_config)
            for index, kind in enumerate(kinds)
        ),
        key=lambda action: order[action.kind],
    )
    perceptual = [
        _encoded_qualified_tonal_action(
            action,
            input_hash=payload["input_hash"],
            effective_config=effective_config,
        )
        if action.kind is RescueActionKind.DENOISE_AUDIO
        else action
        for action in perceptual
    ]
    actions = [
        *payload["actions"],
        *[action.model_dump(mode="json") for action in perceptual],
    ]
    payload["actions"] = actions
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _report(
    layout: RescueArtifactLayout,
    *,
    faithful_status: RescueVerificationStatus = RescueVerificationStatus.PASSED,
    improved_status: RescueVerificationStatus | None = None,
) -> RescueVerificationReport:
    checks: list[RescueVerificationCheck] = []
    artifacts: list[RescueArtifact] = []
    artifact_cases: tuple[
        tuple[
            Literal["faithful", "improved"],
            RescueVerificationStatus | None,
            str,
        ],
        ...,
    ] = (
        ("faithful", faithful_status, "faithful-rescue.mp4"),
        ("improved", improved_status, "improved-viewing.mp4"),
    )
    for artifact, status, name in artifact_cases:
        if status is None:
            continue
        for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS:
            check_status = (
                status if check_id == "decodable" else RescueVerificationStatus.PASSED
            )
            checks.append(
                RescueVerificationCheck(
                    check_id=check_id,
                    artifact=artifact,
                    status=check_status,
                    message="Measured check.",
                    measured={"measured": True},
                )
            )
        if status is RescueVerificationStatus.NEEDS_REVIEW:
            checks.append(
                RescueVerificationCheck(
                    check_id="side_effect_review",
                    artifact=artifact,
                    status=status,
                    message="Manual review required.",
                    measured={"measured": True},
                    required=False,
                )
            )
        staged = layout.private_root / "staging" / name
        artifacts.append(
            RescueArtifact(
                artifact_role=(
                    "faithful" if name == "faithful-rescue.mp4" else "improved"
                ),
                relative_path=name,
                sha256=sha256(staged.read_bytes()).hexdigest(),
                description="Measured candidate.",
            )
        )
    return RescueVerificationReport(
        plan_digest=_plan(
            include_improved=improved_status
            in {
                RescueVerificationStatus.PASSED,
                RescueVerificationStatus.NEEDS_REVIEW,
            }
        ).plan_digest,
        faithful_status=faithful_status,
        improved_status=improved_status,
        checks=tuple(checks),
        artifacts=tuple(artifacts),
        outcome=RescueOutcome.COMPLETED,
    )


def _documents(plan: RescuePlan) -> dict[str, object]:
    return {
        "changes.json": {"plan_digest": plan.plan_digest, "source_modified": False},
        "technical-report.json": {
            "plan_digest": plan.plan_digest,
            "limitations": ["Observed measurements only."],
        },
        "report.html": "<!doctype html><title>Video Rescue report</title>",
    }


def _publish(
    layout: RescueArtifactLayout,
    report: RescueVerificationReport,
    *,
    cancellation_callback: Callable[[], bool] = lambda: False,
    documents: dict[str, object] | None = None,
    plan: RescuePlan | None = None,
) -> tuple[RescueArtifact, ...]:
    selected_plan = plan or _plan(
        include_improved=report.improved_status
        in {
            RescueVerificationStatus.PASSED,
            RescueVerificationStatus.NEEDS_REVIEW,
        }
    )
    return publish_verified_rescue(
        layout,
        verification=report,
        plan=selected_plan,
        mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        damaged_ranges=(),
        public_documents=documents or _documents(selected_plan),
        cancellation_callback=cancellation_callback,
    )


def test_layout_uses_the_exact_private_and_public_contract(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "任务 空间")
    assert layout.private_root.name == "rescue-review-private"
    assert layout.public_root.name == "rescue-output"
    assert (layout.private_root / "damage-map-private.json").is_file()
    assert (layout.private_root / "previews").is_dir()
    assert (layout.private_root / "staging").is_dir()
    assert layout.validate_public_tree() == ()


@pytest.mark.parametrize(
    "value",
    [
        "../escape.mp4",
        "nested/../../escape.mp4",
        "/private/file.mp4",
        "C:/Users/private/file.mp4",
        r"C:\Users\private\file.mp4",
        r"\\server\share\file.mp4",
        ".",
        "rescue-review-private",
        "rescue-review-private/previews/x.mp4",
    ],
)
def test_public_manifest_rejects_private_absolute_and_parent_paths(
    tmp_path: Path, value: str
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    with pytest.raises(RescueArtifactError):
        layout.validate_public_manifest({"artifact_path": value})


def test_public_manifest_accepts_plain_text_and_safe_relative_artifacts(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    layout.validate_public_manifest(
        {"message": "Review recommended.", "artifact_path": "faithful-rescue.mp4"}
    )


def test_public_manifest_rejects_path_leak_in_object_key(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    with pytest.raises(RescueArtifactError):
        layout.validate_public_manifest({"C:/Users/private/file.mp4": "value"})


@pytest.mark.parametrize(
    "manifest",
    [
        {"message": "Inspect /etc/passwd before sharing."},
        {"message": "Observed at /var/lib/videoscope/cache.bin"},
        {"note /资料/用户/私密.mp4": "value"},
        {"message": r"Inspect C:\Users\private\clip.mp4"},
        {"message": r"Inspect \\server\share\clip.mp4"},
    ],
)
def test_manifest_rejects_embedded_generic_absolute_paths(
    tmp_path: Path, manifest: dict[str, str]
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    with pytest.raises(RescueArtifactError):
        layout.validate_public_manifest(manifest)


@pytest.mark.parametrize("absolute", ["//server/share", "//server/共享", "/"])
@pytest.mark.parametrize("location", ["key", "value"])
def test_manifest_rejects_posix_network_and_root_paths_everywhere(
    tmp_path: Path, absolute: str, location: str
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    token = absolute if absolute == "/" else f"Observed {absolute} locally"
    manifest = {token: "safe"} if location == "key" else {"message": token}
    with pytest.raises(RescueArtifactError):
        layout.validate_public_manifest(manifest)


def test_text_privacy_parser_allows_markup_ratios_and_report_relative_urls() -> None:
    artifact_module._validate_public_text(
        "<section><p>Codec h264/aac at 1/2 speed.</p>"
        '<a href="report.html#finding-1">Open</a>'
        '<img src="evidence/frame-0001.jpg" alt="Evidence"></section>'
    )


def test_text_privacy_parser_accepts_https_closing_tags_and_arithmetic_slash() -> None:
    artifact_module._validate_public_text("https://example.invalid/reference")
    artifact_module._validate_public_text("Measured ratio: 1 / 2")
    artifact_module._validate_public_text(
        '<section><p>Safe</p><a href="report.html#finding-1">Open</a>'
        '<img src="evidence/frame-0001.jpg" alt="Evidence"></section>'
    )


def test_manifest_allows_safe_report_relative_url(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    layout.validate_public_manifest({"report_url": "report.html#finding-1"})


@pytest.mark.parametrize("value", ["Inspect /etc/passwd", "Read /资料/私密.txt"])
def test_plain_text_privacy_parser_rejects_absolute_paths(value: str) -> None:
    with pytest.raises(RescueArtifactError):
        artifact_module._validate_public_text(value)


@pytest.mark.parametrize("value", ["//server/share", "//server/共享", "/"])
def test_plain_text_rejects_posix_network_and_root_paths(value: str) -> None:
    with pytest.raises(RescueArtifactError):
        artifact_module._validate_public_text(value)


@pytest.mark.parametrize("value", ["//server/share", "//server/共享", "/"])
def test_html_text_rejects_posix_network_and_root_paths(value: str) -> None:
    with pytest.raises(RescueArtifactError):
        artifact_module._validate_public_text(f"<p>Private path: {value}</p>")


def test_html_privacy_parser_rejects_unsafe_report_url() -> None:
    with pytest.raises(RescueArtifactError):
        artifact_module._validate_public_text('<a href="../private/report.html">x</a>')


def test_public_tree_rejects_unexpected_nested_link_and_hardlink_files(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    layout.public_root.mkdir(exist_ok=True)
    (layout.public_root / "surprise.txt").write_text("no", encoding="utf-8")
    with pytest.raises(RescueArtifactError):
        layout.validate_public_tree()
    (layout.public_root / "surprise.txt").unlink()

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    linked = layout.public_root / "faithful-rescue.mp4"
    try:
        linked.symlink_to(outside)
    except OSError:
        pass
    else:
        with pytest.raises(RescueArtifactError):
            layout.validate_public_tree()
        linked.unlink()

    try:
        os.link(outside, linked)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(RescueArtifactError):
        layout.validate_public_tree()


def test_layout_rejects_symlinked_job_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")
    with pytest.raises(RescueArtifactError):
        RescueArtifactLayout.create(alias / "job")


def test_layout_rejects_linked_private_component(tmp_path: Path) -> None:
    root = tmp_path / "job"
    private = root / "rescue-review-private"
    private.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (private / "staging").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")
    with pytest.raises(RescueArtifactError):
        RescueArtifactLayout.create(root)


def test_publish_keeps_faithful_when_improved_fails(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    _stage(layout, "improved-viewing.mp4", b"improved")
    published = _publish(
        layout,
        _report(layout, improved_status=RescueVerificationStatus.FAILED),
    )
    assert [item.relative_path for item in published] == ["faithful-rescue.mp4"]
    assert (layout.public_root / "faithful-rescue.mp4").read_bytes() == b"faithful"
    assert not (layout.public_root / "improved-viewing.mp4").exists()
    assert {path.name for path in layout.public_root.iterdir()} == {
        "rescue-plan.json",
        "faithful-rescue.mp4",
        "damaged-segments.json",
        "changes.json",
        "verification-report.json",
        "technical-report.json",
        "report.html",
    }


def test_publish_does_not_duplicate_faithful_only_restoration_as_improved(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"restored once")
    _stage(layout, "improved-viewing.mp4", b"duplicate")
    plan = _plan(public_artifacts=_public_artifacts())
    report = _report(
        layout,
        improved_status=RescueVerificationStatus.PASSED,
    ).model_copy(update={"plan_digest": plan.plan_digest})

    published = _publish(layout, report, plan=plan)

    assert [item.relative_path for item in published] == ["faithful-rescue.mp4"]
    assert not (layout.public_root / "improved-viewing.mp4").exists()


@pytest.mark.parametrize(
    "kinds",
    [
        (RescueActionKind.DEBLUR,),
        (RescueActionKind.DENOISE_AUDIO,),
        (RescueActionKind.STABILIZE,),
        (
            RescueActionKind.DEBLUR,
            RescueActionKind.DENOISE_AUDIO,
            RescueActionKind.STABILIZE,
        ),
    ],
)
def test_publish_recomputes_dynamic_required_policy_from_actual_plan(
    tmp_path: Path,
    kinds: tuple[RescueActionKind, ...],
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "dynamic policy")
    _stage(layout, "faithful-rescue.mp4", b"base-only verification")
    plan = _plan_with_perceptual_actions(kinds)
    forged = _report(layout).model_copy(update={"plan_digest": plan.plan_digest})

    with pytest.raises(RescueArtifactError):
        _publish(layout, forged, plan=plan)


def test_publish_allows_needs_review_improved_but_rejects_failed_faithful(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    _stage(layout, "improved-viewing.mp4", b"improved")
    published = _publish(
        layout,
        _report(
            layout,
            faithful_status=RescueVerificationStatus.FAILED,
            improved_status=RescueVerificationStatus.NEEDS_REVIEW,
        ),
    )
    assert published == ()
    assert not layout.public_root.exists()


def test_publication_cancellation_leaves_no_partial_public_output(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    _stage(layout, "improved-viewing.mp4", b"improved")
    calls = 0

    def cancel_during_staging() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(RescueCancelledError):
        _publish(
            layout,
            _report(layout, improved_status=RescueVerificationStatus.PASSED),
            cancellation_callback=cancel_during_staging,
        )
    assert not layout.public_root.exists()
    assert not any(layout.job_root.glob(".rescue-output-publish-*"))


def test_callback_failure_rolls_staged_media_back_without_public_output(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    staged = _stage(layout, "faithful-rescue.mp4", b"faithful")
    calls = 0

    def fail_after_first_check() -> bool:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise RuntimeError("callback failed")
        return False

    with pytest.raises(RescueArtifactError):
        _publish(
            layout,
            _report(layout),
            cancellation_callback=fail_after_first_check,
        )
    assert staged.read_bytes() == b"faithful"
    assert not layout.public_root.exists()


def test_publication_recomputes_report_bound_hash_before_commit(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    staged = _stage(layout, "faithful-rescue.mp4", b"faithful")
    report = _report(layout)
    staged.write_bytes(b"changed after verification")
    with pytest.raises(RescueArtifactError):
        _publish(layout, report)
    assert not layout.public_root.exists()


def test_publication_rejects_report_bound_to_another_plan(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    report = _report(layout).model_copy(update={"plan_digest": "f" * 64})
    with pytest.raises(RescueArtifactError):
        _publish(layout, report)
    assert not layout.public_root.exists()


@pytest.mark.parametrize(
    "documents",
    [
        {
            "changes.json": {"C:/Users/private/source.mp4": "value"},
            "technical-report.json": {"ok": True},
            "report.html": "<title>safe</title>",
        },
        {
            "changes.json": {"ok": True},
            "technical-report.json": {"path": "RESCUE-REVIEW-PRIVATE/x"},
            "report.html": "<title>safe</title>",
        },
        {
            "changes.json": {"ok": True},
            "technical-report.json": {"ok": True},
            "report.html": "<p>C:/Users/private/source.mp4</p>",
        },
    ],
)
def test_every_public_text_document_is_privacy_checked(
    tmp_path: Path, documents: dict[str, object]
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout), documents=documents)
    assert not layout.public_root.exists()


@pytest.mark.parametrize(
    "leak",
    [
        "/etc/passwd",
        "/var/lib/videoscope/private.json",
        "/资料/用户/私密.mp4",
        r"C:\Users\private\clip.mp4",
        r"\\server\share\clip.mp4",
    ],
)
def test_html_text_rejects_embedded_generic_absolute_paths(
    tmp_path: Path, leak: str
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    documents = _documents(_plan())
    documents["report.html"] = f"<!doctype html><p>Private source: {leak}</p>"
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout), documents=documents)
    assert not layout.public_root.exists()


def test_publication_rejects_plan_missing_required_documents(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    incomplete_plan = _plan(public_artifacts=("faithful-rescue.mp4",))
    report = _report(layout).model_copy(
        update={"plan_digest": incomplete_plan.plan_digest}
    )
    with pytest.raises(RescueArtifactError):
        _publish(layout, report, plan=incomplete_plan)
    assert not layout.public_root.exists()


def test_publication_rejects_plan_with_extra_or_different_artifacts(
    tmp_path: Path,
) -> None:
    for declared in (
        (*_public_artifacts(), "unexpected.bin"),
        tuple(name for name in _public_artifacts() if name != "report.html"),
    ):
        layout = RescueArtifactLayout.create(
            tmp_path / sha256_bytes(str(declared).encode())
        )
        _stage(layout, "faithful-rescue.mp4", b"faithful")
        plan = _plan(public_artifacts=declared)
        report = _report(layout).model_copy(update={"plan_digest": plan.plan_digest})
        with pytest.raises(RescueArtifactError):
            _publish(layout, report, plan=plan)
        assert not layout.public_root.exists()


def test_concurrent_staged_mutation_after_copy_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    staged = _stage(layout, "faithful-rescue.mp4", b"faithful")
    report = _report(layout)
    original_copy = artifact_module._copy_verified_file

    def mutate_after_copy(*args: Any, **kwargs: Any) -> Any:
        result = original_copy(*args, **kwargs)
        staged.write_bytes(b"concurrent mutation")
        return result

    monkeypatch.setattr(artifact_module, "_copy_verified_file", mutate_after_copy)
    with pytest.raises(RescueArtifactError):
        _publish(layout, report)
    assert not layout.public_root.exists()


def test_concurrent_staged_path_swap_after_copy_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    staged = _stage(layout, "faithful-rescue.mp4", b"faithful")
    report = _report(layout)
    original_copy = artifact_module._copy_verified_file

    def swap_after_copy(*args: Any, **kwargs: Any) -> Any:
        result = original_copy(*args, **kwargs)
        original = staged.with_suffix(".verified")
        staged.replace(original)
        staged.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(artifact_module, "_copy_verified_file", swap_after_copy)
    with pytest.raises(RescueArtifactError):
        _publish(layout, report)
    assert not layout.public_root.exists()


def test_transaction_mutation_after_validation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    original_validate = artifact_module._validate_complete_bundle

    def mutate_after_validation(*args: Any, **kwargs: Any) -> Any:
        result = original_validate(*args, **kwargs)
        transaction = args[1]
        (transaction / "report.html").write_text(
            "<!doctype html><p>/etc/passwd</p>", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(
        artifact_module, "_validate_complete_bundle", mutate_after_validation
    )
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))
    assert not layout.public_root.exists()


def test_transaction_file_replacement_after_validation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    original_validate = artifact_module._validate_complete_bundle

    def replace_after_validation(*args: Any, **kwargs: Any) -> Any:
        result = original_validate(*args, **kwargs)
        transaction = args[1]
        report = transaction / "report.html"
        original = transaction / "report-original.html"
        report.replace(original)
        report.write_bytes(original.read_bytes())
        original.unlink()
        return result

    monkeypatch.setattr(
        artifact_module, "_validate_complete_bundle", replace_after_validation
    )
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))
    assert not layout.public_root.exists()


def test_source_identity_is_rechecked_at_the_final_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    staged = _stage(layout, "faithful-rescue.mp4", b"faithful")
    original_snapshot = artifact_module._snapshot_transaction_tree
    calls = 0

    def swap_source_after_final_tree_snapshot(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original_snapshot(*args, **kwargs)
        if calls == 2:
            original = staged.with_suffix(".sealed")
            staged.replace(original)
            staged.write_bytes(original.read_bytes())
        return result

    monkeypatch.setattr(
        artifact_module,
        "_snapshot_transaction_tree",
        swap_source_after_final_tree_snapshot,
    )
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))
    assert not layout.public_root.exists()


def test_destination_race_requires_atomic_no_replace_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    atomic_rename = getattr(
        artifact_module, "_atomic_rename_directory_no_replace", None
    )
    assert atomic_rename is not None
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")

    def race_before_atomic_rename(source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "racer.txt").write_text("racer", encoding="utf-8")
        atomic_rename(source, destination)

    monkeypatch.setattr(
        artifact_module,
        "_atomic_rename_directory_no_replace",
        race_before_atomic_rename,
    )
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))
    assert (layout.public_root / "racer.txt").read_text(encoding="utf-8") == "racer"


def test_needs_review_improved_candidate_is_not_published(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    _stage(layout, "improved-viewing.mp4", b"improved")
    published = _publish(
        layout,
        _report(layout, improved_status=RescueVerificationStatus.NEEDS_REVIEW),
    )
    assert tuple(item.relative_path for item in published) == ("faithful-rescue.mp4",)
    assert {path.name for path in layout.public_root.iterdir()} == {
        "changes.json",
        "damaged-segments.json",
        "faithful-rescue.mp4",
        "report.html",
        "rescue-plan.json",
        "technical-report.json",
        "verification-report.json",
    }


def test_directory_rename_failure_never_exposes_partial_public_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")

    def fail_rename(_source: Path, _destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(artifact_module, "_rename_transaction", fail_rename)
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))
    assert not layout.public_root.exists()
    assert not any(layout.job_root.glob(".rescue-output-publish-*"))


def test_publication_rejects_staged_symlink_and_hardlink_alias(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    staged = layout.private_root / "staging" / "faithful-rescue.mp4"
    try:
        staged.symlink_to(outside)
    except OSError:
        pass
    else:
        with pytest.raises(RescueArtifactError):
            _publish(layout, _report(layout))
        staged.unlink()
    try:
        os.link(outside, staged)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))


def test_layout_identity_change_is_rejected_before_publication(tmp_path: Path) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    original = layout.private_root
    moved = layout.job_root / "private-old"
    original.rename(moved)
    original.mkdir()
    (original / "staging").mkdir()
    _stage(layout, "faithful-rescue.mp4", b"attacker")
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))


def test_segment_names_and_manifest_are_deterministic_and_path_safe() -> None:
    assert deterministic_segment_name(0) == "faithful-segment-0001.mp4"
    assert deterministic_segment_name(11) == "faithful-segment-0012.mp4"
    mappings = (
        SourceMapping(4.0, 6.0, 2.0, 4.0, "faithful-rescue.mp4"),
        SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),
    )
    manifest = build_damaged_segments_manifest(
        mappings=mappings,
        damaged_ranges=((2.0, 4.0),),
    )
    assert manifest == {
        "damaged_ranges": [[2.0, 4.0]],
        "source_mappings": [
            {
                "source_start": 0.0,
                "source_end": 2.0,
                "output_start": 0.0,
                "output_end": 2.0,
                "output_relative_path": "faithful-rescue.mp4",
            },
            {
                "source_start": 4.0,
                "source_end": 6.0,
                "output_start": 2.0,
                "output_end": 4.0,
                "output_relative_path": "faithful-rescue.mp4",
            },
        ],
    }


def test_repeated_unicode_publication_is_deterministic_and_no_overwrite(
    tmp_path: Path,
) -> None:
    layout = RescueArtifactLayout.create(tmp_path / "中文 job")
    _stage(layout, "faithful-rescue.mp4", b"faithful")
    first = _publish(layout, _report(layout))
    assert first[0].sha256 == sha256_bytes(b"faithful")
    _stage(layout, "faithful-rescue.mp4", b"different")
    with pytest.raises(RescueArtifactError):
        _publish(layout, _report(layout))
    assert (layout.public_root / "faithful-rescue.mp4").read_bytes() == b"faithful"


def sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
