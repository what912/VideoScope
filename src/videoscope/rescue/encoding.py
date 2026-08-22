"""One confirmation-bound video encoding contract for every Rescue variant."""

from __future__ import annotations

from fractions import Fraction

from videoscope.rescue.models import (
    RescueEffectiveConfig,
    canonical_video_encode_contract,
)


def canonical_video_encode_arguments(
    config: RescueEffectiveConfig,
    *,
    stream_index: int = 0,
    frame_rate: str | None = None,
    crf_override: int | None = None,
) -> tuple[str, ...]:
    """Return the shared H.264 topology and deterministic encoder arguments."""
    if (
        isinstance(stream_index, bool)
        or not isinstance(stream_index, int)
        or stream_index < 0
    ):
        raise ValueError("video stream index must be a non-negative integer")
    if frame_rate is not None:
        try:
            parsed_frame_rate = Fraction(frame_rate)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise ValueError("explicit video frame rate must be a rational") from exc
        if (
            parsed_frame_rate <= 0
            or parsed_frame_rate.numerator > 1_000_000
            or parsed_frame_rate.denominator > 1_000_000
        ):
            raise ValueError("explicit video frame rate is outside safe bounds")
    if crf_override is not None and (
        isinstance(crf_override, bool)
        or not isinstance(crf_override, int)
        or not 1 <= crf_override <= 30
    ):
        raise ValueError("video CRF override is outside safe High-profile bounds")
    contract = canonical_video_encode_contract(config)
    crf = contract.crf if crf_override is None else crf_override
    suffix = f":v:{stream_index}"
    arguments: tuple[str, ...] = (
        f"-c{suffix}",
        contract.encoder,
        f"-preset{suffix}",
        contract.preset,
        f"-crf{suffix}",
        str(crf),
        f"-pix_fmt{suffix}",
        contract.pixel_format,
        f"-profile{suffix}",
        contract.profile,
        f"-level{suffix}",
        contract.level,
        f"-g{suffix}",
        str(contract.gop_size),
        f"-keyint_min{suffix}",
        str(contract.minimum_keyframe_interval),
        f"-bf{suffix}",
        str(contract.b_frames),
        f"-refs{suffix}",
        str(contract.reference_frames),
        f"-sc_threshold{suffix}",
        str(contract.scene_change_threshold),
    )
    if frame_rate is not None:
        arguments = (*arguments, f"-r{suffix}", frame_rate)
    return (
        *arguments,
        f"-fps_mode{suffix}",
        contract.fps_mode,
        "-video_track_timescale",
        str(contract.track_timescale),
    )


__all__ = ["canonical_video_encode_arguments"]
