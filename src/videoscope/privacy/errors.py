"""Sanitized errors for the opt-in Safe Sharing workflow."""

from __future__ import annotations


class PrivacyError(Exception):
    """Base privacy error whose public message never exposes internal details."""

    exit_code = 4
    public_message = "Safe Sharing could not complete."

    def __init__(self, internal_message: str | None = None) -> None:
        super().__init__(self.public_message)
        self.internal_message = internal_message


class PrivacyInputError(PrivacyError):
    """The supplied input, profile, or configuration is not valid."""

    exit_code = 2
    public_message = "The Safe Sharing input or configuration is not valid."


class PrivacyArtifactError(PrivacyError):
    """A Safe Sharing artifact could not be safely read or written."""

    public_message = "A Safe Sharing artifact could not be handled safely."


class PrivacyPlanError(PrivacyInputError):
    """The reviewed risks cannot form a safe redaction plan."""

    public_message = "The reviewed risks cannot form a Safe Sharing plan."


class PrivacyConfirmationError(PrivacyInputError):
    """The confirmation does not match the exact reviewed plan."""

    public_message = "Safe Sharing confirmation does not match the current plan."


class PrivacyMediaError(PrivacyError):
    """Local media processing failed without exposing tool diagnostics publicly."""

    exit_code = 3
    public_message = "The selected media could not be processed locally."


class PrivacyCancelledError(PrivacyError):
    """The user cancelled Safe Sharing before it completed."""

    exit_code = 130
    public_message = "Safe Sharing was cancelled."
