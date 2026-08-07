"""Deterministic planning for safe Publish Ready transformations."""

from __future__ import annotations

import json
from hashlib import sha256

from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.resolve.models import (
    EXPECTED_PUBLISH_ARTIFACTS,
    PUBLISH_PREVIEW_ARTIFACT,
    PublishAction,
    PublishActionKind,
    PublishBackend,
    PublishEffectiveConfig,
    PublishPlan,
    PublishProfileId,
    make_publish_plan_digest,
)
from videoscope.resolve.profiles import PublishProfile, get_publish_profile

_OUTPUT_FILENAME = "publish-ready.mp4"
_PUBLIC_RAW_PROBE_KEYS = frozenset(
    {
        "audio_codec",
        "color_range",
        "color_space",
        "format_long_name",
        "format_name",
        "pixel_format",
        "video_stream_index",
    }
)


def build_publish_plan(
    metadata: VideoMetadata,
    input_hash: str,
    profile_id: PublishProfileId,
    *,
    preview_seconds: float = 6.0,
    keep_workspace: bool = False,
    run_diagnostics: bool = True,
) -> PublishPlan:
    """Return a planning-only, path-free safe-action plan for one profile."""
    profile = get_publish_profile(profile_id)
    actions = _build_actions(metadata, profile)
    backend = PublishBackend.NATIVE_LOCAL
    effective_config = PublishEffectiveConfig(
        preview_seconds=preview_seconds,
        keep_workspace=keep_workspace,
        run_diagnostics=run_diagnostics,
    )
    task_id = _make_task_id(
        input_hash=input_hash,
        profile_id=profile.id,
        profile_version=profile.version,
        effective_config=effective_config,
    )
    plan_digest = make_publish_plan_digest(
        task_id=task_id,
        input_hash=input_hash,
        source_read_only=True,
        profile_id=profile.id,
        profile_version=profile.version,
        backend=backend,
        actions=actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=effective_config,
        output_filename=_OUTPUT_FILENAME,
    )
    return PublishPlan(
        task_id=task_id,
        input_hash=input_hash,
        source_metadata=_public_source_metadata(metadata),
        source_read_only=True,
        profile_id=profile.id,
        profile_version=profile.version,
        backend=backend,
        actions=actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=effective_config,
        output_filename=_OUTPUT_FILENAME,
        plan_digest=plan_digest,
    )


def _make_task_id(
    *,
    input_hash: str,
    profile_id: PublishProfileId,
    profile_version: str,
    effective_config: PublishEffectiveConfig,
) -> str:
    """Return a deterministic, path-free identity for one effective task."""
    payload = {
        "effective_config": effective_config.model_dump(mode="json"),
        "input_hash": input_hash,
        "profile_id": profile_id.value,
        "profile_version": profile_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _build_actions(
    metadata: VideoMetadata,
    profile: PublishProfile,
) -> tuple[PublishAction, ...]:
    actions: list[PublishAction] = []
    if _can_remux(metadata, profile):
        actions.append(
            _action(
                kind=PublishActionKind.REMUX,
                description="Repackage compatible streams in an MP4 container.",
                parameters={"container": profile.container},
                affects=("container",),
            )
        )
    else:
        transcode_parameters: dict[str, JsonValue] = {
            "container": profile.container,
            "video_codec": profile.video_codec,
            "pixel_format": profile.pixel_format,
            "maximum_fps": profile.maximum_fps,
        }
        transcode_affects = ["video"]
        if metadata.has_audio:
            transcode_parameters["audio_codec"] = profile.audio_codec
            transcode_affects.append("audio")
        actions.append(
            _action(
                kind=PublishActionKind.TRANSCODE,
                description="Encode streams to the selected compatibility profile.",
                parameters=transcode_parameters,
                affects=tuple(transcode_affects),
            )
        )

    if profile.width is not None and profile.height is not None:
        actions.append(
            _action(
                kind=PublishActionKind.SCALE_PAD,
                description="Fit the complete source image and pad the target canvas.",
                parameters={
                    "width": profile.width,
                    "height": profile.height,
                    "mode": "fit",
                    "pad_color": "black",
                },
                affects=("video_canvas",),
            )
        )

    actions.extend(
        (
            _action(
                kind=PublishActionKind.STRIP_METADATA,
                description="Remove nonessential source metadata from the output.",
                parameters={},
                affects=("metadata",),
            ),
            _action(
                kind=PublishActionKind.FASTSTART,
                description="Place MP4 playback metadata at the start of the file.",
                parameters={},
                affects=("container_layout",),
            ),
            _action(
                kind=PublishActionKind.EXTRACT_COVER,
                description="Extract one representative cover image.",
                parameters={},
                affects=("cover",),
            ),
        )
    )
    return tuple(actions)


def _can_remux(metadata: VideoMetadata, profile: PublishProfile) -> bool:
    container_names = {
        name.strip().casefold()
        for name in metadata.container_format.split(",")
        if name.strip()
    }
    pixel_format = metadata.raw_probe.get("pixel_format")
    audio_codec = metadata.raw_probe.get("audio_codec")
    return (
        profile.id is PublishProfileId.COMPATIBLE_MP4
        and profile.container.casefold() in container_names
        and metadata.codec.casefold() == profile.video_codec.casefold()
        and (
            not metadata.has_audio
            or (
                isinstance(audio_codec, str)
                and audio_codec.casefold() == profile.audio_codec.casefold()
            )
        )
        and isinstance(pixel_format, str)
        and pixel_format.casefold() == profile.pixel_format.casefold()
        and metadata.average_frame_rate <= profile.maximum_fps
    )


def _action(
    *,
    kind: PublishActionKind,
    description: str,
    parameters: dict[str, JsonValue],
    affects: tuple[str, ...],
) -> PublishAction:
    return PublishAction(
        action_id=kind.value,
        kind=kind,
        description=description,
        parameters=parameters,
        affects=affects,
        changes_content_semantics=False,
        confirmation_required=False,
    )


def _public_source_metadata(metadata: VideoMetadata) -> VideoMetadata:
    """Remove the source filename and keep only sanctioned probe summary keys."""
    raw_probe = {
        key: value
        for key, value in metadata.raw_probe.items()
        if key in _PUBLIC_RAW_PROBE_KEYS
    }
    return metadata.model_copy(
        update={
            "filename": "source",
            "raw_probe": raw_probe,
        }
    )


__all__ = ["build_publish_plan"]
