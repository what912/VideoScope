"""Sanitized errors for the opt-in local Video Rescue workflow."""

from __future__ import annotations


class RescueError(Exception):
    """Base rescue error whose public text does not expose local diagnostics."""

    exit_code = 4
    public_message = "Video Rescue could not complete."

    def __init__(self, internal_message: str | None = None) -> None:
        super().__init__(self.public_message)
        self.internal_message = internal_message


class RescueInputError(RescueError):
    """The supplied local input or configuration is invalid."""

    exit_code = 2
    public_message = "The Video Rescue input or configuration is not valid."


class RescueScanError(RescueError):
    """A local scan could not establish the media damage map."""

    public_message = "Video Rescue could not scan the selected media locally."


class RescuePlanError(RescueInputError):
    """The observed media cannot form a valid rescue plan."""

    public_message = "The observed media cannot form a Video Rescue plan."


class RescueConfirmationError(RescueInputError):
    """The submitted confirmation does not bind to the current plan."""

    public_message = "Video Rescue confirmation does not match the current plan."


class RescueMediaError(RescueError):
    """Local media processing failed without exposing command diagnostics."""

    exit_code = 3
    public_message = "The selected media could not be processed locally."


class RescueArtifactError(RescueError):
    """A Rescue artifact could not be safely written or verified."""

    public_message = "A Video Rescue artifact could not be handled safely."


class RescueQualificationUnavailableError(RescueError):
    """An optional local qualification provider is explicitly unavailable."""

    public_message = "Optional local Video Rescue qualification was unavailable."


class RescueCancelledError(RescueError):
    """The user cancelled Video Rescue before completion."""

    exit_code = 130
    public_message = "Video Rescue was cancelled."
