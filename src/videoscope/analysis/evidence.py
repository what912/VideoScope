"""Deterministic evidence selection and output materialization."""

from __future__ import annotations

import shutil
from pathlib import Path

from videoscope.domain import Evidence, Finding
from videoscope.video import FrameSample

EVIDENCE_FRAME_COUNT = 3


class EvidenceManager:
    """Select front/middle/final samples and copy them into report artifacts."""

    def __init__(
        self,
        *,
        workspace: Path,
        output_directory: Path,
        frame_samples: tuple[FrameSample, ...],
    ) -> None:
        self.workspace = workspace.resolve()
        self.output_directory = output_directory
        self.frame_samples = frame_samples

    def materialize(self, findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
        """Copy deterministic evidence and return Findings with relative paths."""
        evidence_directory = self.output_directory / "evidence"
        evidence_directory.mkdir(parents=True, exist_ok=True)
        return tuple(self._materialize_finding(finding) for finding in findings)

    def _materialize_finding(self, finding: Finding) -> Finding:
        selected = self._select_samples(finding)
        evidence: list[Evidence] = []
        for ordinal, sample in enumerate(selected):
            source = self._resolve_source(sample)
            suffix = source.suffix.lower() or ".jpg"
            filename = f"{finding.id}_{ordinal:02d}{suffix}"
            destination = self.output_directory / "evidence" / filename
            shutil.copy2(source, destination)
            original = min(
                finding.evidence,
                key=lambda item: (
                    abs(item.timestamp_seconds - sample.timestamp_seconds),
                    item.timestamp_seconds,
                ),
            )
            evidence.append(
                Evidence(
                    evidence_type=original.evidence_type,
                    timestamp_seconds=sample.timestamp_seconds,
                    relative_path=(Path("evidence") / filename).as_posix(),
                    description=original.description,
                    metadata=original.metadata,
                )
            )
        return Finding.model_validate(
            {
                **finding.model_dump(mode="python"),
                "evidence": evidence,
            }
        )

    def _select_samples(self, finding: Finding) -> tuple[FrameSample, ...]:
        start = finding.time_range.start_seconds
        end = finding.time_range.end_seconds
        midpoint = start + (end - start) / 2.0
        eligible = tuple(
            sample
            for sample in self.frame_samples
            if start <= sample.timestamp_seconds <= end
        )
        candidates = eligible or self.frame_samples
        selected: list[FrameSample] = []
        for evidence in finding.evidence:
            if not candidates:
                break
            sample = min(
                candidates,
                key=lambda item: (
                    abs(item.timestamp_seconds - evidence.timestamp_seconds),
                    item.timestamp_seconds,
                    item.sample_index,
                ),
            )
            if sample.sample_index not in {item.sample_index for item in selected}:
                selected.append(sample)
            if len(selected) == EVIDENCE_FRAME_COUNT:
                break
        for target in (start, midpoint, end):
            if not candidates:
                break
            sample = min(
                candidates,
                key=lambda item: (
                    abs(item.timestamp_seconds - target),
                    item.timestamp_seconds,
                    item.sample_index,
                ),
            )
            if sample.sample_index not in {item.sample_index for item in selected}:
                selected.append(sample)
            if len(selected) == EVIDENCE_FRAME_COUNT:
                break
        if len(selected) < EVIDENCE_FRAME_COUNT:
            for sample in candidates:
                if sample.sample_index not in {item.sample_index for item in selected}:
                    selected.append(sample)
                if len(selected) == EVIDENCE_FRAME_COUNT:
                    break
        return tuple(
            sorted(
                selected,
                key=lambda sample: (
                    sample.timestamp_seconds,
                    sample.sample_index,
                ),
            )
        )

    def _resolve_source(self, sample: FrameSample) -> Path:
        relative_path = Path(sample.relative_path)
        if relative_path.is_absolute():
            raise ValueError("Frame sample evidence path must be relative")
        source = (self.workspace / relative_path).resolve()
        if not source.is_relative_to(self.workspace):
            raise ValueError("Frame sample evidence path escapes workspace")
        if not source.is_file():
            raise FileNotFoundError("Selected evidence frame is unavailable")
        return source
