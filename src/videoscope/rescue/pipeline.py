"""Review-gated, local-only orchestration for Video Rescue."""

from __future__ import annotations

import hmac
import math
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from videoscope.processes import (
    PinnedDescriptorError,
    hash_descriptor,
    pinned_descriptor_path,
    secure_read_open,
)
from videoscope.rescue.action_roles import (
    REMAINING_IMPROVEMENT_ACTION_KINDS,
    action_artifact_role,
)
from videoscope.rescue.artifacts import (
    RescueArtifactLayout,
    project_public_rescue_verification,
    publish_verified_rescue,
)
from videoscope.rescue.assessment import (
    LocalRescueAssessmentService,
    RescueAssessmentBundle,
    RescueAssessmentWarning,
)
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueConfirmationError,
    RescueError,
    RescueInputError,
    RescueMediaError,
    RescuePlanError,
    RescueQualificationUnavailableError,
    RescueScanError,
)
from videoscope.rescue.executor import (
    NativeRescueExecutor,
    RescueExecutionResult,
    SourceMapping,
)
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageKind,
    MediaDamageMap,
    RescueActionExecution,
    RescueActionExecutionStatus,
    RescueActionKind,
    RescueChangeLog,
    RescueConfirmation,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    RescueTechnicalReport,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.preview import RescuePreviewBuilder, RescuePreviewSet
from videoscope.rescue.qualification import (
    NativeRescueCandidateQualifier,
    SharpenQualificationEvidenceV1,
)
from videoscope.rescue.report import render_rescue_report
from videoscope.rescue.scanner import RescueScanConfig, RescueScanner
from videoscope.rescue.serialization import (
    write_damage_map_json,
    write_rescue_change_log_json,
    write_rescue_plan_json,
    write_tonal_encoded_qualification_json,
)
from videoscope.rescue.stabilization import (
    StabilizationQualificationEvidenceV1,
    UnavailableStabilizationCandidateQualifier,
    UnavailableStabilizationImmediateParentProvider,
    validate_stabilization_immediate_parent_handle,
)
from videoscope.rescue.symptoms import RescueSymptomAssessment, classify_symptoms
from videoscope.rescue.tonal_qualification import (
    NativeTonalCandidateQualifier,
    TonalEncodedQualificationEvidenceV3,
)
from videoscope.rescue.verification import RescueVerifier
from videoscope.video.probe import probe_video


class RescueStatus(StrEnum):
    SCANNING = "scanning"
    PLANNING = "planning"
    PREVIEWING = "previewing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = frozenset(
    {
        RescueStatus.COMPLETED,
        RescueStatus.PARTIAL,
        RescueStatus.NEEDS_REVIEW,
        RescueStatus.FAILED,
        RescueStatus.CANCELLED,
    }
)


def _cleanup_verification_controls(
    private_root: Path, handles: tuple[Any, ...]
) -> None:
    """Delete only runtime controls proven to belong to this private workspace."""
    try:
        resolved_root = Path(private_root).resolve(strict=True)
    except OSError:
        raise RescueArtifactError(
            "private verification control root is unavailable"
        ) from None
    validated: list[Path] = []
    try:
        for handle in handles:
            for raw_path in tuple(handle.cleanup_paths):
                path = Path(raw_path)
                if path.is_symlink():
                    raise ValueError("verification control path is a symlink")
                path.resolve(strict=False).relative_to(resolved_root)
                validated.append(path)
    except (AttributeError, OSError, TypeError, ValueError):
        raise RescueArtifactError(
            "private verification control cleanup failed"
        ) from None
    cleanup_failed = False
    for path in tuple(dict.fromkeys(validated)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True
    if cleanup_failed or any(path.exists() for path in validated):
        raise RescueArtifactError("private verification control cleanup failed")


def _cleanup_stabilization_parent_root(private_root: Path, parent_root: Path) -> None:
    """Remove the complete fixed parent root without following symlinks."""
    parent_root = Path(parent_root)
    if not parent_root.exists() and not parent_root.is_symlink():
        return
    try:
        resolved_private = Path(private_root).resolve(strict=True)
        if parent_root.is_symlink() or not parent_root.is_dir():
            raise ValueError
        resolved_parent = parent_root.resolve(strict=True)
        resolved_parent.relative_to(resolved_private)
        if resolved_parent == resolved_private:
            raise ValueError

        def remove_owned(path: Path) -> None:
            if path.is_symlink():
                path.unlink()
                return
            if path.is_dir():
                for child in path.iterdir():
                    remove_owned(child)
                path.rmdir()
                return
            if path.is_file():
                path.unlink()
                return
            raise OSError("unsupported private parent artifact")

        remove_owned(parent_root)
    except (OSError, ValueError):
        raise RescueArtifactError(
            "stabilization immediate-parent cleanup failed"
        ) from None


@dataclass(frozen=True, slots=True)
class RescueConfig:
    output_directory: Path
    strategy: RescueStrategy = RescueStrategy.CONSERVATIVE
    symptoms: tuple[RescueSymptom, ...] = ()
    locked_ranges: tuple[tuple[float, float], ...] = ()
    preview_seconds: float = 10.0
    keep_workspace: bool = False
    effective_config: RescueEffectiveConfig = field(
        default_factory=RescueEffectiveConfig
    )

    def __post_init__(self) -> None:
        if not isinstance(self.output_directory, Path):
            object.__setattr__(self, "output_directory", Path(self.output_directory))
        if not isinstance(self.strategy, RescueStrategy):
            object.__setattr__(self, "strategy", RescueStrategy(self.strategy))
        canonical_symptoms: list[RescueSymptom] = []
        for value in self.symptoms:
            if isinstance(value, RescueSymptom):
                symptom = value
            else:
                if not isinstance(value, str) or not value:
                    raise RescueInputError("symptom hints must not be empty")
                try:
                    symptom = RescueSymptom(value)
                except ValueError as exc:
                    raise RescueInputError("unsupported Rescue symptom hint") from exc
            if symptom in canonical_symptoms:
                raise RescueInputError("duplicate Rescue symptom hint")
            canonical_symptoms.append(symptom)
        if RescueSymptom.MISSING_AUDIO in canonical_symptoms and any(
            item
            in {
                RescueSymptom.AUDIO_VIDEO_OFFSET,
                RescueSymptom.LOW_LOUDNESS,
                RescueSymptom.AUDIO_NOISE,
                RescueSymptom.AUDIO_CLIPPING,
            }
            for item in canonical_symptoms
        ):
            raise RescueInputError("conflicting audio symptom hints")
        object.__setattr__(self, "symptoms", tuple(canonical_symptoms))
        if (
            not math.isfinite(self.preview_seconds)
            or self.preview_seconds <= 0
            or self.preview_seconds > 10
        ):
            raise ValueError("preview_seconds must be in the range (0, 10]")
        for start, end in self.locked_ranges:
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end < start
            ):
                raise ValueError("locked ranges must be ordered non-negative seconds")


@dataclass(frozen=True, slots=True)
class RescuePreparation:
    """One issued, immutable plan object, valid only to its issuing pipeline."""

    source_hash: str
    plan: RescuePlan
    damage_map: MediaDamageMap
    assessments: RescueAssessmentBundle
    symptom_assessments: tuple[RescueSymptomAssessment, ...]
    status: RescueStatus
    previews: RescuePreviewSet | None


@dataclass(frozen=True, slots=True)
class RescueResult:
    status: RescueStatus
    faithful_path: Path | None
    improved_path: Path | None
    technical_report: RescueTechnicalReport | None
    report_path: Path | None
    public_root: Path | None
    source_mappings: tuple[SourceMapping, ...] = ()
    failed_source_ranges: tuple[tuple[float, float], ...] = ()

    @property
    def verification(self) -> RescueVerificationReport | None:
        """Expose the independently derived report without duplicating state."""
        return self.technical_report.verification if self.technical_report else None


@dataclass(slots=True)
class RescuePipelineDependencies:
    """Injectable local boundaries; defaults are the Task 1-8 production modules."""

    probe: Callable[[Path], Any] = probe_video
    scanner: Any = field(default_factory=RescueScanner)
    assessment_service: Any = field(default_factory=LocalRescueAssessmentService)
    planner: Callable[..., RescuePlan] = build_rescue_plan
    candidate_qualifier: Any = field(default_factory=NativeRescueCandidateQualifier)
    tonal_candidate_qualifier: Any = field(
        default_factory=NativeTonalCandidateQualifier
    )
    stabilization_candidate_qualifier: Any = field(
        default_factory=UnavailableStabilizationCandidateQualifier
    )
    stabilization_parent_provider: Any = field(
        default_factory=UnavailableStabilizationImmediateParentProvider
    )
    preview_builder: Any = field(default_factory=RescuePreviewBuilder)
    executor: Any = field(default_factory=NativeRescueExecutor)
    verifier: Any = field(default_factory=RescueVerifier)
    report_renderer: Callable[..., str] = render_rescue_report
    publisher: Callable[..., tuple[Any, ...]] = publish_verified_rescue


@dataclass(slots=True)
class _Issued:
    preparation: RescuePreparation
    layout: RescueArtifactLayout
    source: Path
    plan: RescuePlan
    plan_digest: str
    source_descriptor: int
    confirmation: RescueConfirmation | None = None
    consumed: bool = False
    executing: bool = False


class VideoRescuePipeline:
    """Prepare, explicitly confirm, and execute a bounded local Rescue job."""

    def __init__(
        self,
        config: RescueConfig,
        *,
        dependencies: RescuePipelineDependencies | None = None,
        progress: Callable[[RescueStatus], None] | None = None,
    ) -> None:
        self._config = config
        self._dependencies = dependencies or RescuePipelineDependencies()
        self._progress = progress
        self._issued: dict[int, _Issued] = {}
        self._cancelled = False
        self._terminal_status: RescueStatus | None = None

    def cancel(self) -> None:
        """Request cancellation; every local boundary is checked afterwards."""
        self._cancelled = True
        self.abort()

    def abort(self, preparation: RescuePreparation | None = None) -> None:
        """Release one or all preparations that have not started execution."""
        if preparation is not None:
            issued = self._issued.get(id(preparation))
            if (
                issued is not None
                and issued.preparation is preparation
                and not issued.executing
            ):
                self._release_issued(preparation)
            return
        for issued in tuple(self._issued.values()):
            if not issued.executing:
                self._release_issued(issued.preparation)

    def close(self) -> None:
        """Cancel this pipeline and idempotently release retained sources."""
        self.cancel()

    def prepare(self, source: Path) -> RescuePreparation:
        source = Path(source)
        self._check_cancelled()
        try:
            source_descriptor = secure_read_open(source)
        except (OSError, PinnedDescriptorError) as exc:
            raise RescueInputError("source video could not be opened safely") from exc
        source_initialized = False
        try:
            source_for_io = (
                pinned_descriptor_path(source_descriptor)
                if os.name == "posix"
                else source
            )
            source_hash = hash_descriptor(source_descriptor)
            source_initialized = True
        except (OSError, PinnedDescriptorError) as exc:
            raise RescueInputError("source video could not be opened safely") from exc
        finally:
            if not source_initialized:
                os.close(source_descriptor)
        layout: RescueArtifactLayout | None = None
        descriptor_issued = False
        try:
            layout = RescueArtifactLayout.create(self._config.output_directory)
            self._emit(RescueStatus.SCANNING)
            metadata = self._call_scan(source_for_io, source_hash)
            base_damage_map = metadata[1]
            if base_damage_map.input_hash != source_hash:
                raise RescueScanError("damage map is bound to different source bytes")
            self._check_cancelled()
            try:
                assessments = self._dependencies.assessment_service.assess(
                    source_for_io,
                    source_hash,
                    metadata[0],
                    base_damage_map,
                    layout.private_root / "assessment",
                    self._is_cancelled,
                )
            except RescueCancelledError:
                raise
            except Exception as exc:
                assessments = RescueAssessmentBundle(
                    warnings=(
                        RescueAssessmentWarning(
                            component="assessment",
                            error_type=type(exc).__name__,
                            message="The local assessment service was unavailable.",
                        ),
                    ),
                    limitations=(
                        "Balanced improvements were omitted because measured "
                        "assessment evidence was unavailable.",
                    ),
                )
            assessments = _mark_requested_audio_evidence_gaps(
                assessments, self._config.symptoms, base_damage_map
            )
            self._check_cancelled()
            damage_map = assessments.merge_damage_map(base_damage_map)
            symptom_assessments = classify_symptoms(damage_map, self._config.symptoms)
            write_damage_map_json(
                damage_map, layout.private_root / "damage-map-private.json"
            )

            self._emit(RescueStatus.PLANNING)
            effective = self._config.effective_config.model_copy(
                update={
                    "max_preview_total_seconds": self._config.preview_seconds,
                    "locked_ranges": self._config.locked_ranges,
                }
            )
            planner_inputs = {
                "metadata": metadata[0],
                "damage_map": damage_map,
                "strategy": self._config.strategy,
                "config": effective,
                "locked_ranges": self._config.locked_ranges,
                "requested_symptoms": self._config.symptoms,
                "assessment_parameters": dict(assessments.parameters),
                "assessment_limitations": assessments.limitations,
                "assessment_warnings": tuple(
                    warning.message for warning in assessments.warnings
                ),
                "visual_assessment": assessments.visual_assessment,
                "flicker_correction": assessments.flicker_correction,
                "stabilization_assessment": (assessments.stabilization_assessment),
                "audio_assessment": assessments.audio_assessment,
                "fixed_offset_assessment": assessments.fixed_offset_assessment,
            }
            try:
                draft_plan = self._dependencies.planner(**planner_inputs)
                tonal_qualification: TonalEncodedQualificationEvidenceV3 | None = None
                had_draft_tonal = any(
                    action.kind is RescueActionKind.DENOISE_AUDIO
                    and action.parameters.get("interference_profiles")
                    for action in draft_plan.actions
                )
                tonal_plan = draft_plan
                if had_draft_tonal:
                    try:
                        tonal_qualification = (
                            self._dependencies.tonal_candidate_qualifier.qualify(
                                draft_plan,
                                source_for_io,
                                layout.private_root / "tonal-qualification",
                                self._is_cancelled,
                            )
                        )
                        write_tonal_encoded_qualification_json(
                            tonal_qualification,
                            layout.private_root
                            / "tonal-qualification-evidence-private.json",
                        )
                    except RescueCancelledError:
                        raise
                    except RescueArtifactError:
                        raise
                    except OSError as exc:
                        raise RescueArtifactError(
                            "tonal qualification private cleanup failed"
                        ) from exc
                    except Exception:
                        tonal_qualification = None
                    self._check_cancelled()
                    tonal_plan = self._dependencies.planner(
                        **planner_inputs,
                        tonal_qualification=tonal_qualification,
                        require_tonal_qualification=True,
                    )
                sharpen_qualification: SharpenQualificationEvidenceV1 | None = None
                had_draft_sharpen = any(
                    action.kind is RescueActionKind.SHARPEN
                    for action in tonal_plan.actions
                )
                if had_draft_sharpen:
                    try:
                        sharpen_qualification = (
                            self._dependencies.candidate_qualifier.qualify(
                                tonal_plan,
                                source_for_io,
                                layout.private_root / "sharpen-qualification",
                                self._is_cancelled,
                            )
                        )
                    except RescueCancelledError:
                        raise
                    except RescueArtifactError:
                        raise
                    except OSError as exc:
                        raise RescueArtifactError(
                            "sharpen qualification private cleanup failed"
                        ) from exc
                    except Exception:
                        sharpen_qualification = None
                    self._check_cancelled()
                    plan = self._dependencies.planner(
                        **planner_inputs,
                        sharpen_qualification=sharpen_qualification,
                        require_sharpen_qualification=True,
                        tonal_qualification=tonal_qualification,
                        require_tonal_qualification=had_draft_tonal,
                    )
                else:
                    plan = tonal_plan
                stabilization_qualification: (
                    StabilizationQualificationEvidenceV1 | None
                ) = None
                if any(
                    action.kind is RescueActionKind.STABILIZE
                    and action.parameters.get("method") == "transition_anchor_v1"
                    for action in plan.actions
                ):
                    stabilization_parent = None
                    stabilization_parent_root = (
                        layout.private_root / "stabilization-parent"
                    )
                    stabilization_parent_root_owned = False
                    try:
                        stabilization_parent_root.mkdir(parents=False, exist_ok=False)
                        stabilization_parent_root_owned = True
                        stabilization_parent = (
                            self._dependencies.stabilization_parent_provider.provide(
                                plan,
                                source_for_io,
                                stabilization_parent_root,
                                self._is_cancelled,
                            )
                        )
                        validate_stabilization_immediate_parent_handle(
                            stabilization_parent, stabilization_parent_root
                        )
                        qualifier = self._dependencies.stabilization_candidate_qualifier
                        stabilization_qualification = qualifier.qualify(
                            plan,
                            stabilization_parent,
                            layout.private_root / "stabilization-qualification",
                            self._is_cancelled,
                        )
                    except RescueQualificationUnavailableError:
                        stabilization_qualification = None
                    finally:
                        if stabilization_parent_root_owned:
                            _cleanup_stabilization_parent_root(
                                layout.private_root, stabilization_parent_root
                            )
                    self._check_cancelled()
                    if stabilization_qualification is not None:
                        plan = self._dependencies.planner(
                            **planner_inputs,
                            sharpen_qualification=sharpen_qualification,
                            require_sharpen_qualification=had_draft_sharpen,
                            tonal_qualification=tonal_qualification,
                            require_tonal_qualification=had_draft_tonal,
                            stabilization_qualification=stabilization_qualification,
                        )
            except RescueError:
                raise
            except Exception as exc:
                raise RescuePlanError("local Rescue planning failed") from exc
            self._check_cancelled()
            if plan.input_hash != source_hash:
                raise RescuePlanError("Rescue plan is bound to different source bytes")
            write_rescue_plan_json(
                plan, layout.private_root / "rescue-plan-private.json"
            )

            self._emit(RescueStatus.PREVIEWING)
            try:
                previews = self._dependencies.preview_builder.build(
                    plan, source_for_io, layout.private_root / "previews"
                )
            except RescueError:
                raise
            except Exception as exc:
                raise RescueMediaError("local Rescue preview failed") from exc
            self._check_cancelled()
            if not hmac.compare_digest(hash_descriptor(source_descriptor), source_hash):
                raise RescueConfirmationError("source bytes changed during preparation")

            preparation = RescuePreparation(
                source_hash=source_hash,
                plan=plan,
                damage_map=damage_map,
                assessments=assessments,
                symptom_assessments=symptom_assessments,
                status=RescueStatus.AWAITING_CONFIRMATION,
                previews=previews,
            )
            self.abort()
            self._issued[id(preparation)] = _Issued(
                preparation,
                layout,
                source_for_io,
                plan,
                plan.plan_digest,
                source_descriptor,
            )
            descriptor_issued = True
            self._emit(RescueStatus.AWAITING_CONFIRMATION)
            return preparation
        except RescueCancelledError:
            self._emit_terminal(RescueStatus.CANCELLED)
            self._cleanup(layout)
            raise
        except Exception:
            self._emit_terminal(RescueStatus.FAILED)
            self._cleanup(layout)
            raise
        finally:
            if not descriptor_issued:
                os.close(source_descriptor)

    def confirm(
        self,
        preparation: RescuePreparation | None,
        confirmation: RescueConfirmation | None,
    ) -> RescuePreparation:
        issued = self._require_issued(preparation)
        if issued.consumed:
            raise RescueConfirmationError("prepared Rescue plans cannot be reused")
        if confirmation is None:
            raise RescueConfirmationError("a confirmation is required")
        if not hmac.compare_digest(
            hash_descriptor(issued.source_descriptor), issued.preparation.source_hash
        ):
            raise RescueConfirmationError("source bytes no longer match preparation")
        self._validate_confirmation(issued.preparation, confirmation)
        issued.confirmation = confirmation
        return issued.preparation

    def execute(
        self,
        preparation: RescuePreparation,
        confirmation: RescueConfirmation,
    ) -> RescueResult:
        issued = self._require_issued(preparation)
        if issued.consumed:
            raise RescueConfirmationError("prepared Rescue plans cannot be reused")
        if issued.confirmation is not confirmation:
            raise RescueConfirmationError("execute requires the exact confirmation")
        assert confirmation is not None
        self._validate_confirmation(issued.preparation, confirmation)
        issued.consumed = True
        issued.executing = True
        source = issued.source
        try:
            self._check_cancelled()
            if not hmac.compare_digest(
                hash_descriptor(issued.source_descriptor),
                issued.preparation.source_hash,
            ):
                raise RescueConfirmationError(
                    "source bytes no longer match preparation"
                )
            self._emit(RescueStatus.PROCESSING)
            execution_plan = _execution_plan(
                issued.preparation.plan, confirmation.accepted_action_ids
            )
            execution: RescueExecutionResult = (
                self._dependencies.executor.execute_faithful(
                    execution_plan,
                    source,
                    issued.layout.private_root,
                    self._is_cancelled,
                )
            )
            restoration = getattr(
                self._dependencies.executor, "execute_faithful_restoration", None
            )
            if callable(restoration):
                execution = restoration(
                    execution_plan,
                    execution,
                    issued.layout.private_root,
                    self._is_cancelled,
                )

            try:
                self._check_cancelled()
            except RescueCancelledError:
                _cleanup_verification_controls(
                    issued.layout.private_root,
                    tuple(execution.verification_controls),
                )
                raise

            improved_path: Path | None = None
            improved_verification_controls: tuple[Any, ...] = ()
            all_verification_controls: list[Any] = list(execution.verification_controls)
            improvement_failure = False
            limitations: list[str] = []
            if confirmation.publish_improved:
                try:
                    execute_with_controls = getattr(
                        self._dependencies.executor,
                        "execute_improved_with_controls",
                        None,
                    )
                    if callable(execute_with_controls):
                        improved_result = execute_with_controls(
                            execution_plan,
                            execution.output_path,
                            issued.layout.private_root,
                            self._is_cancelled,
                            source_mappings=execution.source_mappings,
                            inherited_action_ids=execution.applied_action_ids,
                        )
                        improved_path = Path(improved_result.output_path)
                        improved_verification_controls = tuple(
                            improved_result.verification_controls
                        )
                        all_verification_controls.extend(improved_verification_controls)
                    else:
                        improved_path = Path(
                            self._dependencies.executor.execute_improved(
                                execution_plan,
                                execution.output_path,
                                issued.layout.private_root,
                                self._is_cancelled,
                                source_mappings=execution.source_mappings,
                                inherited_action_ids=execution.applied_action_ids,
                            )
                        )
                except RescueCancelledError:
                    _cleanup_verification_controls(
                        issued.layout.private_root,
                        tuple(execution.verification_controls),
                    )
                    raise
                except Exception:
                    improvement_failure = True
                    limitations.append(
                        "The supported improved-viewing candidate could not be "
                        "completed; the verified faithful output was retained."
                    )
            elif self._config.strategy is RescueStrategy.BALANCED:
                limitations.append(
                    "no supported improvement was selected; only a faithful rescue "
                    "was delivered"
                )
            else:
                limitations.append(
                    "Improved viewing output was not requested for this Conservative "
                    "run."
                )

            try:
                self._emit(RescueStatus.VERIFYING)
                public_mappings = _public_source_mappings(execution.source_mappings)
                failed_source_ranges = _complete_failed_source_ranges(
                    public_mappings,
                    issued.preparation.damage_map.duration_seconds,
                    execution.failed_source_ranges,
                )
                execution_is_partial = bool(failed_source_ranges)
                verification_kwargs: dict[str, Any] = {
                    "faithful_render_mode": execution.render_mode
                }
                if all_verification_controls:
                    verification_kwargs["verification_controls"] = tuple(
                        all_verification_controls
                    )
                self._check_cancelled()
                verification = self._dependencies.verifier.verify(
                    source,
                    execution.output_path,
                    improved_path,
                    execution_plan,
                    public_mappings,
                    self._is_cancelled,
                    **verification_kwargs,
                )
            finally:
                _cleanup_verification_controls(
                    issued.layout.private_root,
                    tuple(all_verification_controls),
                )
            self._check_cancelled()
            if improvement_failure:
                verification = _record_failed_improvement(verification)

            status = _result_status(verification, execution_is_partial)
            manual_review_reasons: list[str] = []
            for reason in (
                *(
                    warning.message
                    for warning in issued.preparation.assessments.warnings
                ),
                *execution_plan.assessment_warnings,
            ):
                if reason not in manual_review_reasons:
                    manual_review_reasons.append(reason)
            if manual_review_reasons:
                if status is RescueStatus.COMPLETED:
                    status = RescueStatus.NEEDS_REVIEW
            if execution_is_partial:
                manual_review_reasons.append(
                    "Some observed source intervals were not retained in the "
                    "faithful output."
                )
            if status is RescueStatus.NEEDS_REVIEW and improved_path is not None:
                manual_review_reasons.append(
                    "The improved candidate requires manual review before use."
                )
            report_outcome = (
                RescueOutcome.PARTIAL
                if execution_is_partial
                and verification.outcome is RescueOutcome.COMPLETED
                else verification.outcome
            )
            if (
                status is RescueStatus.NEEDS_REVIEW
                and report_outcome is RescueOutcome.COMPLETED
            ):
                report_outcome = RescueOutcome.NEEDS_REVIEW
            technical = RescueTechnicalReport(
                plan_digest=issued.preparation.plan.plan_digest,
                outcome=report_outcome,
                damage_map=issued.preparation.damage_map,
                verification=verification,
                requested_symptoms=execution_plan.requested_symptoms,
                assessment_parameters=execution_plan.assessment_parameters,
                assessment_limitations=execution_plan.assessment_limitations,
                assessment_warnings=execution_plan.assessment_warnings,
                artifacts=verification.artifacts,
                action_executions=_action_execution_ledger(
                    execution_plan,
                    improved_path=improved_path,
                    improvement_failure=improvement_failure,
                ),
                limitations=tuple(
                    [*limitations, *issued.preparation.assessments.limitations]
                ),
                manual_review_reasons=tuple(manual_review_reasons),
            )
            public_verification = project_public_rescue_verification(
                issued.preparation.plan,
                verification,
            )
            public_technical = technical.model_copy(
                update={
                    "verification": public_verification,
                    "artifacts": public_verification.artifacts,
                }
            )
            changes = RescueChangeLog(
                plan_digest=issued.preparation.plan.plan_digest,
                processor={"mode": "local_cpu"},
                actions=tuple(
                    action
                    for action, action_execution in zip(
                        execution_plan.actions, technical.action_executions
                    )
                    if action_execution.status is RescueActionExecutionStatus.SUCCEEDED
                ),
                action_executions=technical.action_executions,
                artifacts=public_verification.artifacts,
            )
            write_rescue_change_log_json(
                changes, issued.layout.private_root / "changes-private.json"
            )
            html = self._dependencies.report_renderer(
                issued.preparation.plan, public_technical, public_mappings
            )
            artifacts = self._dependencies.publisher(
                issued.layout,
                verification=verification,
                plan=issued.preparation.plan,
                mappings=public_mappings,
                damaged_ranges=failed_source_ranges,
                public_documents={
                    "changes.json": changes.model_dump(mode="json"),
                    "technical-report.json": public_technical.model_dump(mode="json"),
                    "report.html": html,
                },
                cancellation_callback=self._is_cancelled,
            )
            if not artifacts:
                self._check_cancelled()
                status = RescueStatus.FAILED
                result = RescueResult(
                    status,
                    None,
                    None,
                    technical,
                    None,
                    None,
                    public_mappings,
                    failed_source_ranges,
                )
            else:
                public = issued.layout.public_root
                published_names = {artifact.relative_path for artifact in artifacts}
                result = RescueResult(
                    status,
                    public / "faithful-rescue.mp4",
                    (
                        public / "improved-viewing.mp4"
                        if "improved-viewing.mp4" in published_names
                        else None
                    ),
                    technical,
                    public / "report.html",
                    public,
                    public_mappings,
                    failed_source_ranges,
                )
            self._emit_terminal(status)
            return result
        except RescueCancelledError:
            self._emit_terminal(RescueStatus.CANCELLED)
            raise
        except Exception:
            self._emit_terminal(RescueStatus.FAILED)
            raise
        finally:
            self._cleanup(issued.layout)
            self._release_issued(issued.preparation)

    def _call_scan(self, source: Path, source_hash: str) -> tuple[Any, MediaDamageMap]:
        try:
            metadata = self._dependencies.probe(source)
            damage_map = self._dependencies.scanner.scan(
                source, source_hash, metadata, RescueScanConfig()
            )
            return metadata, damage_map
        except RescueError:
            raise
        except Exception as exc:
            raise RescueScanError("local source scan failed") from exc

    def _require_issued(self, preparation: RescuePreparation | None) -> _Issued:
        if preparation is None:
            raise RescueConfirmationError("no Rescue preparation was issued")
        issued = self._issued.get(id(preparation))
        if issued is None or issued.preparation is not preparation:
            raise RescueConfirmationError("preparation was not issued by this pipeline")
        if preparation.plan is not issued.plan or not hmac.compare_digest(
            preparation.plan.plan_digest, issued.plan_digest
        ):
            raise RescueConfirmationError("prepared Rescue plan was altered")
        return issued

    def _release_issued(self, preparation: RescuePreparation) -> None:
        issued = self._issued.get(id(preparation))
        if issued is None or issued.preparation is not preparation:
            return
        del self._issued[id(preparation)]
        os.close(issued.source_descriptor)

    def _validate_confirmation(
        self, preparation: RescuePreparation, confirmation: RescueConfirmation
    ) -> None:
        if not hmac.compare_digest(
            confirmation.plan_digest, preparation.plan.plan_digest
        ):
            raise RescueConfirmationError("confirmation digest does not match")
        try:
            preparation.plan.validate_confirmation(confirmation)
        except ValueError as exc:
            raise RescueConfirmationError(
                "confirmation choices do not match plan"
            ) from exc
        confirmable_actions = {
            action.id
            for action in preparation.plan.actions
            if action.requires_confirmation
        }
        accepted_actions = set(confirmation.accepted_action_ids)
        if accepted_actions != confirmable_actions:
            raise RescueConfirmationError(
                "confirmation must accept the immutable previewed action set"
            )
        if (
            preparation.previews is not None
            and set(preparation.previews.previewed_action_ids) != confirmable_actions
        ):
            raise RescueConfirmationError(
                "confirmed actions were not all represented in the issued previews"
            )
        trim_damage_ids: set[str] = set()
        for action in preparation.plan.actions:
            if (
                action.kind is not RescueActionKind.TRIM_DAMAGED_EDGES
                or action.id not in accepted_actions
            ):
                continue
            values = action.parameters.get("damage_ids")
            if isinstance(values, list):
                trim_damage_ids.update(
                    value for value in values if isinstance(value, str)
                )
        if set(confirmation.accepted_trim_damage_ids) != trim_damage_ids:
            raise RescueConfirmationError(
                "confirmation must accept exactly the plan's trimmed damage IDs"
            )
        has_improvement = _has_supported_improvement(
            _execution_plan(preparation.plan, confirmation.accepted_action_ids)
        )
        if confirmation.publish_improved is not has_improvement:
            raise RescueConfirmationError(
                "confirmation improved-output choice does not match the plan"
            )

    def _emit(self, status: RescueStatus) -> None:
        if self._progress is not None:
            self._progress(status)

    def _emit_terminal(self, status: RescueStatus) -> None:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("terminal status required")
        if self._terminal_status is None:
            self._terminal_status = status
            self._emit(status)

    def _cleanup(self, layout: RescueArtifactLayout | None) -> None:
        if layout is not None and not self._config.keep_workspace:
            shutil.rmtree(layout.private_root, ignore_errors=True)

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise RescueCancelledError("Rescue was cancelled")


def _execution_plan(
    plan: RescuePlan, accepted_action_ids: tuple[str, ...]
) -> RescuePlan:
    """Return an in-memory execution view of one immutable signed plan.

    The original plan and digest remain the confirmation/publication contract.
    This view can only remove confirmable actions whose IDs were not accepted;
    non-confirmable safety and verification actions always remain present.
    """

    expected = {action.id for action in plan.actions if action.requires_confirmation}
    if set(accepted_action_ids) != expected:
        raise RescueConfirmationError(
            "execution requires the immutable previewed action set"
        )
    return plan


def _has_supported_improvement(plan: RescuePlan) -> bool:
    return plan.strategy is RescueStrategy.BALANCED and any(
        action.kind in REMAINING_IMPROVEMENT_ACTION_KINDS for action in plan.actions
    )


def _action_execution_ledger(
    plan: RescuePlan,
    *,
    improved_path: Path | None,
    improvement_failure: bool,
) -> tuple[RescueActionExecution, ...]:
    records: list[RescueActionExecution] = []
    for action in plan.actions:
        role: Literal["faithful", "improved", "document"]
        artifact_role = action_artifact_role(action.kind)
        if artifact_role == "faithful":
            status = RescueActionExecutionStatus.SUCCEEDED
            reason = None
            role = "faithful"
        elif artifact_role == "improved":
            if improvement_failure:
                status = RescueActionExecutionStatus.FAILED
                reason = "The improved candidate could not be completed."
            elif improved_path is None:
                status = RescueActionExecutionStatus.SKIPPED
                reason = "No improved candidate was rendered."
            else:
                status = RescueActionExecutionStatus.SUCCEEDED
                reason = None
            role = "improved"
        else:
            status = RescueActionExecutionStatus.SUCCEEDED
            reason = None
            role = "document" if action.kind is RescueActionKind.VERIFY else "faithful"
        records.append(
            RescueActionExecution(
                action_id=action.id,
                kind=action.kind,
                status=status,
                artifact_role=role,
                reason=reason,
            )
        )
    return tuple(records)


def _public_source_mappings(
    mappings: tuple[SourceMapping, ...],
) -> tuple[SourceMapping, ...]:
    """Replace private staging names with the one published faithful artifact."""
    return tuple(
        SourceMapping(
            source_start=mapping.source_start,
            source_end=mapping.source_end,
            output_start=mapping.output_start,
            output_end=mapping.output_end,
            output_relative_path="faithful-rescue.mp4",
        )
        for mapping in mappings
    )


def _complete_failed_source_ranges(
    mappings: tuple[SourceMapping, ...],
    source_duration: float,
    reported_failures: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """Include every source interval omitted by the verified faithful mapping."""
    missing = list(reported_failures)
    cursor = 0.0
    for mapping in sorted(
        mappings, key=lambda item: (item.source_start, item.source_end)
    ):
        if mapping.source_start > cursor + 1e-9:
            missing.append((cursor, min(mapping.source_start, source_duration)))
        cursor = max(cursor, min(mapping.source_end, source_duration))
    if cursor < source_duration - 1e-9:
        missing.append((cursor, source_duration))
    merged: list[tuple[float, float]] = []
    for start, end in sorted(missing):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 1e-9:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _record_failed_improvement(
    verification: RescueVerificationReport,
) -> RescueVerificationReport:
    if verification.improved_status is not None:
        return verification
    failed_checks = tuple(
        RescueVerificationCheck(
            check_id=check_id,
            artifact="improved",
            status=RescueVerificationStatus.FAILED,
            message="The improved candidate was not completed for local verification.",
            measured={"completed": False},
        )
        for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    )
    return RescueVerificationReport(
        plan_digest=verification.plan_digest,
        faithful_status=verification.faithful_status,
        improved_status=RescueVerificationStatus.FAILED,
        checks=(*verification.checks, *failed_checks),
        artifacts=verification.artifacts,
        outcome=RescueOutcome.PARTIAL,
    )


def _result_status(
    verification: RescueVerificationReport,
    execution_is_partial: bool,
) -> RescueStatus:
    if verification.faithful_status is RescueVerificationStatus.FAILED:
        return RescueStatus.FAILED
    if verification.outcome is RescueOutcome.NEEDS_REVIEW:
        return RescueStatus.NEEDS_REVIEW
    if verification.outcome is RescueOutcome.FAILED:
        return RescueStatus.FAILED
    if execution_is_partial or verification.outcome is RescueOutcome.PARTIAL:
        return RescueStatus.PARTIAL
    return RescueStatus.COMPLETED


def _mark_requested_audio_evidence_gaps(
    assessments: RescueAssessmentBundle,
    symptoms: tuple[RescueSymptom, ...],
    damage_map: MediaDamageMap,
) -> RescueAssessmentBundle:
    warnings = list(assessments.warnings)
    observed_kinds = {interval.kind for interval in damage_map.intervals}
    if (
        RescueSymptom.AUDIO_VIDEO_OFFSET in symptoms
        or DamageKind.FIXED_AV_OFFSET in observed_kinds
    ) and (
        assessments.fixed_offset_assessment is None
        or assessments.fixed_offset_assessment.offset_seconds is None
    ):
        warnings.append(
            RescueAssessmentWarning(
                component="sync",
                error_type="UnavailableEvidence",
                message=(
                    "Requested fixed A/V offset evidence was unavailable; no "
                    "automatic timing correction was inferred."
                ),
            )
        )
    measurement = (
        assessments.audio_assessment.measurement
        if assessments.audio_assessment is not None
        else None
    )
    if (
        RescueSymptom.AUDIO_NOISE in symptoms
        or DamageKind.AUDIO_NOISE in observed_kinds
    ) and (measurement is None or measurement.noise_floor_dbfs is None):
        warnings.append(
            RescueAssessmentWarning(
                component="audio_noise",
                error_type="UnavailableEvidence",
                message=(
                    "Requested native audio-noise evidence was unavailable; no "
                    "automatic denoise action was inferred."
                ),
            )
        )
    if tuple(warnings) == assessments.warnings:
        return assessments
    return assessments.model_copy(update={"warnings": tuple(warnings)})


__all__ = [
    "RescueConfig",
    "RescuePipelineDependencies",
    "RescuePreparation",
    "RescueResult",
    "RescueStatus",
    "VideoRescuePipeline",
]
