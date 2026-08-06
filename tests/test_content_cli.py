"""CLI adapter tests for the shared useful-content lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from typer.testing import CliRunner

from videoscope.cli import app
from videoscope.content import (
    ContentConfirmation,
    ContentGoal,
    ContentInputError,
    ContentPipelineConfig,
    ContentResult,
    ContentStatus,
)

runner = CliRunner()


@dataclass(frozen=True)
class _FakeAction:
    id: str
    changes_content: bool
    requires_confirmation: bool
    source_ranges: tuple[object, ...] = ()
    description: str = "Retain source timeline."


@dataclass(frozen=True)
class _FakePlan:
    goal: ContentGoal = ContentGoal.FAITHFUL_CLEAN
    plan_digest: str = "a" * 64
    actions: tuple[_FakeAction, ...] = ()
    storyboard: object = field(
        default_factory=lambda: SimpleNamespace(estimated_output_duration_seconds=10.0)
    )


@dataclass(frozen=True)
class _FakeReview:
    plan: _FakePlan
    previews: tuple[object, ...] = ()


class _FakeContentPipeline:
    instances: ClassVar[list[_FakeContentPipeline]] = []
    content_changes: ClassVar[bool] = False
    result_status: ClassVar[ContentStatus] = ContentStatus.COMPLETED
    prepare_error: ClassVar[Exception | None] = None

    def __init__(
        self,
        config: ContentPipelineConfig,
        *,
        progress: object | None = None,
    ) -> None:
        del progress
        self.config = config
        self.confirmed_ids: tuple[str, ...] | None = None
        self.closed = False
        self.cancelled = False
        type(self).instances.append(self)

    def prepare(self, input_path: Path) -> object:
        del input_path
        if self.prepare_error is not None:
            raise self.prepare_error
        return object()

    def preview(self, preparation: object) -> _FakeReview:
        del preparation
        actions = (
            (_FakeAction("action_" + "1" * 64, True, True),)
            if self.content_changes
            else ()
        )
        return _FakeReview(_FakePlan(goal=self.config.content.goal, actions=actions))

    def confirm(
        self,
        review: _FakeReview,
        *,
        accepted_action_ids: tuple[str, ...],
    ) -> ContentConfirmation:
        del review
        self.confirmed_ids = accepted_action_ids
        return cast(ContentConfirmation, object())

    def execute(
        self,
        review: _FakeReview,
        confirmation: ContentConfirmation,
    ) -> ContentResult:
        del review, confirmation
        return cast(
            ContentResult,
            SimpleNamespace(
                status=self.result_status,
                public_root=Path("content-output"),
            ),
        )

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        self.closed = True


def _install_fake_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeContentPipeline.instances = []
    _FakeContentPipeline.content_changes = False
    _FakeContentPipeline.result_status = ContentStatus.COMPLETED
    _FakeContentPipeline.prepare_error = None
    monkeypatch.setattr(
        "videoscope.cli.LongVideoContentPipeline",
        _FakeContentPipeline,
    )
    monkeypatch.setattr("videoscope.cli.compute_file_sha256", lambda _path: "b" * 64)


def _args(source: Path, output: Path, *extra: str) -> list[str]:
    return ["content", str(source), "--output", str(output), *extra]


def test_help_lists_content_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "content" in result.stdout


def test_content_parses_all_bounded_local_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_pipeline(monkeypatch)
    source = tmp_path / "中文 source.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        _args(
            source,
            tmp_path / "输出",
            "--goal",
            "selected_clips",
            "--transcript",
            str(tmp_path / "字幕.srt"),
            "--keep-range",
            "1:2:重点",
            "--exclude-range",
            "3:4",
            "--locked-keep-range",
            "5:6",
            "--locked-exclude-range",
            "7:8",
            "--chapter",
            "0:5:开场",
            "--target-duration",
            "9",
            "--keep-workspace",
            "--json-only",
            "--export-subtitles",
            "--export-clips",
            "--quiet",
        ),
    )

    assert result.exit_code == 0
    instance = _FakeContentPipeline.instances[0]
    assert instance.config.content.goal is ContentGoal.SELECTED_CLIPS
    assert instance.config.content.target_duration_seconds == 9
    assert instance.config.content.generate_html_report is False
    assert instance.config.content.export_subtitles is True
    assert instance.config.content.export_clips is True
    assert len(instance.config.user_ranges) == 5
    assert instance.config.keep_workspace is True
    assert instance.closed is True


def test_noninteractive_content_change_requires_matching_review_and_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_pipeline(monkeypatch)
    _FakeContentPipeline.content_changes = True
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: False)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    missing = runner.invoke(
        app,
        _args(source, tmp_path / "missing", "--yes", "--quiet"),
    )
    reviewed = tmp_path / "review.json"
    reviewed.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "videoscope.cli.read_content_plan_json",
        lambda _path: _FakePlan(plan_digest="f" * 64),
    )
    mismatch = runner.invoke(
        app,
        _args(
            source,
            tmp_path / "mismatch",
            "--yes",
            "--reviewed-plan",
            str(reviewed),
            "--quiet",
        ),
    )

    assert missing.exit_code == 2
    assert mismatch.exit_code == 2
    assert all(item.confirmed_ids is None for item in _FakeContentPipeline.instances)


def test_matching_review_executes_exact_action_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_pipeline(monkeypatch)
    _FakeContentPipeline.content_changes = True
    reviewed = tmp_path / "review.json"
    reviewed.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "videoscope.cli.read_content_plan_json",
        lambda _path: _FakePlan(),
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    result = runner.invoke(
        app,
        _args(
            source,
            tmp_path / "out",
            "--yes",
            "--reviewed-plan",
            str(reviewed),
            "--quiet",
        ),
    )

    assert result.exit_code == 0
    assert _FakeContentPipeline.instances[0].confirmed_ids == ("action_" + "1" * 64,)


def test_interactive_confirmation_accepts_or_rejects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_pipeline(monkeypatch)
    _FakeContentPipeline.content_changes = True
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: True)
    monkeypatch.setattr("videoscope.cli._render_content_review", lambda _review: None)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    accepted = runner.invoke(
        app,
        _args(source, tmp_path / "accepted", "--quiet"),
        input="y\n",
    )
    rejected = runner.invoke(
        app,
        _args(source, tmp_path / "rejected", "--quiet"),
        input="n\n",
    )

    assert accepted.exit_code == 0
    assert rejected.exit_code == 2
    assert _FakeContentPipeline.instances[0].confirmed_ids is not None
    assert _FakeContentPipeline.instances[1].confirmed_ids is None
    assert all(item.closed for item in _FakeContentPipeline.instances)


def test_content_exit_codes_are_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_pipeline(monkeypatch)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    _FakeContentPipeline.result_status = ContentStatus.NEEDS_REVIEW
    review = runner.invoke(
        app,
        _args(source, tmp_path / "review", "--quiet"),
    )
    _FakeContentPipeline.prepare_error = ContentInputError("injected")
    invalid = runner.invoke(
        app,
        _args(source, tmp_path / "invalid", "--quiet"),
    )

    assert review.exit_code == 5
    assert invalid.exit_code == 2

    _FakeContentPipeline.prepare_error = None
    _FakeContentPipeline.result_status = ContentStatus.FAILED
    verification = runner.invoke(
        app,
        _args(source, tmp_path / "verification", "--quiet"),
    )
    assert verification.exit_code == 5
