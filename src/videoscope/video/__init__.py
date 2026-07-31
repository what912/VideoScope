"""Video probing, hashing, and deterministic frame sampling."""

from videoscope.video.errors import (
    ExternalToolNotFoundError,
    FrameSamplingError,
    NoVideoStreamError,
    VideoDecodeError,
    VideoHashError,
    VideoNotFoundError,
    VideoProbeError,
    VideoProcessingError,
)
from videoscope.video.hashing import compute_file_sha256
from videoscope.video.probe import metadata_from_ffprobe, parse_frame_rate, probe_video
from videoscope.video.sampling import (
    FrameSample,
    FrameSamplingResult,
    build_sampling_filter,
    sample_frames,
)

__all__ = [
    "ExternalToolNotFoundError",
    "FrameSample",
    "FrameSamplingError",
    "FrameSamplingResult",
    "NoVideoStreamError",
    "VideoDecodeError",
    "VideoHashError",
    "VideoNotFoundError",
    "VideoProbeError",
    "VideoProcessingError",
    "build_sampling_filter",
    "compute_file_sha256",
    "metadata_from_ffprobe",
    "parse_frame_rate",
    "probe_video",
    "sample_frames",
]
