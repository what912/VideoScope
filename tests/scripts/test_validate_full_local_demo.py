"""Contract tests for the confirmation-gated full local demo driver."""

from __future__ import annotations

import importlib
import json
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


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        content_factory=lambda *_: None,
        privacy_factory=lambda *_: None,
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
    assert set(review.workflows) == {"publish_ready", "video_rescue"}
    assert _Publish.instances[0].execute_calls == 0
    assert _Rescue.instances[0].confirm_calls == _Rescue.instances[0].execute_calls == 0
    assert _Publish.instances[0].discard_calls == _Rescue.instances[0].abort_calls == 1
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
