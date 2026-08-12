"""Contract tests for the confirmation-gated full local demo driver."""

from __future__ import annotations

import importlib
import json
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _module() -> Any:
    """The production change that makes these tests fail is a missing driver."""
    return importlib.import_module("scripts.validate_full_local_demo")


def test_prepare_api_exists_before_any_confirmation_or_execution() -> None:
    module = _module()
    assert callable(module.prepare_all)


def test_workflow_models_forbid_unknown_public_fields() -> None:
    module = _module()
    with pytest.raises(ValueError):
        module.WorkflowCandidate(
            id="candidate", kind="remux", requires_confirmation=True, forged=True
        )


def test_public_artifact_paths_must_be_relative() -> None:
    module = _module()
    with pytest.raises(ValueError, match="relative"):
        module.WorkflowOutcome(
            workflow_id="publish_ready",
            status="completed",
            source_sha256_before="a" * 64,
            source_sha256_after="a" * 64,
            artifacts={"video": str(Path("C:/private/video.mp4"))},
        )


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation capability unavailable: {exc}")


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "linked_component", ["review-previews", "publish-ready"]
)
def test_preview_preservation_rejects_symlinked_parent_before_external_write(
    tmp_path: Path, linked_component: str
) -> None:
    module = _module()
    source = tmp_path / "private-preview.mp4"
    source.write_bytes(b"new-preview")
    output = tmp_path / "output"
    output.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"unchanged")
    if linked_component == "review-previews":
        link = output / "review-previews"
    else:
        (output / "review-previews").mkdir()
        link = output / "review-previews" / "publish-ready"
    _symlink_or_skip(link, outside, directory=True)

    with pytest.raises(module.DemoConfirmationError, match="preview"):
        module._copy_review_preview(
            source,
            output,
            "review-previews/publish-ready/publish-preview.mp4",
        )

    assert sentinel.read_bytes() == b"unchanged"
    assert not (outside / "publish-ready").exists()
    assert not (outside / "publish-preview.mp4").exists()


def test_preview_preservation_rejects_destination_symlink_without_overwrite(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "private-preview.mp4"
    source.write_bytes(b"new-preview")
    output = tmp_path / "output"
    destination = output / "review-previews" / "publish-ready" / "preview.mp4"
    destination.parent.mkdir(parents=True)
    sentinel = tmp_path / "outside-sentinel.mp4"
    sentinel.write_bytes(b"unchanged")
    _symlink_or_skip(destination, sentinel, directory=False)

    with pytest.raises(module.DemoConfirmationError, match="preview"):
        module._copy_review_preview(
            source,
            output,
            "review-previews/publish-ready/preview.mp4",
        )

    assert sentinel.read_bytes() == b"unchanged"
    assert destination.is_symlink()


def test_preview_preservation_atomically_overwrites_regular_destination(
    tmp_path: Path,
) -> None:
    module = _module()
    source = tmp_path / "private-preview.mp4"
    source.write_bytes(b"new-preview")
    output = tmp_path / "output"
    destination = output / "review-previews" / "publish-ready" / "preview.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old-preview")

    relative = module._copy_review_preview(
        source,
        output,
        "review-previews/publish-ready/preview.mp4",
    )

    assert relative == "review-previews/publish-ready/preview.mp4"
    assert destination.read_bytes() == b"new-preview"
    assert not list(destination.parent.glob(".preview-copy-*.tmp"))


def test_preview_copy_failure_preserves_existing_destination_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    source = tmp_path / "private-preview.mp4"
    source.write_bytes(b"new-preview")
    output = tmp_path / "output"
    destination = output / "review-previews" / "publish-ready" / "preview.mp4"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old-preview")

    def partial_copy(_source: Path, target: Path) -> None:
        Path(target).write_bytes(b"partial")
        raise OSError("injected copy interruption")

    monkeypatch.setattr(module.shutil, "copy2", partial_copy)
    with pytest.raises(module.DemoConfirmationError, match="preview"):
        module._copy_review_preview(
            source,
            output,
            "review-previews/publish-ready/preview.mp4",
        )

    assert destination.read_bytes() == b"old-preview"
    assert not list(destination.parent.glob(".preview-copy-*.tmp"))


class _Publish:
    instances: list[_Publish] = []
    status = "completed"
    verification = "passed"
    mutate = False

    def __init__(self, _config: object) -> None:
        self.config = _config
        self.source: Path | None = None
        self.private_preview: Path | None = None
        self.execute_calls = 0
        self.discard_calls = 0
        type(self).instances.append(self)

    def prepare(self, source: Path) -> object:
        self.source = source
        digest = _sha(source)
        workspace = source.parent / f"publish-private-{len(type(self).instances)}"
        self.private_preview = workspace / "preview" / "publish-preview.mp4"
        self.private_preview.parent.mkdir(parents=True, exist_ok=True)
        self.private_preview.write_bytes(b"publish-preview")
        return SimpleNamespace(
            preview_path=self.private_preview,
            plan=SimpleNamespace(
                input_hash=digest,
                plan_digest="c" * 64,
                preview_artifact="preview/publish-preview.mp4",
                actions=(
                    SimpleNamespace(
                        action_id="remux",
                        kind="remux",
                        affects=("video",),
                        confirmation_required=True,
                        description="Preserve streams.",
                    ),
                ),
            ),
        )

    def execute(self, _preparation: object, confirmed_plan_digest: str) -> object:
        assert confirmed_plan_digest == "c" * 64
        self.execute_calls += 1
        assert self.source is not None
        if type(self).mutate:
            self.source.write_bytes(b"changed")
        root = self.source.parent / "publish-ready"
        root.mkdir(exist_ok=True)
        paths = {
            name: root / filename
            for name, filename in {
                "video_path": "publish-ready.mp4",
                "cover_path": "cover.jpg",
                "technical_report_path": "technical-report.json",
            }.items()
        }
        for path in paths.values():
            path.write_bytes(b"artifact")
        return SimpleNamespace(
            status=type(self).status,
            output_directory=root,
            **paths,
            change_log=SimpleNamespace(actions=()),
            technical_report=SimpleNamespace(
                verification=SimpleNamespace(
                    status=type(self).verification, checks=(), manual_review_reasons=()
                ),
            ),
        )

    def discard(self, _preparation: object) -> None:
        output = Path(getattr(self.config, "output_directory")).parent
        copied = output / "review-previews" / "publish-ready" / "publish-preview.mp4"
        assert copied.read_bytes() == b"publish-preview"
        assert self.private_preview is not None
        self.private_preview.unlink()
        self.discard_calls += 1


class _Rescue:
    instances: list[_Rescue] = []
    status = "completed"
    include_improved = True
    include_faithful = True
    faithful_verification = "passed"
    changed_range = False

    def __init__(self, _config: object) -> None:
        self.config = _config
        self.source: Path | None = None
        self.private_paths: tuple[Path, ...] = ()
        self.confirm_calls = 0
        self.execute_calls = 0
        self.abort_calls = 0
        type(self).instances.append(self)

    def prepare(self, source: Path) -> object:
        self.source = source
        action_range = (
            (6.0, 10.0)
            if type(self).changed_range and len(type(self).instances) > 1
            else (5.0, 10.0)
        )
        private = source.parent / f"rescue-private-{len(type(self).instances)}"
        variants: dict[str, object] = {}
        paths: list[Path] = []
        for variant in ("source", "faithful", "improved"):
            variant_paths = tuple(
                private / variant / f"{variant}-{index:02d}.mp4" for index in range(2)
            )
            for path in variant_paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{variant}:{path.name}".encode())
            paths.extend(variant_paths)
            variants[variant] = SimpleNamespace(
                variant=variant,
                time_ranges=((5.0, 7.0), (8.0, 10.0)),
                paths=variant_paths,
            )
        self.private_paths = tuple(paths)
        return SimpleNamespace(
            source_hash=_sha(source),
            status="awaiting_confirmation",
            previews=SimpleNamespace(
                source=variants["source"],
                faithful=variants["faithful"],
                improved=variants["improved"],
            ),
            plan=SimpleNamespace(
                input_hash=_sha(source),
                plan_digest="d" * 64,
                assessment_limitations=("Measured evidence is required.",),
                damage_intervals=(),
                actions=(
                    SimpleNamespace(
                        id="luma",
                        kind="adjust_luma",
                        source_ranges=(action_range,),
                        requires_confirmation=True,
                        parameters={
                            "assessment_evidence": [
                                {"metric": "median_luma", "value": 0.1}
                            ],
                            "damage_ids": ["damage_demo"],
                            "measured_delta": 0.2,
                        },
                    ),
                ),
            ),
        )

    def confirm(self, _preparation: object, confirmation: object) -> object:
        self.confirm_calls += 1
        assert getattr(confirmation, "accepted_action_ids") == ("luma",)
        return _preparation

    def execute(self, _preparation: object, _confirmation: object) -> object:
        self.execute_calls += 1
        assert self.source is not None
        root = self.source.parent / "video-rescue"
        root.mkdir(exist_ok=True)
        faithful = root / "faithful-rescue.mp4" if type(self).include_faithful else None
        if faithful is not None:
            faithful.write_bytes(b"faithful")
        improved = (
            root / "improved-viewing.mp4" if type(self).include_improved else None
        )
        if improved is not None:
            improved.write_bytes(b"improved")
        report = root / "technical-report.json"
        report.write_bytes(b"report")
        return SimpleNamespace(
            status=type(self).status,
            public_root=root,
            faithful_path=faithful,
            improved_path=improved,
            report_path=report,
            technical_report=SimpleNamespace(
                verification=SimpleNamespace(
                    faithful_status=type(self).faithful_verification,
                    checks=(),
                ),
                limitations=(),
            ),
        )

    def abort(self, _preparation: object | None = None) -> None:
        output = Path(getattr(self.config, "output_directory")).parent
        copied = tuple(
            output
            / "review-previews"
            / "video-rescue"
            / variant
            / f"{variant}-{index:02d}.mp4"
            for variant in ("source", "faithful", "improved")
            for index in range(2)
        )
        assert all(path.is_file() for path in copied)
        for path in self.private_paths:
            path.unlink()
        self.abort_calls += 1


class _Content:
    instances: list[_Content] = []
    status = "completed"

    def __init__(self, config: object) -> None:
        self.config = config
        self.close_calls = 0
        type(self).instances.append(self)

    def prepare(self, source: Path) -> object:
        content = getattr(self.config, "content")
        assert getattr(content, "goal").value == "selected_clips"
        assert getattr(content, "export_clips") is True
        assert getattr(content, "minimum_chapter_duration_seconds") == 1.0
        return SimpleNamespace(
            content_map=SimpleNamespace(
                input_hash=_sha(source),
                user_ranges=getattr(self.config, "user_ranges"),
            ),
            actions=(),
        )

    def preview(self, preparation: object) -> object:
        return SimpleNamespace(
            preparation=preparation,
            previews=(),
            plan=SimpleNamespace(
                input_hash=getattr(preparation, "content_map").input_hash,
                plan_digest="e" * 64,
                actions=(),
            ),
        )

    def close(self) -> None:
        self.close_calls += 1

    def confirm(
        self, review: object, *, accepted_action_ids: tuple[str, ...]
    ) -> object:
        assert accepted_action_ids == ()
        return SimpleNamespace(review=review)

    def execute(self, review: object, confirmation: object) -> object:
        assert getattr(confirmation, "review") is review
        return SimpleNamespace(
            status=type(self).status,
            public_root=None,
            artifacts=(),
            verification=SimpleNamespace(outcome=type(self).status, checks=()),
            technical_report=SimpleNamespace(source_mappings=(), limitations=()),
        )


class _Privacy:
    instances: list[_Privacy] = []
    profile = "public"
    changed_scan = False
    changed_plan = False
    status = "completed"

    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory
        self.discard_calls = 0
        type(self).instances.append(self)

    def scan(self, *, source: Path, config: object) -> object:
        assert getattr(config, "audience") == "public"
        assert getattr(config, "sample_fps") == 5.0
        return SimpleNamespace(
            scan_id="1" * 32,
            risk_map=SimpleNamespace(
                input_hash=_sha(source),
                profile=type(self).profile,
                duration_seconds=42.0,
                risks=(),
                model_dump=lambda **_: {
                    "input_hash": _sha(source),
                    "profile": type(self).profile,
                    "duration_seconds": 42.0,
                    "risks": [],
                    "nonce": (
                        1
                        if type(self).changed_scan and len(type(self).instances) > 1
                        else 0
                    ),
                },
            ),
            scanner_executions=(),
            warnings=(),
        )

    def review(
        self,
        scan_id: str,
        reviews: Sequence[object],
        *,
        manual_visual_regions: Sequence[object],
        manual_audio_intervals: Sequence[object],
    ) -> object:
        assert scan_id == "1" * 32
        assert len(reviews) == 2
        assert len(manual_visual_regions) == len(manual_audio_intervals) == 1
        return SimpleNamespace(review_id="2" * 32)

    def prepare(self, review_id: str) -> object:
        assert review_id == "2" * 32
        return SimpleNamespace(
            preparation_id="3" * 32,
            plan=SimpleNamespace(
                digest=(
                    "0" * 64
                    if type(self).changed_plan and len(type(self).instances) > 1
                    else "f" * 64
                ),
                actions=(
                    SimpleNamespace(
                        id="solid-fill",
                        kind="visual_redaction",
                        start_seconds=25.0,
                        end_seconds=32.0,
                        box=SimpleNamespace(
                            model_dump=lambda **_: {
                                "x_min": 0.58,
                                "y_min": 0.18,
                                "x_max": 0.94,
                                "y_max": 0.78,
                            }
                        ),
                        parameters={},
                        requires_confirmation=True,
                    ),
                    SimpleNamespace(
                        id="mute",
                        kind="audio_mute",
                        start_seconds=25.0,
                        end_seconds=32.0,
                        box=None,
                        parameters={},
                        requires_confirmation=True,
                    ),
                ),
            ),
        )

    def preview(self, preparation_id: str) -> Path:
        assert preparation_id == "3" * 32
        preview = self.output_directory / "privacy-review-private" / "preview.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"privacy-preview")
        return preview

    def confirm(self, preparation_id: str, plan_digest: str) -> object:
        assert preparation_id == "3" * 32
        assert plan_digest in {"f" * 64, "0" * 64}
        package = self.output_directory / "share-package"
        package.mkdir(parents=True, exist_ok=True)
        for name in ("share-safe.mp4", "verification.json", "technical-report.json"):
            (package / name).write_bytes(b"artifact")
        return SimpleNamespace(
            status=type(self).status,
            video_relative_path="share-package/share-safe.mp4",
            verification_relative_path="share-package/verification.json",
            technical_report_relative_path="share-package/technical-report.json",
            verification=SimpleNamespace(
                status=type(self).status, checks=(), limitations=()
            ),
        )

    def discard(self, lifecycle_id: str) -> None:
        assert lifecycle_id == "1" * 32
        self.discard_calls += 1


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_useful_content_uses_only_approved_keep_ranges(
    prepared_inputs: tuple[Path, Path, Path, Any],
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    dependencies = module.DemoPipelineDependencies(
        publish_factory=dependencies.publish_factory,
        rescue_factory=dependencies.rescue_factory,
        content_factory=_Content,
        privacy_factory=_Privacy,
    )
    prepared = module.prepare_useful_content(source, output, dependencies=dependencies)
    assert [item.ranges[0] for item in prepared.candidates if item.kind == "keep"] == [
        (0.0, 5.0),
        (10.0, 20.0),
        (36.0, 42.0),
    ]
    assert _Content.instances[-1].close_calls == 1


def test_safe_sharing_uses_exact_manual_visual_and_audio_selections(
    prepared_inputs: tuple[Path, Path, Path, Any],
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    dependencies = module.DemoPipelineDependencies(
        publish_factory=dependencies.publish_factory,
        rescue_factory=dependencies.rescue_factory,
        content_factory=_Content,
        privacy_factory=_Privacy,
    )
    scanned = module.scan_safe_sharing(source, output, dependencies=dependencies)
    assert scanned.manual_visual_regions[0].box.model_dump() == {
        "x_min": 0.58,
        "y_min": 0.18,
        "x_max": 0.94,
        "y_max": 0.78,
    }
    assert (
        scanned.manual_audio_intervals[0].start_seconds,
        scanned.manual_audio_intervals[0].end_seconds,
    ) == (25.0, 32.0)
    assert _Privacy.instances[-1].discard_calls == 1


def _privacy_prepared(
    module: Any, source: Path, output: Path, dependencies: Any
) -> Any:
    scanned = module.scan_safe_sharing(source, output, dependencies=dependencies)
    return module._privacy_scan_workflow(scanned)


def _privacy_review_file(module: Any, prepared: Any, source: Path) -> Any:
    choices = []
    for candidate in prepared.candidates:
        style = "solid_fill" if candidate.kind == "manual_visual" else "mute"
        choices.append(
            module.PrivacyReviewChoice(
                risk_id=candidate.id,
                decision="redact",
                style=style,
            )
        )
    return module.PrivacyReviewFile(
        source_sha256=_sha(source),
        contract_sha256="b" * 64,
        scan_digest=prepared.plan_digest,
        reviewed_at="2026-08-12T00:00:00+00:00",
        choices=tuple(choices),
    )


@pytest.mark.parametrize("mutation", ["missing", "unknown", "digest"])  # type: ignore[untyped-decorator]
def test_privacy_review_rejects_missing_unknown_and_changed_scan(
    prepared_inputs: tuple[Path, Path, Path, object], mutation: str
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    prepared = _privacy_prepared(module, source, output, dependencies)
    review = _privacy_review_file(module, prepared, source)
    if mutation == "missing":
        review = review.model_copy(update={"choices": review.choices[:-1]})
    elif mutation == "unknown":
        review = review.model_copy(
            update={
                "choices": (
                    *review.choices[:-1],
                    review.choices[-1].model_copy(
                        update={"risk_id": "privacy_risk_" + "0" * 64}
                    ),
                )
            }
        )
    else:
        review = review.model_copy(update={"scan_digest": "0" * 64})
    with pytest.raises(module.DemoConfirmationError):
        module._validate_privacy_review(
            prepared,
            review,
            source_hash=_sha(source),
            contract_hash="b" * 64,
        )


def test_privacy_review_model_rejects_duplicate_risk_decisions(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    prepared = _privacy_prepared(module, source, output, dependencies)
    review = _privacy_review_file(module, prepared, source)
    with pytest.raises(ValueError, match="unique"):
        module.PrivacyReviewFile(
            source_sha256=review.source_sha256,
            contract_sha256=review.contract_sha256,
            scan_digest=review.scan_digest,
            reviewed_at=review.reviewed_at,
            choices=(review.choices[0], review.choices[0]),
        )


def test_preview_rejects_changed_scan_and_discards_lifecycle(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    prepared = _privacy_prepared(module, source, output, dependencies)
    review = _privacy_review_file(module, prepared, source)
    _Privacy.changed_scan = True
    with pytest.raises(module.DemoConfirmationError, match="digest"):
        module.preview_safe_sharing(
            prepared,
            review,
            source=source,
            output=output,
            source_hash=_sha(source),
            contract_hash="b" * 64,
            dependencies=dependencies,
        )
    assert _Privacy.instances[-1].discard_calls == 1


def test_safe_sharing_rejects_non_public_audience(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    _Privacy.profile = "family"
    with pytest.raises(module.DemoConfirmationError, match="public"):
        module.scan_safe_sharing(source, output, dependencies=dependencies)
    assert _Privacy.instances[-1].discard_calls == 1


def test_candidate_rejects_private_path_leakage() -> None:
    module = _module()
    with pytest.raises(ValueError, match="absolute path"):
        module.WorkflowCandidate(
            id="candidate",
            kind="keep",
            requires_confirmation=True,
            evidence=({"private_path": str(Path.cwd().resolve() / "secret.jpg")},),
        )


@pytest.mark.parametrize("field", ["actions", "checks", "limitations"])  # type: ignore[untyped-decorator]
def test_outcome_rejects_private_path_leakage(field: str) -> None:
    module = _module()
    leaked = str(Path.cwd().resolve() / "secret.jpg")
    values: dict[str, object] = {
        "workflow_id": "safe_sharing",
        "status": "needs_review",
        "source_sha256_before": "a" * 64,
        "source_sha256_after": "a" * 64,
    }
    values[field] = (leaked,) if field == "limitations" else ({"path": leaked},)
    with pytest.raises(ValueError, match="absolute path"):
        module.WorkflowOutcome(**values)


def test_c_d_confirmation_validates_all_workflows_before_filtering() -> None:
    module = _module()
    prepared = {
        workflow_id: module.PreparedWorkflow(
            workflow_id=workflow_id,
            plan_digest=digest * 64,
            candidates=(),
            preparation_status="ready_to_confirm",
        )
        for workflow_id, digest in {
            "publish_ready": "a",
            "video_rescue": "b",
            "useful_content": "c",
            "safe_sharing": "d",
        }.items()
    }
    confirmable = module.ConfirmablePlan(
        source_sha256="e" * 64,
        contract_sha256="f" * 64,
        workflows=prepared,
    )
    confirmation = module.ExecutionConfirmation(
        source_sha256="e" * 64,
        contract_sha256="f" * 64,
        workflows={
            key: module.WorkflowConfirmation(
                workflow_id=key,
                plan_digest=("0" * 64 if key == "publish_ready" else value.plan_digest),
            )
            for key, value in prepared.items()
        },
    )
    with pytest.raises(module.DemoConfirmationError, match="digest"):
        module._validate_confirmation_document(confirmable, confirmation)


def test_default_content_factory_injects_explicit_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    captured: dict[str, object] = {}

    class Preview:
        def __init__(self, *, ffmpeg_executable: str) -> None:
            captured["preview"] = ffmpeg_executable

    class Executor:
        def __init__(self, *, ffmpeg: str) -> None:
            captured["executor"] = ffmpeg

    class Pipeline:
        def __init__(self, _config: object, *, dependencies: object) -> None:
            captured["dependencies"] = dependencies

    monkeypatch.setattr(module, "ContentPreviewBuilder", Preview)
    monkeypatch.setattr(module, "NativeContentExecutor", Executor)
    monkeypatch.setattr(module, "LongVideoContentPipeline", Pipeline)
    config = module._content_pipeline_config("a" * 64, Path("out"))
    config = config.model_copy(
        update={
            "features": config.features.model_copy(update={"ffmpeg": "local-ffmpeg"})
        }
    )
    module._default_content_factory(config)
    assert captured["preview"] == captured["executor"] == "local-ffmpeg"


def test_recursive_public_json_normalization_handles_real_privacy_parameters() -> None:
    module = _module()

    class Style(StrEnum):
        SOLID = "solid_fill"

    normalized = module._public_json_value(
        {
            "dict": {
                "categories": ("title", "author"),
                "styles": {Style.SOLID},
                "flags": (True, None, 2),
            }
        }
    )
    assert normalized == {
        "dict": {
            "categories": ["title", "author"],
            "styles": ["solid_fill"],
            "flags": [True, None, 2],
        }
    }


def test_recursive_public_json_normalization_rejects_paths_and_absolute_values(
    tmp_path: Path,
) -> None:
    module = _module()
    with pytest.raises(ValueError, match="path"):
        module._public_json_value({"nested": {"path": tmp_path / "private.jpg"}})
    with pytest.raises(ValueError, match="absolute path"):
        module._public_json_value({"nested": str(tmp_path / "private.jpg")})


def test_exact_range_and_box_changes_do_not_match_preparation() -> None:
    module = _module()
    expected = module.PreparedWorkflow(
        workflow_id="useful_content",
        plan_digest="e" * 64,
        candidates=(
            module.WorkflowCandidate(
                id="keep",
                kind="keep",
                ranges=((0.0, 5.0),),
                requires_confirmation=False,
            ),
        ),
        preparation_status="ready_to_confirm",
    )
    shifted = expected.model_copy(
        update={
            "candidates": (
                expected.candidates[0].model_copy(update={"ranges": ((1 / 24, 5.0),)}),
            )
        }
    )
    with pytest.raises(module.DemoConfirmationError, match="candidate"):
        module._same_preparation(expected, shifted)

    privacy = module.PreparedWorkflow(
        workflow_id="safe_sharing",
        plan_digest="f" * 64,
        candidates=(
            module.WorkflowCandidate(
                id="redact",
                kind="visual_redaction",
                ranges=((25.0, 32.0),),
                requires_confirmation=True,
                evidence=(
                    {
                        "box": {
                            "x_min": 0.58,
                            "y_min": 0.18,
                            "x_max": 0.94,
                            "y_max": 0.78,
                        }
                    },
                ),
            ),
        ),
        preparation_status="ready_to_confirm",
    )
    expanded = privacy.model_copy(
        update={
            "candidates": (
                privacy.candidates[0].model_copy(
                    update={
                        "evidence": (
                            {
                                "box": {
                                    "x_min": 0.57,
                                    "y_min": 0.18,
                                    "x_max": 0.94,
                                    "y_max": 0.78,
                                }
                            },
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(module.DemoConfirmationError, match="candidate"):
        module._same_preparation(privacy, expanded)


def test_execute_safe_sharing_rejects_changed_plan_and_discards(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    prepared = _privacy_prepared(module, source, output, dependencies)
    review = _privacy_review_file(module, prepared, source)
    confirmable = module.preview_safe_sharing(
        prepared,
        review,
        source=source,
        output=output,
        source_hash=_sha(source),
        contract_hash="b" * 64,
        dependencies=dependencies,
    )
    _Privacy.changed_plan = True
    with pytest.raises(module.DemoConfirmationError, match="digest"):
        module.execute_safe_sharing(
            prepared,
            confirmable,
            review,
            module.WorkflowConfirmation(
                workflow_id="safe_sharing",
                plan_digest="f" * 64,
                accepted_action_ids=("solid-fill", "mute"),
            ),
            source=source,
            output=output,
            source_hash=_sha(source),
            contract_hash="b" * 64,
            dependencies=dependencies,
        )
    assert _Privacy.instances[-1].discard_calls == 1


@pytest.mark.parametrize("status", ["needs_review", "partial", "failed"])  # type: ignore[untyped-decorator]
def test_safe_sharing_outcome_is_truthful_and_always_requires_final_review(
    prepared_inputs: tuple[Path, Path, Path, object], status: str
) -> None:
    module = _module()
    source, _manifest, output, dependencies = prepared_inputs
    prepared = _privacy_prepared(module, source, output, dependencies)
    review = _privacy_review_file(module, prepared, source)
    confirmable = module.preview_safe_sharing(
        prepared,
        review,
        source=source,
        output=output,
        source_hash=_sha(source),
        contract_hash="b" * 64,
        dependencies=dependencies,
    )
    _Privacy.status = status
    outcome = module.execute_safe_sharing(
        prepared,
        confirmable,
        review,
        module.WorkflowConfirmation(
            workflow_id="safe_sharing",
            plan_digest="f" * 64,
            accepted_action_ids=("solid-fill", "mute"),
        ),
        source=source,
        output=output,
        source_hash=_sha(source),
        contract_hash="b" * 64,
        dependencies=dependencies,
    )
    assert outcome.status == status
    assert outcome.final_human_review_required is True


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def _reset_fakes() -> None:
    _Publish.instances = []
    _Publish.status = "completed"
    _Publish.verification = "passed"
    _Publish.mutate = False
    _Rescue.instances = []
    _Rescue.status = "completed"
    _Rescue.include_improved = True
    _Rescue.include_faithful = True
    _Rescue.faithful_verification = "passed"
    _Rescue.changed_range = False
    _Content.instances = []
    _Content.status = "completed"
    _Privacy.instances = []
    _Privacy.profile = "public"
    _Privacy.changed_scan = False
    _Privacy.changed_plan = False
    _Privacy.status = "completed"


@pytest.fixture  # type: ignore[untyped-decorator]
def prepared_inputs(tmp_path: Path) -> tuple[Path, Path, Path, object]:
    module = _module()
    source = tmp_path / "中文 source.mp4"
    source.write_bytes(b"source")
    manifest = tmp_path / "demo-manifest.json"
    manifest.write_text(
        json.dumps(
            {"source": {"sha256": _sha(source)}, "contract": {"sha256": "b" * 64}}
        ),
        encoding="utf-8",
    )
    dependencies = module.DemoPipelineDependencies(
        publish_factory=_Publish,
        rescue_factory=_Rescue,
        content_factory=_Content,
        privacy_factory=_Privacy,
    )
    return source, manifest, tmp_path / "out", dependencies


def test_prepare_releases_private_state_without_confirmation_or_execution(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, manifest, output, dependencies = prepared_inputs
    review = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    )
    assert set(review.workflows) == {
        "publish_ready",
        "video_rescue",
        "useful_content",
        "safe_sharing",
    }
    assert _Publish.instances[0].execute_calls == 0
    assert _Rescue.instances[0].confirm_calls == _Rescue.instances[0].execute_calls == 0
    assert _Publish.instances[0].discard_calls == _Rescue.instances[0].abort_calls == 1
    assert _Content.instances[0].close_calls == _Privacy.instances[0].discard_calls == 1
    assert not (output / "confirmation.json").exists()
    assert "C:" not in (output / "prepared-review.json").read_text(encoding="utf-8")
    publish_candidate = review.workflows["publish_ready"].candidates[0]
    assert publish_candidate.preview_relative_path == (
        "review-previews/publish-ready/publish-preview.mp4"
    )
    assert (output / publish_candidate.preview_relative_path).is_file()
    rescue_candidate = review.workflows["video_rescue"].candidates[0]
    preview_evidence = tuple(
        item for item in rescue_candidate.evidence if item.get("source") == "preview"
    )
    assert tuple(item["relative_path"] for item in preview_evidence) == (
        "review-previews/video-rescue/source/source-00.mp4",
        "review-previews/video-rescue/source/source-01.mp4",
        "review-previews/video-rescue/faithful/faithful-00.mp4",
        "review-previews/video-rescue/faithful/faithful-01.mp4",
        "review-previews/video-rescue/improved/improved-00.mp4",
        "review-previews/video-rescue/improved/improved-01.mp4",
    )
    assert all(
        (output / str(item["relative_path"])).is_file() for item in preview_evidence
    )
    assessment_evidence = tuple(
        item
        for item in rescue_candidate.evidence
        if item.get("source") == "assessment_evidence"
    )
    assert assessment_evidence == (
        {
            "source": "assessment_evidence",
            "metric": "median_luma",
            "value": 0.1,
        },
    )
    assert any(
        item.get("name") == "damage_ids" and item.get("value") == ["damage_demo"]
        for item in rescue_candidate.evidence
    )


def test_execute_rejects_forged_digest_and_reprepared_range_change(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, manifest, output, dependencies = prepared_inputs
    prepared = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    ).workflows["video_rescue"]
    with pytest.raises(module.DemoConfirmationError, match="digest"):
        module.execute_rescue(
            prepared,
            module.WorkflowConfirmation(
                workflow_id="video_rescue",
                plan_digest="0" * 64,
                accepted_action_ids=("luma",),
            ),
            source=source,
            output=output,
            dependencies=dependencies,
        )
    _Rescue.changed_range = True
    with pytest.raises(module.DemoConfirmationError, match="candidate"):
        module.execute_rescue(
            prepared,
            module.WorkflowConfirmation(
                workflow_id="video_rescue",
                plan_digest="d" * 64,
                accepted_action_ids=("luma",),
            ),
            source=source,
            output=output,
            dependencies=dependencies,
        )
    assert _Rescue.instances[-1].abort_calls == 1


def test_execute_rejects_forged_action_id_before_fresh_pipeline_creation(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, manifest, output, dependencies = prepared_inputs
    prepared = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    ).workflows["video_rescue"]
    before_instances = len(_Rescue.instances)
    with pytest.raises(module.DemoConfirmationError, match="action"):
        module.execute_rescue(
            prepared,
            module.WorkflowConfirmation(
                workflow_id="video_rescue",
                plan_digest="d" * 64,
                accepted_action_ids=("forged-action",),
            ),
            source=source,
            output=output,
            dependencies=dependencies,
        )
    assert len(_Rescue.instances) == before_instances


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("pipeline", "verification", "expected"),
    [
        ("completed", "passed", "completed"),
        ("completed", "needs_review", "needs_review"),
        ("needs_review", "passed", "needs_review"),
        ("failed", "failed", "failed"),
    ],
)
def test_publish_does_not_promote_unsuccessful_outcomes(
    prepared_inputs: tuple[Path, Path, Path, object],
    pipeline: str,
    verification: str,
    expected: str,
) -> None:
    module = _module()
    _Publish.status, _Publish.verification = pipeline, verification
    source, manifest, output, dependencies = prepared_inputs
    prepared = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    ).workflows["publish_ready"]
    outcome = module.execute_publish_ready(
        prepared,
        module.WorkflowConfirmation(workflow_id="publish_ready", plan_digest="c" * 64),
        source=source,
        output=output,
        dependencies=dependencies,
    )
    assert outcome.status == expected
    assert outcome.source_sha256_before == outcome.source_sha256_after == _sha(source)


def test_publish_rejects_source_mutation_and_discards_preparation(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    _Publish.mutate = True
    source, manifest, output, dependencies = prepared_inputs
    prepared = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    ).workflows["publish_ready"]
    with pytest.raises(module.DemoConfirmationError, match="source"):
        module.execute_publish_ready(
            prepared,
            module.WorkflowConfirmation(
                workflow_id="publish_ready", plan_digest="c" * 64
            ),
            source=source,
            output=output,
            dependencies=dependencies,
        )
    assert _Publish.instances[-1].discard_calls == 1


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "status", ["completed", "partial", "needs_review", "failed"]
)
def test_rescue_statuses_remain_truthful_and_missing_improvement_is_not_advertised(
    prepared_inputs: tuple[Path, Path, Path, object],
    status: str,
) -> None:
    module = _module()
    _Rescue.status = status
    _Rescue.include_improved = False
    source, manifest, output, dependencies = prepared_inputs
    prepared = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    ).workflows["video_rescue"]
    outcome = module.execute_rescue(
        prepared,
        module.WorkflowConfirmation(
            workflow_id="video_rescue",
            plan_digest="d" * 64,
            accepted_action_ids=("luma",),
        ),
        source=source,
        output=output,
        dependencies=dependencies,
    )
    assert outcome.status == ("partial" if status == "completed" else status)
    assert "improved" not in outcome.artifacts


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("include_faithful", "faithful_verification", "expected"),
    [
        (False, "passed", "failed"),
        (True, "needs_review", "needs_review"),
        (True, "failed", "failed"),
    ],
)
def test_completed_rescue_requires_faithful_artifact_and_passed_verification(
    prepared_inputs: tuple[Path, Path, Path, object],
    include_faithful: bool,
    faithful_verification: str,
    expected: str,
) -> None:
    module = _module()
    _Rescue.include_faithful = include_faithful
    _Rescue.faithful_verification = faithful_verification
    source, manifest, output, dependencies = prepared_inputs
    prepared = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    ).workflows["video_rescue"]
    outcome = module.execute_rescue(
        prepared,
        module.WorkflowConfirmation(
            workflow_id="video_rescue",
            plan_digest="d" * 64,
            accepted_action_ids=("luma",),
        ),
        source=source,
        output=output,
        dependencies=dependencies,
    )
    assert outcome.status == expected
    assert ("faithful" in outcome.artifacts) is include_faithful


def _write_confirmation(module: Any, root: Path, review: object) -> Path:
    confirmation = module.ExecutionConfirmation(
        source_sha256=getattr(review, "source_sha256"),
        contract_sha256=getattr(review, "contract_sha256"),
        workflows={
            "publish_ready": module.WorkflowConfirmation(
                workflow_id="publish_ready", plan_digest="c" * 64
            ),
            "video_rescue": module.WorkflowConfirmation(
                workflow_id="video_rescue",
                plan_digest="d" * 64,
                accepted_action_ids=("luma",),
            ),
        },
    )
    path = root / "user-confirmation.json"
    path.write_text(confirmation.model_dump_json(), encoding="utf-8")
    return path


def test_execute_cli_infers_canonical_source_manifest_and_output_root(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, manifest, output, dependencies = prepared_inputs
    review = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    )
    canonical_source = output / "VideoScope-Full-Local-Demo-Source.mp4"
    canonical_source.write_bytes(source.read_bytes())
    (output / "demo-manifest.json").write_bytes(manifest.read_bytes())
    confirmation = _write_confirmation(module, output, review)

    result = module.main(
        [
            "execute",
            "--prepared",
            str(output / "prepared-review.json"),
            "--confirmation",
            str(confirmation),
            "--only",
            "publish-ready",
            "--only",
            "video-rescue",
        ],
        dependencies=dependencies,
    )

    assert result == 0
    outcomes = json.loads((output / "execution-outcomes.json").read_text("utf-8"))
    assert set(outcomes["outcomes"]) == {"publish_ready", "video_rescue"}


def _outcome(module: Any, workflow_id: str, source_hash: str) -> object:
    return module.WorkflowOutcome(
        workflow_id=workflow_id,
        status="completed",
        source_sha256_before=source_hash,
        source_sha256_after=source_hash,
    )


def test_selected_execution_preserves_bound_unselected_outcomes_and_replaces_selected(
    tmp_path: Path,
) -> None:
    module = _module()
    source_hash = "a" * 64
    contract_hash = "b" * 64
    path = tmp_path / "execution-outcomes.json"
    existing = module._OutcomeDocument(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        outcomes={
            "publish_ready": _outcome(module, "publish_ready", source_hash),
            "video_rescue": _outcome(module, "video_rescue", source_hash),
            "useful_content": module.WorkflowOutcome(
                workflow_id="useful_content",
                status="failed",
                source_sha256_before=source_hash,
                source_sha256_after=source_hash,
            ),
        },
    )
    path.write_text(existing.model_dump_json(), encoding="utf-8")
    replacement = _outcome(module, "useful_content", source_hash)

    merged = module._merge_execution_outcomes(
        path,
        {"useful_content": replacement},
        source_hash=source_hash,
        contract_hash=contract_hash,
    )

    assert tuple(merged.outcomes) == (
        "publish_ready",
        "video_rescue",
        "useful_content",
    )
    assert merged.outcomes["publish_ready"] == existing.outcomes["publish_ready"]
    assert merged.outcomes["video_rescue"] == existing.outcomes["video_rescue"]
    assert merged.outcomes["useful_content"] == replacement


@pytest.mark.parametrize("binding", ["source", "contract"])  # type: ignore[untyped-decorator]
def test_selected_execution_rejects_conflicting_existing_outcome_binding(
    tmp_path: Path,
    binding: str,
) -> None:
    module = _module()
    source_hash = "a" * 64
    contract_hash = "b" * 64
    document = {
        "schema_version": "1",
        "source_sha256": source_hash if binding != "source" else "c" * 64,
        "contract_sha256": contract_hash if binding != "contract" else "d" * 64,
        "outcomes": {
            "publish_ready": module.WorkflowOutcome(
                workflow_id="publish_ready",
                status="completed",
                source_sha256_before=(source_hash if binding != "source" else "c" * 64),
                source_sha256_after=source_hash if binding != "source" else "c" * 64,
            ).model_dump(mode="json")
        },
    }
    path = tmp_path / "execution-outcomes.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(module.DemoConfirmationError, match="binding"):
        module._merge_execution_outcomes(
            path,
            {"useful_content": _outcome(module, "useful_content", source_hash)},
            source_hash=source_hash,
            contract_hash=contract_hash,
        )


def test_selected_execution_rejects_unknown_existing_workflow(
    tmp_path: Path,
) -> None:
    module = _module()
    source_hash = "a" * 64
    contract_hash = "b" * 64
    path = tmp_path / "execution-outcomes.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "source_sha256": source_hash,
                "contract_sha256": contract_hash,
                "outcomes": {
                    "arbitrary_json": {
                        "workflow_id": "arbitrary_json",
                        "status": "completed",
                        "source_sha256_before": source_hash,
                        "source_sha256_after": source_hash,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.DemoConfirmationError, match="workflow"):
        module._merge_execution_outcomes(
            path,
            {"useful_content": _outcome(module, "useful_content", source_hash)},
            source_hash=source_hash,
            contract_hash=contract_hash,
        )


def test_execute_cli_selected_c_d_merges_bound_existing_a_b(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    output = tmp_path / "out"
    output.mkdir()
    source = output / "VideoScope-Full-Local-Demo-Source.mp4"
    source.write_bytes(b"source")
    source_hash = _sha(source)
    contract_hash = "b" * 64
    (output / "demo-manifest.json").write_text(
        json.dumps(
            {
                "source": {"sha256": source_hash},
                "contract": {"sha256": contract_hash},
            }
        ),
        encoding="utf-8",
    )
    workflows = {
        workflow_id: module.PreparedWorkflow(
            workflow_id=workflow_id,
            plan_digest=digest * 64,
            candidates=(),
            preparation_status="ready_to_confirm",
        )
        for workflow_id, digest in zip(module._WORKFLOWS, "cdef", strict=True)
    }
    review = module.PreparedReview(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        workflows=workflows,
    )
    confirmation = module.ExecutionConfirmation(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        workflows={
            key: module.WorkflowConfirmation(
                workflow_id=key,
                plan_digest=value.plan_digest,
            )
            for key, value in workflows.items()
        },
    )
    confirmable = module.ConfirmablePlan(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        workflows=workflows,
    )
    prepared_path = output / "prepared-review.json"
    confirmation_path = output / "confirmation.json"
    confirmable_path = output / "confirmable-plan.json"
    prepared_path.write_text(review.model_dump_json(), encoding="utf-8")
    confirmation_path.write_text(confirmation.model_dump_json(), encoding="utf-8")
    confirmable_path.write_text(confirmable.model_dump_json(), encoding="utf-8")
    old_c = module.WorkflowOutcome(
        workflow_id="useful_content",
        status="failed",
        source_sha256_before=source_hash,
        source_sha256_after=source_hash,
    )
    existing = module._OutcomeDocument(
        source_sha256=source_hash,
        contract_sha256=contract_hash,
        outcomes={
            "publish_ready": _outcome(module, "publish_ready", source_hash),
            "video_rescue": _outcome(module, "video_rescue", source_hash),
            "useful_content": old_c,
        },
    )
    (output / "execution-outcomes.json").write_text(
        existing.model_dump_json(), encoding="utf-8"
    )
    new_c = _outcome(module, "useful_content", source_hash)
    new_d = _outcome(module, "safe_sharing", source_hash)
    monkeypatch.setattr(
        module,
        "execute_from_confirmation",
        lambda *_args, **_kwargs: {
            "useful_content": new_c,
            "safe_sharing": new_d,
        },
    )

    result = module.main(
        [
            "execute",
            "--prepared",
            str(prepared_path),
            "--confirmable-plan",
            str(confirmable_path),
            "--confirmation",
            str(confirmation_path),
            "--only",
            "useful-content",
            "--only",
            "safe-sharing",
        ]
    )

    assert result == 0
    merged = module._OutcomeDocument.model_validate_json(
        (output / "execution-outcomes.json").read_text(encoding="utf-8")
    )
    assert merged.outcomes == {
        "publish_ready": existing.outcomes["publish_ready"],
        "video_rescue": existing.outcomes["video_rescue"],
        "useful_content": new_c,
        "safe_sharing": new_d,
    }

    (output / "execution-outcomes.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "source_sha256": source_hash,
                "contract_sha256": contract_hash,
                "outcomes": {
                    "arbitrary_json": {
                        "workflow_id": "arbitrary_json",
                        "status": "completed",
                        "source_sha256_before": source_hash,
                        "source_sha256_after": source_hash,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    execute_called = False

    def unexpected_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal execute_called
        execute_called = True
        return {}

    monkeypatch.setattr(module, "execute_from_confirmation", unexpected_execute)
    with pytest.raises(module.DemoConfirmationError, match="workflow"):
        module.main(
            [
                "execute",
                "--prepared",
                str(prepared_path),
                "--confirmable-plan",
                str(confirmable_path),
                "--confirmation",
                str(confirmation_path),
                "--only",
                "useful-content",
                "--only",
                "safe-sharing",
            ]
        )
    assert execute_called is False


def test_execute_cli_rejects_manifest_binding_before_pipeline_creation(
    prepared_inputs: tuple[Path, Path, Path, object],
) -> None:
    module = _module()
    source, manifest, output, dependencies = prepared_inputs
    review = module.prepare_all(
        source, output, manifest_path=manifest, dependencies=dependencies
    )
    (output / "VideoScope-Full-Local-Demo-Source.mp4").write_bytes(source.read_bytes())
    bad_manifest = {
        "source": {"sha256": "0" * 64},
        "contract": {"sha256": "b" * 64},
    }
    (output / "demo-manifest.json").write_text(json.dumps(bad_manifest), "utf-8")
    confirmation = _write_confirmation(module, output, review)
    publish_count = len(_Publish.instances)
    rescue_count = len(_Rescue.instances)

    with pytest.raises(module.DemoConfirmationError, match="manifest"):
        module.main(
            [
                "execute",
                "--prepared",
                str(output / "prepared-review.json"),
                "--confirmation",
                str(confirmation),
            ],
            dependencies=dependencies,
        )

    assert len(_Publish.instances) == publish_count
    assert len(_Rescue.instances) == rescue_count
