from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.full_local_demo_contract import DemoContract, load_demo_contract

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    REPOSITORY_ROOT / "demos" / "full-local-four-mode" / "demo-contract.json"
)


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
