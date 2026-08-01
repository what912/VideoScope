"""Structured, privacy-conscious errors for local video operations."""

from __future__ import annotations

from pathlib import Path

MAX_DIAGNOSTIC_LENGTH = 2000
REDACTED_PATH = "<input>"


def sanitize_diagnostic(
    text: str | None,
    *,
    sensitive_paths: tuple[Path, ...] = (),
) -> str:
    """Return a bounded diagnostic with known absolute paths removed."""
    diagnostic = (text or "").strip()
    replacements: set[str] = set()
    for path in sensitive_paths:
        candidates = (str(path), path.as_posix())
        replacements.update(candidate for candidate in candidates if candidate)
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        replacements.update((str(resolved), resolved.as_posix()))

    for candidate in sorted(replacements, key=len, reverse=True):
        diagnostic = diagnostic.replace(candidate, REDACTED_PATH)

    if not diagnostic:
        return "no diagnostic output"
    if len(diagnostic) > MAX_DIAGNOSTIC_LENGTH:
        return f"{diagnostic[:MAX_DIAGNOSTIC_LENGTH]}..."
    return diagnostic


class VideoProcessingError(RuntimeError):
    """Base class exposing a stable machine-readable error code."""

    code = "video_processing_error"

    def __init__(
        self,
        message: str,
        *,
        stderr_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr_summary = stderr_summary


class VideoNotFoundError(VideoProcessingError):
    """The requested local input file does not exist."""

    code = "video_not_found"


class ExternalToolNotFoundError(VideoProcessingError):
    """A required FFmpeg executable could not be started."""

    code = "external_tool_not_found"

    def __init__(
        self,
        message: str,
        *,
        work_directory: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.work_directory = work_directory


class VideoProbeError(VideoProcessingError):
    """ffprobe could not return usable normalized metadata."""

    code = "video_probe_error"


class VideoDecodeError(VideoProbeError):
    """ffprobe rejected or could not decode the supplied media."""

    code = "video_decode_error"


class NoVideoStreamError(VideoProbeError):
    """The media contains no usable video stream."""

    code = "no_video_stream"


class VideoHashError(VideoProcessingError):
    """The input could not be read while hashing."""

    code = "video_hash_error"


class FrameSamplingError(VideoProcessingError):
    """FFmpeg could not produce a valid set of sampled frames."""

    code = "frame_sampling_error"

    def __init__(
        self,
        message: str,
        *,
        work_directory: Path,
        stderr_summary: str | None = None,
    ) -> None:
        super().__init__(message, stderr_summary=stderr_summary)
        self.work_directory = work_directory
