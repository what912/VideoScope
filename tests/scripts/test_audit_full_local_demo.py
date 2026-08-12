from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import JsonValue

from scripts.audit_full_local_demo import (
    AuditSummary,
    AuditWorkflow,
    assemble_summary,
    audit_source_and_results,
    build_contact_sheet,
    hero_timestamps,
    render_beginner_guide,
    write_verification_summary,
)
from scripts.full_local_demo_contract import load_demo_contract
from scripts.validate_full_local_demo import WorkflowOutcome

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "demos" / "full-local-four-mode" / "demo-contract.json"
SHA = "a" * 64


def test_contact_sheet_uses_all_seven_hero_frames() -> None:
    timestamps = hero_timestamps(load_demo_contract(CONTRACT_PATH))
    assert timestamps == (2.5, 7.5, 15.0, 22.5, 28.5, 34.0, 39.0)


def test_summary_never_promotes_review_needed_to_passed() -> None:
    workflow = WorkflowOutcome(
        workflow_id="video_rescue",
        status="needs_review",
        source_sha256_before=SHA,
        source_sha256_after=SHA,
    )
    summary = assemble_summary((workflow,))
    assert summary.workflows["video_rescue"].status == "needs_review"
    assert summary.overall_status != "passed"


def test_public_files_have_no_absolute_paths_or_secrets(tmp_path: Path) -> None:
    workflow = AuditWorkflow(
        workflow_id="publish_ready",
        status="completed",
        artifacts={"video": "publish-ready/video.mp4"},
        source_unchanged=True,
    )
    summary = AuditSummary(
        source_sha256=SHA,
        contract_digest=SHA,
        deterministic_generation_status="passed",
        workflows={workflow.workflow_id: workflow},
        overall_status="passed",
    )
    output = tmp_path / "verification-summary.json"
    write_verification_summary(summary, output)
    text = output.read_text(encoding="utf-8")
    assert str(Path.home()) not in text
    assert not re.search(r"[A-Za-z]:[/\\\\]", text)
    assert "api_key" not in text.lower()


def test_artifact_paths_reject_absolute_and_parent_paths() -> None:
    with pytest.raises(ValueError):
        AuditWorkflow(
            workflow_id="publish_ready",
            status="completed",
            artifacts={"video": "C:/private/video.mp4"},
            source_unchanged=True,
        )
    with pytest.raises(ValueError):
        AuditWorkflow(
            workflow_id="publish_ready",
            status="completed",
            artifacts={"video": "../video.mp4"},
            source_unchanged=True,
        )


def test_missing_safe_sharing_public_artifact_is_not_verified(tmp_path: Path) -> None:
    source = tmp_path / "VideoScope-Full-Local-Demo-Source.mp4"
    source.write_bytes(b"source")
    import hashlib

    source_sha = hashlib.sha256(b"source").hexdigest()
    contract_bytes = b'{"contract":"test"}\n'
    (tmp_path / "demo-contract.json").write_bytes(contract_bytes)
    contract_sha = hashlib.sha256(contract_bytes).hexdigest()
    (tmp_path / "demo-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "source": {"path": source.name, "sha256": source_sha},
                "contract": {"path": "demo-contract.json", "sha256": contract_sha},
                "tools": {
                    "generator": "1.0",
                    "ffmpeg": "test",
                    "ffprobe": "test",
                    "hyperframes": "test",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "execution-outcomes.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "source_sha256": source_sha,
                "contract_sha256": contract_sha,
                "outcomes": {
                    "safe_sharing": {
                        "workflow_id": "safe_sharing",
                        "status": "needs_review",
                        "source_sha256_before": source_sha,
                        "source_sha256_after": source_sha,
                        "artifacts": {},
                        "final_human_review_required": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    summary = audit_source_and_results(tmp_path)

    safe = summary.workflows["safe_sharing"]
    assert safe.status == "not_verified"
    assert safe.final_human_review_required is True
    assert summary.overall_status != "passed"


def test_beginner_guide_has_confirmation_and_review_warning(tmp_path: Path) -> None:
    workflow = AuditWorkflow(
        workflow_id="safe_sharing",
        status="not_verified",
        final_human_review_required=True,
        source_unchanged=True,
    )
    summary = AuditSummary(
        source_sha256=SHA,
        contract_digest=SHA,
        deterministic_generation_status="passed",
        workflows={workflow.workflow_id: workflow},
        overall_status="not_verified",
    )
    template = tmp_path / "template.md"
    template.write_text(
        "# {title}\n\n{workflow_table}\n\n{steps}\n\n{limitations}\n", encoding="utf-8"
    )
    output = tmp_path / "README-demo.md"

    render_beginner_guide(summary, template, output)

    text = output.read_text(encoding="utf-8")
    assert "确认" in text
    assert "digest" in text
    assert "人工复核" in text
    assert str(Path.home()) not in text


def test_contact_sheet_extracts_all_seven_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image

    import scripts.audit_full_local_demo as audit

    video = tmp_path / "source.mp4"
    video.write_bytes(b"video")
    extracted: list[str] = []

    def fake_run(arguments: list[str]) -> object:
        destination = Path(arguments[-1])
        extracted.append(arguments[arguments.index("-ss") + 1])
        Image.new("RGB", (320, 180), (20, 40, 60)).save(destination)
        return object()

    monkeypatch.setattr(audit, "_run", fake_run)
    output = tmp_path / "sheet.webp"

    build_contact_sheet(video, HERO_TIMESTAMPS_FOR_TEST, output)

    assert extracted == [f"{item:.3f}" for item in HERO_TIMESTAMPS_FOR_TEST]
    with Image.open(output) as sheet:
        assert sheet.size == (1280, 440)


HERO_TIMESTAMPS_FOR_TEST = (2.5, 7.5, 15.0, 22.5, 28.5, 34.0, 39.0)


def test_useful_content_requires_exact_confirmed_source_mappings() -> None:
    import scripts.audit_full_local_demo as audit

    actions: tuple[dict[str, JsonValue], ...] = tuple(
        {"source_range": {"start_seconds": start, "end_seconds": end}}
        for start, end in ((0.0, 5.0), (10.0, 20.0), (36.0, 42.0))
    )
    assert audit._check_useful_source_mappings(actions)["status"] == "passed"
    assert audit._check_useful_source_mappings(actions[:-1])["status"] == "not_verified"


def test_safe_sharing_absent_checks_are_explicitly_not_verified() -> None:
    import scripts.audit_full_local_demo as audit

    checks = audit._safe_sharing_not_verified_checks()
    assert [item["check_id"] for item in checks] == [
        "redaction_boundaries",
        "audio_mute_30db",
        "forbidden_metadata",
    ]
    assert {item["status"] for item in checks} == {"not_verified"}


def test_safe_sharing_present_media_runs_three_targeted_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.audit_full_local_demo as audit

    source = tmp_path / "source.mp4"
    candidate = tmp_path / "candidate.mp4"
    source.write_bytes(b"source")
    candidate.write_bytes(b"candidate")
    monkeypatch.setattr(
        audit,
        "_safe_sharing_visual_check",
        lambda *_: {"check_id": "redaction_boundaries", "status": "passed"},
    )
    monkeypatch.setattr(
        audit,
        "_safe_sharing_audio_check",
        lambda *_: {"check_id": "audio_mute_30db", "status": "passed"},
    )
    monkeypatch.setattr(
        audit,
        "_safe_sharing_metadata_check",
        lambda *_: {"check_id": "forbidden_metadata", "status": "passed"},
    )

    checks = audit._audit_safe_sharing_media(source, candidate)

    assert [item["check_id"] for item in checks] == [
        "redaction_boundaries",
        "audio_mute_30db",
        "forbidden_metadata",
    ]


def test_safe_sharing_artifact_is_resolved_from_workflow_root(tmp_path: Path) -> None:
    import scripts.audit_full_local_demo as audit

    artifact = tmp_path / "safe-sharing" / "share-package" / "share-safe.mp4"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"video")

    resolved = audit._resolve_artifact(
        tmp_path, "safe_sharing", "share-package/share-safe.mp4"
    )

    assert resolved == artifact.resolve()


def test_atomic_write_preserves_previous_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.audit_full_local_demo as audit

    output = tmp_path / "summary.json"
    output.write_text("approved", encoding="utf-8")

    def fail_replace(source: Path, target: Path) -> None:
        del source, target
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        audit._atomic_write(output, b"partial")
    assert output.read_text(encoding="utf-8") == "approved"
    assert list(tmp_path.iterdir()) == [output]


def test_contract_digest_is_recomputed_not_trusted(tmp_path: Path) -> None:
    import hashlib

    import scripts.audit_full_local_demo as audit

    contract = tmp_path / "contract.json"
    contract.write_text('{"version": 2}', encoding="utf-8")
    stale = hashlib.sha256(b'{"version":1}\n').hexdigest()
    with pytest.raises(audit.AuditError, match="contract hash"):
        audit._verify_contract_digest(tmp_path, "contract.json", stale)


def test_deterministic_generation_requires_matching_source_and_manifest_pair(
    tmp_path: Path,
) -> None:
    import scripts.audit_full_local_demo as audit

    current_source = tmp_path / "source.mp4"
    first_source = tmp_path / ".fix1-first-source.mp4"
    current_manifest = tmp_path / "demo-manifest.json"
    first_manifest = tmp_path / ".fix1-first-manifest.json"
    current_source.write_bytes(b"same")
    first_source.write_bytes(b"same")
    current_manifest.write_bytes(b'{"run": 2}')
    first_manifest.write_bytes(b'{"run": 1}')
    assert audit._deterministic_generation_status(tmp_path, current_source) == (
        "not_verified"
    )
    first_manifest.write_bytes(current_manifest.read_bytes())
    assert audit._deterministic_generation_status(tmp_path, current_source) == "passed"


def test_failed_mandatory_check_downgrades_completed_workflow() -> None:
    import scripts.audit_full_local_demo as audit

    outcome = WorkflowOutcome(
        workflow_id="publish_ready",
        status="completed",
        source_sha256_before=SHA,
        source_sha256_after=SHA,
    )
    workflow = audit._audit_workflow(outcome)
    downgraded = audit._apply_mandatory_checks(
        workflow,
        ({"check_id": "probe_video", "status": "not_verified"},),
    )
    assert downgraded.status == "not_verified"


def test_public_string_values_reject_secret_assignments() -> None:
    for secret in ("api_key=sk-example", "token: bearer-example", "password = hunter2"):
        with pytest.raises(ValueError, match="secret"):
            AuditWorkflow(
                workflow_id="publish_ready",
                status="completed",
                source_unchanged=True,
                limitations=(secret,),
            )


def test_public_string_values_reject_secret_nested_in_actions() -> None:
    with pytest.raises(ValueError, match="secret"):
        AuditWorkflow(
            workflow_id="publish_ready",
            status="completed",
            source_unchanged=True,
            actions=({"description": "token=hidden"},),
        )


def test_public_string_values_allow_plain_api_key_guidance() -> None:
    workflow = AuditWorkflow(
        workflow_id="publish_ready",
        status="completed",
        source_unchanged=True,
        limitations=("No API key is required for this local audit.",),
    )
    assert workflow.limitations


def test_beginner_template_inventories_embedded_gsap_runtime() -> None:
    text = (ROOT / "demos" / "full-local-four-mode" / "README-template.md").read_text(
        encoding="utf-8"
    )
    assert "GSAP 3.15.0" in text
    assert "Copyright GreenSock" in text
    assert "https://gsap.com/standard-license" in text
    assert "embedded offline" in text
    assert "92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb" in text
