"""Sanitized errors for Long Video to Useful Content."""

from __future__ import annotations


class ContentError(Exception):
    """Base error whose public message is safe to show outside the workspace."""

    exit_code = 4
    public_message = "Long Video to Useful Content could not complete."

    def __init__(self, internal_message: str | None = None) -> None:
        super().__init__(self.public_message)
        self.internal_message = internal_message


class ContentInputError(ContentError):
    """The local media, transcript, selection, or configuration is invalid."""

    exit_code = 2
    public_message = "The useful-content input or configuration is not valid."


class ContentTranscriptError(ContentInputError):
    """A supplied local timed transcript cannot be used safely."""

    public_message = "The local timed transcript is not valid."


class ContentMappingError(ContentError):
    """The local structural evidence could not form a valid content map."""

    public_message = "A valid local content map could not be created."


class ContentPlanError(ContentInputError):
    """The content map and user decisions cannot form a valid plan."""

    public_message = "The current decisions cannot form a useful-content plan."


class ContentPreviewError(ContentError):
    """A bounded private preview could not be produced or validated."""

    exit_code = 3
    public_message = "A required local join preview could not be created."


class ContentConfirmationError(ContentInputError):
    """A confirmation does not bind to the current immutable plan."""

    public_message = "The useful-content confirmation does not match the plan."


class ContentMediaError(ContentError):
    """Native local media processing failed."""

    exit_code = 3
    public_message = "The selected media could not be processed locally."


class ContentVerificationError(ContentError):
    """The staged media did not satisfy the independent verification policy."""

    exit_code = 5
    public_message = "The useful-content result did not pass required verification."


class ContentArtifactError(ContentError):
    """A private or public artifact could not be handled safely."""

    public_message = "A useful-content artifact could not be handled safely."


class ContentArtifactLinkError(ContentArtifactError):
    """A filesystem link appeared where an owned artifact was required."""

    public_message = "A useful-content artifact link could not be handled safely."


class ContentCancelledError(ContentError):
    """The user cancelled the local workflow."""

    exit_code = 130
    public_message = "Long Video to Useful Content was cancelled."
