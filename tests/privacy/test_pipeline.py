"""End-to-end lifecycle tests for the Safe Sharing orchestrator."""

from __future__ import annotations

import hmac
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

from videoscope.ai.runtime import ModelRuntimeManager
from videoscope.privacy.errors import (
    PrivacyArtifactError,
    PrivacyConfirmationError,
    PrivacyInputError,
)
from videoscope.privacy.executor import PrivacyNativeResult
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
)
from videoscope.privacy.metadata import PrivateProbeSummary
from videoscope.privacy.models import (
    PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS,
    NormalizedBox,
    PrivacyArtifact,
    PrivacyChangeLog,
    PrivacyDecision,
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyReviewDecision,
    PrivacyRisk,
    PrivacyVerificationCheck,
    PrivacyVerificationReport,
    RedactionStyle,
    VerificationStatus,
)
from videoscope.privacy.pipeline import SafeSharingConfig, SafeSharingPipeline
from videoscope.privacy.scanners import (
    PrivacyScanContext,
    PrivacyScannerExecution,
    PrivacyScannerRunResult,
    PrivacyScannerStatus,
)
from videoscope.privacy.text import SuspiciousTextScanner
from videoscope.privacy.verification import PrivacyVerificationContext
from videoscope.video import FrameSamplingResult


class EmptyScannerRunner:
    def run(self, context: object, configurations: object) -> PrivacyScannerRunResult:
        del context, configurations
        return PrivacyScannerRunResult()


class FailingScannerRunner:
    def run(self, context: object, configurations: object) -> PrivacyScannerRunResult:
        del context, configurations
        return PrivacyScannerRunResult(
            executions=(
                PrivacyScannerExecution(
                    scanner_id="anonymous_face",
                    status=PrivacyScannerStatus.SCANNER_ERROR,
                    elapsed_seconds=0.0,
                    risks_count=0,
                    error_type="RuntimeError",
                    error_message=(
                        "RuntimeError while running privacy scanner; details redacted"
                    ),
                ),
            )
        )


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0
        self.publish_calls = 0
        self.preview_calls = 0

    def preview(
        self,
        plan: PrivacyPlan,
        source: Path,
        output: Path,
        cancellation: object,
    ) -> Path:
        del plan, source, cancellation
        self.preview_calls += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"bounded private preview")
        return output

    def execute(
        self,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: object,
    ) -> PrivacyNativeResult:
        del source, cancellation
        self.calls += 1
        public = workspace / "share-package"
        public.mkdir(parents=True, exist_ok=True)
        private = workspace / "privacy-review-private"
        private.mkdir(parents=True, exist_ok=True)
        pending = private / f"pending-package-fake-{self.calls}"
        pending.mkdir(parents=True, exist_ok=False)
        candidate = pending / "share-safe.mp4"
        candidate.write_bytes(b"safe copy")
        digest = sha256(candidate.read_bytes()).hexdigest()
        artifact = PrivacyArtifact(
            relative_path="share-safe.mp4",
            sha256=digest,
            description="Locally reviewed privacy-safe sharing copy",
        )
        change_log = PrivacyChangeLog(
            plan_digest=plan.digest,
            processor={"executable": "fake"},
            actions=plan.actions,
            artifacts=(artifact,),
        )
        (pending / "changes.json").write_text(
            change_log.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return PrivacyNativeResult(
            staged_video=candidate,
            change_log=change_log,
            pending_root=pending,
        )

    def publish_pending(
        self,
        pending_root: Path,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: object,
    ) -> Path:
        del plan, source, cancellation
        self.publish_calls += 1
        public = workspace / "share-package"
        if any(public.iterdir()):
            raise PrivacyArtifactError("public package is not empty")
        public.rmdir()
        os.replace(pending_root, public)
        return public / "share-safe.mp4"


class BlockingFakeExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def execute(
        self,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: object,
    ) -> PrivacyNativeResult:
        result = super().execute(plan, source, workspace, cancellation)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test executor release timed out")
        return result


class FakeVerifier:
    def verify(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        private_context: PrivacyVerificationContext,
    ) -> PrivacyVerificationReport:
        del source, candidate
        scanner_failed = bool(private_context.scanner_issues)
        checks = tuple(
            PrivacyVerificationCheck(
                check_id=check_id,
                status=(
                    VerificationStatus.NEEDS_REVIEW
                    if scanner_failed and check_id == "visual_coverage"
                    else VerificationStatus.PASSED
                ),
                message="Local verification result.",
            )
            for check_id in PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS
        )
        return PrivacyVerificationReport(
            plan_digest=plan.digest,
            status=(
                PrivacyJobOutcome.NEEDS_REVIEW
                if scanner_failed
                else PrivacyJobOutcome.COMPLETED
            ),
            checks=checks,
        )


class FailingVerifier:
    def verify(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        private_context: PrivacyVerificationContext,
    ) -> PrivacyVerificationReport:
        del source, candidate, plan, private_context
        raise RuntimeError("injected verification failure")


class NonPublishableVerifier:
    def __init__(self, outcome: PrivacyJobOutcome) -> None:
        self.outcome = outcome

    def verify(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        private_context: PrivacyVerificationContext,
    ) -> PrivacyVerificationReport:
        del source, candidate, private_context
        required_status = {
            PrivacyJobOutcome.FAILED: VerificationStatus.FAILED,
            PrivacyJobOutcome.NEEDS_REVIEW: VerificationStatus.NEEDS_REVIEW,
            PrivacyJobOutcome.PARTIAL: VerificationStatus.PASSED,
        }[self.outcome]
        checks = tuple(
            PrivacyVerificationCheck(
                check_id=check_id,
                status=(required_status if index == 0 else VerificationStatus.PASSED),
                message="Injected non-publishable outcome.",
            )
            for index, check_id in enumerate(PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS)
        )
        if self.outcome is PrivacyJobOutcome.PARTIAL:
            checks = (
                *checks,
                PrivacyVerificationCheck(
                    check_id="scanner_issue:optional_test",
                    status=VerificationStatus.NEEDS_REVIEW,
                    message="Optional scanner needs review.",
                    required=False,
                ),
            )
        return PrivacyVerificationReport(
            plan_digest=plan.digest,
            status=self.outcome,
            checks=checks,
        )


def _pipeline(
    tmp_path: Path,
    *,
    scanner_runner: object | None = None,
    use_default_scanners: bool = False,
    model_runtime: object | None = None,
    executor: FakeExecutor | None = None,
) -> tuple[SafeSharingPipeline, FakeExecutor]:
    source = tmp_path / "中文 source.mp4"
    source.write_bytes(b"source bytes")
    active_executor = executor or FakeExecutor()
    pipeline = SafeSharingPipeline(
        tmp_path / "输出 workspace",
        probe=lambda path: (
            SimpleNamespace(duration_seconds=4.0),
            PrivateProbeSummary(
                duration_seconds=4.0,
                filename=path.name,
                global_tags={},
                stream_tags=(),
                chapter_tags=(),
                attachment_tags=(),
            ),
        ),
        sampler=lambda path, **kwargs: FrameSamplingResult(
            work_directory=kwargs["workspace_parent"] / "frames-work",
            samples=(),
        ),
        scene_detector=lambda path, duration_seconds: (),
        scanner_runner=(
            None if use_default_scanners else scanner_runner or EmptyScannerRunner()
        ),
        metadata_scanner=lambda summary, input_hash, profile: [],
        executor=active_executor,
        verifier=FakeVerifier(),
        **(
            {"model_runtime": cast(Any, model_runtime)}
            if model_runtime is not None
            else {}
        ),
    )
    return pipeline, active_executor


def test_pipeline_requires_review_then_exact_confirmation(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)

    with pytest.raises(PrivacyConfirmationError):
        pipeline.confirm(preparation.preparation_id, "0" * 64)

    result = pipeline.confirm(preparation.preparation_id, preparation.plan.digest)
    assert result.status is PrivacyJobOutcome.NEEDS_REVIEW
    assert result.execution_count == 1


def test_pipeline_does_not_execute_confirmation_twice(tmp_path: Path) -> None:
    pipeline, executor = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)

    pipeline.confirm(preparation.preparation_id, preparation.plan.digest)
    with pytest.raises(PrivacyConfirmationError) as error:
        pipeline.confirm(preparation.preparation_id, preparation.plan.digest)

    assert executor.calls == 1
    assert "already consumed" in (error.value.internal_message or "")


def test_pipeline_preview_uses_bounded_preview_executor_only(tmp_path: Path) -> None:
    pipeline, executor = _pipeline(tmp_path)
    pipeline._preview_executor = executor  # noqa: SLF001 - injected boundary spy
    source = next(tmp_path.glob("*.mp4"))
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)

    preview = pipeline.preview(preparation.preparation_id)

    assert preview.read_bytes() == b"bounded private preview"
    assert executor.preview_calls == 1
    assert executor.calls == 0
    assert executor.publish_calls == 0


def test_concurrent_confirmation_consumes_digest_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)
    original_compare = hmac.compare_digest
    compare_calls = 0
    compare_guard = Lock()
    both_confirmations_reached_digest = Event()

    def racing_compare_digest(left: str, right: str) -> bool:
        nonlocal compare_calls
        with compare_guard:
            compare_calls += 1
            if compare_calls == 2:
                both_confirmations_reached_digest.set()
        both_confirmations_reached_digest.wait(timeout=0.25)
        return original_compare(left, right)

    monkeypatch.setattr(
        "videoscope.privacy.pipeline.hmac.compare_digest",
        racing_compare_digest,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(
            pool.submit(
                pipeline.confirm,
                preparation.preparation_id,
                preparation.plan.digest,
            )
            for _ in range(2)
        )
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except Exception as exc:  # noqa: BLE001 - assertion captures both outcomes
                outcomes.append(exc)

    assert executor.calls == 1
    assert sum(isinstance(item, PrivacyConfirmationError) for item in outcomes) == 1
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1


def test_two_resumed_pipeline_instances_claim_confirmation_exactly_once(
    tmp_path: Path,
) -> None:
    first, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    reviewed = first.review(scan.scan_id, ())
    preparation = first.prepare(reviewed.review_id)
    second, _ = _pipeline(tmp_path, executor=executor)
    second_scan = second.resume(source=source, config=config)
    second_preparation = second.current_preparation(second_scan.scan_id)
    assert second_preparation is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                first.confirm,
                preparation.preparation_id,
                preparation.plan.digest,
            ),
            pool.submit(
                second.confirm,
                second_preparation.preparation_id,
                second_preparation.plan.digest,
            ),
        )
        outcomes: list[object] = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=5))
            except Exception as exc:  # noqa: BLE001 - assertion captures both outcomes
                outcomes.append(exc)

    assert executor.calls == 1
    assert sum(isinstance(item, PrivacyConfirmationError) for item in outcomes) == 1
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1


def test_existing_confirmation_claim_fails_closed_without_execution(
    tmp_path: Path,
) -> None:
    pipeline, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)
    private_root = next(tmp_path.rglob("privacy-review-private"))
    claim = private_root / f"confirmation-claim-{preparation.preparation_id}.lock"
    claim.write_text(preparation.plan.digest + "\n", encoding="utf-8")

    with pytest.raises(PrivacyConfirmationError):
        pipeline.confirm(preparation.preparation_id, preparation.plan.digest)

    assert executor.calls == 0
    assert claim.is_file()


def test_stale_discard_cannot_remove_active_claim_or_pending_package(
    tmp_path: Path,
) -> None:
    executor = BlockingFakeExecutor()
    first, _ = _pipeline(tmp_path, executor=executor)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    reviewed = first.review(scan.scan_id, ())
    preparation = first.prepare(reviewed.review_id)
    stale, _ = _pipeline(tmp_path, executor=executor)
    stale_scan = stale.resume(source=source, config=config)
    stale_preparation = stale.current_preparation(stale_scan.scan_id)
    assert stale_preparation is not None

    with ThreadPoolExecutor(max_workers=1) as pool:
        confirmation = pool.submit(
            first.confirm,
            preparation.preparation_id,
            preparation.plan.digest,
        )
        assert executor.started.wait(timeout=5)
        private_root = next(tmp_path.rglob("privacy-review-private"))
        claim = private_root / (f"confirmation-claim-{preparation.preparation_id}.lock")
        pending = next(private_root.glob("pending-package-*"))
        discard_error: Exception | None = None
        try:
            stale.discard(stale_preparation.preparation_id)
        except Exception as exc:  # noqa: BLE001 - exact type asserted below
            discard_error = exc
        finally:
            executor.release.set()
        result = confirmation.result(timeout=5)

    assert isinstance(discard_error, PrivacyInputError)
    assert claim.is_file()
    assert not pending.exists()
    assert result.execution_count == 1
    with pytest.raises(PrivacyConfirmationError):
        stale.confirm(
            stale_preparation.preparation_id,
            stale_preparation.plan.digest,
        )
    assert executor.calls == 1


def test_confirm_cannot_continue_when_discard_transition_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    reviewed = first.review(scan.scan_id, ())
    preparation = first.prepare(reviewed.review_id)
    stale, _ = _pipeline(tmp_path, executor=executor)
    stale.resume(source=source, config=config)
    private_root = next(tmp_path.rglob("privacy-review-private")).resolve()
    discard_entered = Event()
    release_discard = Event()
    original_rmtree = shutil.rmtree

    def blocking_rmtree(path: object, *args: object, **kwargs: object) -> None:
        candidate = Path(cast(Any, path)).resolve()
        if candidate == private_root:
            discard_entered.set()
            if not release_discard.wait(timeout=5):
                raise RuntimeError("test discard release timed out")
        cast(Any, original_rmtree)(path, *args, **kwargs)

    monkeypatch.setattr(
        "videoscope.privacy.pipeline.shutil.rmtree",
        blocking_rmtree,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        discarded = pool.submit(stale.discard, preparation.preparation_id)
        assert discard_entered.wait(timeout=5)
        try:
            with pytest.raises(PrivacyConfirmationError):
                first.confirm(preparation.preparation_id, preparation.plan.digest)
        finally:
            release_discard.set()
        discarded.result(timeout=5)

    assert executor.calls == 0
    assert not private_root.exists()


def test_stale_instance_cannot_review_after_discard_wins(tmp_path: Path) -> None:
    first, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    stale, _ = _pipeline(tmp_path, executor=executor)
    stale_scan = stale.resume(source=source, config=config)
    output = tmp_path / "杈撳嚭 workspace"

    first.discard(scan.scan_id)

    with pytest.raises(PrivacyInputError):
        stale.review(stale_scan.scan_id, ())

    assert not (output / "privacy-review-private").exists()
    assert not (output / "share-package").exists()
    assert executor.calls == 0


def test_stale_prepare_then_confirm_cannot_revive_discarded_lifecycle(
    tmp_path: Path,
) -> None:
    first, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    reviewed = first.review(scan.scan_id, ())
    stale, _ = _pipeline(tmp_path, executor=executor)
    stale.resume(source=source, config=config)
    output = tmp_path / "杈撳嚭 workspace"

    first.discard(reviewed.review_id)

    with pytest.raises((PrivacyInputError, PrivacyConfirmationError)):
        preparation = stale.prepare(reviewed.review_id)
        stale.confirm(preparation.preparation_id, preparation.plan.digest)

    assert not (output / "privacy-review-private").exists()
    assert not (output / "share-package").exists()
    assert executor.calls == 0


def test_stale_preview_cannot_revive_discarded_lifecycle(tmp_path: Path) -> None:
    first, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    reviewed = first.review(scan.scan_id, ())
    preparation = first.prepare(reviewed.review_id)
    stale, _ = _pipeline(tmp_path, executor=executor)
    stale.resume(source=source, config=config)
    stale._preview_executor = executor  # noqa: SLF001 - execution boundary spy
    output = tmp_path / "杈撳嚭 workspace"

    first.discard(preparation.preparation_id)

    with pytest.raises((PrivacyInputError, PrivacyConfirmationError)):
        stale.preview(preparation.preparation_id)

    assert not (output / "privacy-review-private").exists()
    assert not (output / "share-package").exists()
    assert executor.calls == 0


def test_fresh_pipeline_cannot_scan_or_resume_discarded_output_root(
    tmp_path: Path,
) -> None:
    first, executor = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    config = SafeSharingConfig()
    scan = first.scan(source=source, config=config)
    output = tmp_path / "杈撳嚭 workspace"

    first.discard(scan.scan_id)
    fresh, _ = _pipeline(tmp_path, executor=executor)

    with pytest.raises(PrivacyInputError):
        fresh.scan(source=source, config=config)
    with pytest.raises(PrivacyInputError):
        fresh.resume(source=source, config=config)

    assert not (output / "privacy-review-private").exists()
    assert not (output / "share-package").exists()
    assert executor.calls == 0


def test_pipeline_persists_private_review_state_without_public_paths(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    review = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(review.review_id)

    private = tmp_path / "输出 workspace" / "privacy-review-private"
    assert (private / "risk-map.json").is_file()
    assert (private / "review.json").is_file()
    assert (private / "plan.json").is_file()
    assert (
        pipeline.load_preparation(preparation.preparation_id).plan == preparation.plan
    )
    public_json = list((tmp_path / "输出 workspace" / "share-package").glob("*.json"))
    assert public_json == []


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "crafted_sampling_identity",
    (
        "../../outside-frames",
        r"C:\outside-frames",
        "/outside-frames",
    ),
)
def test_resume_rejects_sampling_workspace_escape_without_deleting_external_data(
    tmp_path: Path,
    crafted_sampling_identity: str,
) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = next(tmp_path.glob("*.mp4"))
    pipeline.scan(source=source, config=SafeSharingConfig())
    state_path = next(tmp_path.rglob("pipeline-state.json"))
    output = state_path.parents[1]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    external = tmp_path / "outside-frames"
    external.mkdir()
    sentinel = external / "must-survive.txt"
    sentinel.write_text("private data", encoding="utf-8")
    state["sampling_work_directory"] = crafted_sampling_identity
    state_path.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    restored = SafeSharingPipeline(
        output,
        probe=lambda path: (_ for _ in ()).throw(AssertionError(path)),
        sampler=lambda path, **kwargs: (_ for _ in ()).throw(
            AssertionError((path, kwargs))
        ),
        scene_detector=lambda path, duration_seconds: (),
        scanner_runner=EmptyScannerRunner(),
        metadata_scanner=lambda summary, input_hash, profile: [],
        executor=FakeExecutor(),
        verifier=FakeVerifier(),
    )

    with pytest.raises(PrivacyArtifactError):
        restored.resume(source=source, config=SafeSharingConfig())

    assert sentinel.read_text(encoding="utf-8") == "private data"


def test_scanner_failure_is_conservative_and_does_not_abort_other_stages(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path, scanner_runner=FailingScannerRunner())
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)

    result = pipeline.confirm(preparation.preparation_id, preparation.plan.digest)

    assert result.status is PrivacyJobOutcome.NEEDS_REVIEW
    assert any(
        execution.status is PrivacyScannerStatus.SCANNER_ERROR
        for execution in scan.scanner_executions
    )


def test_required_ocr_disabled_is_an_explicit_manual_review_issue(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path, use_default_scanners=True)
    source = next(tmp_path.glob("*.mp4"))

    scan = pipeline.scan(source=source, config=SafeSharingConfig(enable_ocr=False))
    text_execution = next(
        execution
        for execution in scan.scanner_executions
        if execution.scanner_id == "suspicious_text"
    )
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)
    result = pipeline.confirm(preparation.preparation_id, preparation.plan.digest)

    assert text_execution.status is PrivacyScannerStatus.SKIPPED
    assert text_execution.fallback == "manual_visual_region"
    assert result.status is PrivacyJobOutcome.NEEDS_REVIEW


def test_enabled_ocr_scanner_receives_shared_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    class CapturingTextScanner:
        id = "suspicious_text"
        display_name = "Capturing suspicious text scanner"
        version = "1.0.0"
        description = "Test scanner that captures its shared runtime."
        requirements = SuspiciousTextScanner.requirements
        config_model = SuspiciousTextScanner.config_model

        def __init__(self, runtime: ModelRuntimeManager | None) -> None:
            captured.append(runtime)

        def scan(
            self,
            context: PrivacyScanContext,
            config: BaseModel,
        ) -> list[PrivacyRisk]:
            del context, config
            return []

    runtime = object()
    monkeypatch.setattr(
        "videoscope.privacy.pipeline.SuspiciousTextScanner",
        CapturingTextScanner,
    )
    pipeline, _ = _pipeline(
        tmp_path,
        use_default_scanners=True,
        model_runtime=runtime,
    )
    source = next(tmp_path.glob("*.mp4"))

    pipeline.scan(source=source, config=SafeSharingConfig(enable_ocr=True))

    assert captured == [runtime]


def test_enabled_ocr_without_provider_is_an_explicit_scanner_issue(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path, use_default_scanners=True)
    source = next(tmp_path.glob("*.mp4"))

    scan = pipeline.scan(source=source, config=SafeSharingConfig(enable_ocr=True))
    text_execution = next(
        execution
        for execution in scan.scanner_executions
        if execution.scanner_id == "suspicious_text"
    )

    assert text_execution.status is PrivacyScannerStatus.SKIPPED
    assert text_execution.fallback == "manual_visual_region"


def test_verification_failure_leaves_no_public_artifact_or_pending_package(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path)
    pipeline._verifier = FailingVerifier()  # noqa: SLF001 - fault injection boundary
    source = next(tmp_path.glob("*.mp4"))
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)

    with pytest.raises(RuntimeError, match="injected verification failure"):
        pipeline.confirm(preparation.preparation_id, preparation.plan.digest)

    public = next(tmp_path.rglob("share-package"))
    assert list(public.iterdir()) == []
    assert not any(public.parent.glob("share-package.pending-*"))


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "outcome",
    (
        PrivacyJobOutcome.FAILED,
        PrivacyJobOutcome.NEEDS_REVIEW,
        PrivacyJobOutcome.PARTIAL,
    ),
)
def test_noncompleted_verification_never_publishes_share_package(
    tmp_path: Path,
    outcome: PrivacyJobOutcome,
) -> None:
    pipeline, executor = _pipeline(tmp_path)
    pipeline._verifier = NonPublishableVerifier(  # noqa: SLF001 - fault injection
        outcome
    )
    source = next(tmp_path.glob("*.mp4"))
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    reviewed = pipeline.review(scan.scan_id, ())
    preparation = pipeline.prepare(reviewed.review_id)

    result = pipeline.confirm(preparation.preparation_id, preparation.plan.digest)

    assert result.status is outcome
    public = next(tmp_path.rglob("share-package"))
    assert list(public.iterdir()) == []
    assert executor.publish_calls == 0
    assert not any(public.parent.glob("pending-package-*"))


def test_discard_removes_unclaimed_private_state_but_never_source(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    original = source.read_bytes()
    scan = pipeline.scan(source=source, config=SafeSharingConfig())

    pipeline.discard(scan.scan_id)

    assert source.read_bytes() == original
    assert not (tmp_path / "输出 workspace" / "privacy-review-private").exists()


def test_review_rejects_unknown_risk_and_duplicate_review(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    unknown = PrivacyReviewDecision(
        risk_id="privacy_risk_" + "a" * 64,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.BLUR,
        reviewed_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    with pytest.raises(PrivacyInputError):
        pipeline.review(scan.scan_id, (unknown, unknown))


def test_manual_review_regions_are_persisted_and_enter_the_exact_plan(
    tmp_path: Path,
) -> None:
    pipeline, executor = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    visual = ManualVisualRegionInput(
        start_seconds=0.5,
        end_seconds=1.5,
        box=NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.6),
        style=RedactionStyle.PIXELATE,
    )
    audio = ManualAudioIntervalInput(start_seconds=2.0, end_seconds=3.0)

    reviewed = pipeline.review(
        scan.scan_id,
        (),
        manual_visual_regions=(visual,),
        manual_audio_intervals=(audio,),
    )
    restored, _ = _pipeline(tmp_path, executor=executor)
    restored_scan = restored.resume(source=source, config=SafeSharingConfig())
    restored_review = restored.current_review(restored_scan.scan_id)
    assert restored_review is not None
    assert [risk.risk_type.value for risk in restored_review.manual_risks] == [
        "manual_visual",
        "manual_audio",
    ]

    preparation = restored.prepare(reviewed.review_id)
    assert [risk.risk_type.value for risk in preparation.plan.risks] == [
        "manual_visual",
        "manual_audio",
    ]
    assert preparation.plan.risks[0].box == visual.box
    assert preparation.plan.risks[1].start_seconds == 2.0


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "visuals,audio",
    [
        (
            (
                ManualVisualRegionInput(
                    start_seconds=3.5,
                    end_seconds=4.5,
                    box=NormalizedBox(
                        x_min=0.1,
                        y_min=0.1,
                        x_max=0.3,
                        y_max=0.3,
                    ),
                    style=RedactionStyle.BLUR,
                ),
            ),
            (),
        ),
        ((), (ManualAudioIntervalInput(start_seconds=3.5, end_seconds=4.5),)),
    ],
)
def test_manual_review_rejects_intervals_past_source_duration(
    tmp_path: Path,
    visuals: tuple[ManualVisualRegionInput, ...],
    audio: tuple[ManualAudioIntervalInput, ...],
) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())

    with pytest.raises(PrivacyInputError) as error:
        pipeline.review(
            scan.scan_id,
            (),
            manual_visual_regions=visuals,
            manual_audio_intervals=audio,
        )
    assert "manual privacy selection" in (error.value.internal_message or "")


def test_manual_review_rejects_duplicate_deterministic_regions(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    region = ManualVisualRegionInput(
        start_seconds=0.5,
        end_seconds=1.5,
        box=NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.6),
        style=RedactionStyle.BLUR,
    )

    with pytest.raises(PrivacyInputError) as error:
        pipeline.review(
            scan.scan_id,
            (),
            manual_visual_regions=(region, region),
        )
    assert "duplicate" in (error.value.internal_message or "")


def test_private_state_file_is_not_a_public_absolute_path_leak(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path)
    source = tmp_path / "中文 source.mp4"
    scan = pipeline.scan(source=source, config=SafeSharingConfig())
    pipeline.review(scan.scan_id, ())

    public_root = tmp_path / "输出 workspace" / "share-package"
    for path in public_root.rglob("*.json"):
        content = json.loads(path.read_text(encoding="utf-8"))
        assert str(source.resolve()) not in json.dumps(content, ensure_ascii=False)


def test_safe_sharing_config_cannot_change_after_scan_identity_is_created() -> None:
    config = SafeSharingConfig(scanner_configurations={"qr_barcode": {}})

    with pytest.raises(TypeError):
        cast(dict[str, Any], config.scanner_configurations)["anonymous_face"] = {}
