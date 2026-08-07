"""Structured errors for later Publish Ready orchestration layers."""

from __future__ import annotations


class ResolveError(Exception):
    """Base structured error raised by the Publish Ready workflow."""

    exit_code = 4


class PublishInputError(ResolveError):
    """Input, profile, configuration, or confirmation error (exit code 2)."""

    exit_code = 2


class PublishConfirmationError(PublishInputError):
    """A supplied confirmation is missing or does not match the plan digest."""


class PublishMediaError(ResolveError):
    """FFmpeg or ffprobe could not process the requested media (exit code 3)."""

    exit_code = 3

    def __init__(
        self,
        message: str,
        *,
        stderr_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr_summary = stderr_summary


class PublishArtifactError(ResolveError):
    """An internal orchestration or public artifact operation failed."""


class PublishCancelledError(ResolveError):
    """The user cancelled processing before it completed."""

    exit_code = 130
