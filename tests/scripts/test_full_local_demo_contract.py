from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.full_local_demo_contract import (
    DemoContract,
    canonical_json_bytes,
    load_demo_contract,
    safe_relative_path,
    stream_sha256,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    REPOSITORY_ROOT / "demos" / "full-local-four-mode" / "demo-contract.json"
)
COMPOSITION_PATH = REPOSITORY_ROOT / "demos" / "full-local-four-mode" / "index.html"
EXPECTED_SCENE_IDS = (
    "clean_hook",
    "rescue_evidence",
    "useful_tutorial",
    "low_information",
    "privacy_zone",
    "motion_retake",
    "verified_ending",
)

GSAP_VERSION = "3.15.0"
GSAP_SHA256 = "92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb"


def test_composition_is_offline_deterministic_and_registered() -> None:
    html = COMPOSITION_PATH.read_text(encoding="utf-8")
    assert 'data-composition-id="videoscope-full-local-demo"' in html
    assert 'data-duration="42"' in html
    assert 'data-fps="24"' in html
    assert html.count('data-scene-id="') == 7
    assert "window.__timelines" in html
    assert "paused: true" in html
    for banned in ("http://", "https://", "Math.random", "Date.now", "repeat: -1"):
        assert banned not in html


def test_composition_embeds_verified_offline_gsap_before_application_script() -> None:
    html = COMPOSITION_PATH.read_text(encoding="utf-8")
    match = re.search(
        r'<script data-gsap-version="3\.15\.0" '
        r'data-integrity-sha256="([0-9a-f]{64})" '
        r'src="data:text/javascript;base64,([A-Za-z0-9+/=]+)"></script>',
        html,
    )

    assert match is not None
    assert match.group(1) == GSAP_SHA256
    payload = base64.b64decode(match.group(2), validate=True)
    assert hashlib.sha256(payload).hexdigest() == GSAP_SHA256
    assert payload.startswith(b"/*!\n * GSAP 3.15.0\n")
    assert html.index(match.group(0)) < html.index("const SCENE_COPY")


def test_all_scenes_have_transition_and_content_animation() -> None:
    html = COMPOSITION_PATH.read_text(encoding="utf-8")
    for scene_id in EXPECTED_SCENE_IDS:
        assert f'data-scene-id="{scene_id}"' in html
        assert f'animateScene("{scene_id}"' in html
    assert html.count('data-transition-id="') == 6
    assert "transitionBetween(" in html


def test_contract_has_exact_timeline_and_privacy_ranges() -> None:
    contract = load_demo_contract(CONTRACT_PATH)

    assert contract.duration_seconds == 42.0
    assert contract.frame_rate == 24
    assert [(scene.start_seconds, scene.end_seconds) for scene in contract.scenes] == [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 20.0),
        (20.0, 25.0),
        (25.0, 32.0),
        (32.0, 36.0),
        (36.0, 42.0),
    ]
    assert contract.privacy.start_seconds == 25.0
    assert contract.privacy.end_seconds == 32.0
    assert contract.privacy.box == (0.58, 0.18, 0.94, 0.78)
    assert contract.useful_keep_ranges == ((0.0, 5.0), (10.0, 20.0), (36.0, 42.0))
    assert contract.container == "mp4"
    assert contract.frame_rate_mode == "cfr"


def test_contract_rejects_gaps_overlaps_remote_assets_and_real_identifiers(
    tmp_path: Path,
) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["scenes"][1]["start_seconds"] = 5.1

    with pytest.raises(ValueError, match="contiguous"):
        DemoContract.from_mapping(payload)

    contract_text = CONTRACT_PATH.read_text(encoding="utf-8")
    assert "http://" not in contract_text
    assert "https://" not in contract_text


@pytest.mark.parametrize("scene_start", [5.1, 4.9])  # type: ignore[untyped-decorator]
def test_contract_rejects_scene_gaps_and_overlaps(scene_start: float) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["scenes"][1]["start_seconds"] = scene_start

    with pytest.raises(ValueError, match="contiguous"):
        DemoContract.from_mapping(payload)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "field, replacement",
    [("container", "mov"), ("frame_rate_mode", "vfr")],
)
def test_contract_rejects_changed_container_and_frame_rate_mode(
    field: str, replacement: str
) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload[field] = replacement

    with pytest.raises(ValueError, match=field):
        DemoContract.from_mapping(payload)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "unsafe_value",
    [
        "HTTP://example.invalid/source",
        "file:///outside",
        "/outside",
        r"C:\\outside",
        r"\\outside",
        r"\\\\server\\share",
    ],
)
def test_contract_rejects_remote_and_absolute_or_rooted_paths(
    unsafe_value: str,
) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["scenes"][0]["purpose"] = unsafe_value

    with pytest.raises(ValueError, match="URL|path"):
        DemoContract.from_mapping(payload)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "index, identifier",
    [
        (0, "real.person@example.com"),
        (1, "+1 202-555-9999"),
        (2, "1.0000, 2.0000"),
    ],
)
def test_contract_rejects_nonapproved_fictional_identifiers(
    index: int, identifier: str
) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["fictional_identifiers"][index] = identifier

    with pytest.raises(ValueError, match="fictional identifiers"):
        DemoContract.from_mapping(payload)


def test_contract_rejects_unknown_nested_keys_and_boolean_numbers() -> None:
    nested_payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    nested_payload["privacy"]["extra"] = "not allowed"
    with pytest.raises(ValueError, match="unknown"):
        DemoContract.from_mapping(nested_payload)

    boolean_payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    boolean_payload["frame_rate"] = True
    with pytest.raises(ValueError, match="integer"):
        DemoContract.from_mapping(boolean_payload)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "nonfinite", [float("nan"), float("inf"), float("-inf")]
)
def test_contract_rejects_nonfinite_numbers(nonfinite: float) -> None:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    payload["duration_seconds"] = nonfinite

    with pytest.raises(ValueError, match="finite"):
        DemoContract.from_mapping(payload)


def test_canonical_json_bytes_is_stable_and_rejects_nan() -> None:
    assert canonical_json_bytes({"b": "中文", "a": 1}) == (
        b'{"a":1,"b":"\xe4\xb8\xad\xe6\x96\x87"}\n'
    )

    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})


def test_stream_sha256_and_safe_relative_path_boundaries(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact = artifact_root / "nested" / "report.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"VideoScope")

    assert stream_sha256(artifact) == (
        "4fa668ba9d981947265ed15cab22c792f294145708c2ff3b6017806035ee7db4"
    )
    assert safe_relative_path(artifact, artifact_root) == "nested/report.json"

    with pytest.raises(ValueError, match="contained"):
        safe_relative_path(tmp_path / "outside.json", artifact_root)
