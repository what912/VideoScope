"""Structured errors exposed by the analysis pipeline and CLI."""

from __future__ import annotations


class AnalysisError(RuntimeError):
    """Base class for a failed analysis operation."""

    exit_code = 4


class AnalysisInputError(AnalysisError):
    """The user supplied an invalid input path or analysis selection."""

    exit_code = 2


class AnalysisConfigError(AnalysisInputError):
    """A JSON configuration file or detector selection is invalid."""


class AnalysisProcessingError(AnalysisError):
    """The local media stack could not process the input."""

    exit_code = 3


class AnalysisInternalError(AnalysisError):
    """An unexpected pipeline or artifact failure occurred."""

    exit_code = 4


class AnalysisCancelledError(AnalysisError):
    """The caller requested cancellation of an in-progress analysis."""

    exit_code = 130
