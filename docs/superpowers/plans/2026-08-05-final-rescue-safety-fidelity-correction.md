# Final Rescue Safety and Fidelity Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every automatically confirmable Video Rescue action faithfully
previewed, range-exact, honestly verified, and backed by an idempotent resource
lifetime before the Balanced CPU MVP can be called release-ready.

**Architecture:** Add a private capability policy before plan issuance, share
one retained-source timeline contract between preview and execution, and fail
closed when a global action cannot honor locks or native verification. Move
source-handle ownership into an explicit core lifecycle, make Web cleanup call
that contract, and preserve legacy Rescue schema 0.2 execution state as
unknown instead of silently successful.

**Tech Stack:** Python 3.11+, Pydantic v2, OpenCV/NumPy, FFmpeg/FFprobe argument
arrays, FastAPI job manager, pytest, Ruff, mypy, React/TypeScript contract tests.

## Global Constraints

- VideoScope remains local-first and CPU-first; base tests use no network, GPU,
  model, or model download.
- A content-changing action is automatically confirmable only when an actual
  preview represents it, its exact authorized source ranges can be executed,
  and its output is natively verified or explicitly review-gated.
- Unsupported actions are omitted from the immutable confirmation set and
  surfaced with a finite `needs_review` reason; they are never silently run.
- Locked and clean source intervals must remain unchanged within deterministic
  codec tolerance; segmented stabilization and segmented rotation are out of
  scope.
- Every external command is an argument array with `shell=False`; diagnostics
  are bounded and sanitized; paths with spaces, Chinese, and Unicode remain
  supported on Windows, Linux, and macOS.
- Rescue schema remains strict version `0.2`; the action execution ledger is
  additive, new writers emit it, and a missing legacy ledger means unknown.
- The source video is read-only. Private previews and public outputs remain in
  VideoScope-owned output roots and contain no personal absolute paths.
- Every production behavior change follows RED -> verify RED -> GREEN ->
  verify GREEN -> refactor. Tests assert observable behavior, not mocks or
  source text.
- After each task run focused tests plus Ruff/mypy for changed paths. At the
  end run `python scripts/validate.py`, the forced FFmpeg fixture gate, frontend
  tests/type checks/build, package build, and static distribution audit.
- Local commits are authorized for this plan. Do not push, create a PR, tag,
  publish, or deploy.

---

### Task 1: Capability-gated immutable Rescue plans

**Files:**
- Create: `src/videoscope/rescue/capabilities.py`
- Modify: `src/videoscope/rescue/planner.py`
- Modify: `src/videoscope/rescue/pipeline.py`
- Test: `tests/rescue/test_planner.py`
- Test: `tests/rescue/test_pipeline.py`
- Test: `tests/rescue/test_preview.py`

**Interfaces:**
- Produces `ActionCapabilityReason`, `ActionCapabilityDecision`,
  `evaluate_action_capabilities(actions, preview_ranges, *, duration_seconds,
  locked_ranges) -> tuple[ActionCapabilityDecision, ...]`, and
  `capability_review_warning(action, decision) -> str`.
- `build_rescue_plan()` emits only eligible content-changing actions, retains
  non-content actions, and appends deterministic capability warnings.
- `VideoRescuePipeline` treats plan capability warnings as manual-review
  reasons without changing exact-set confirmation semantics.

- [ ] **Step 1: Add failing planner and confirmation regressions**

  Add tests whose literal expectations prove these breaks are caught:

  ```python
  def test_stabilization_without_a_real_preview_is_review_gated() -> None:
      plan = build_plan_with_recommended_stabilization()
      assert all(action.kind is not RescueActionKind.STABILIZE for action in plan.actions)
      assert "preview_renderer_unavailable" in " ".join(plan.assessment_warnings)


  def test_preview_cap_review_gates_every_uncovered_action() -> None:
      plan = build_plan_with_four_disjoint_content_actions(max_preview_ranges=3)
      confirmable = {a.id for a in plan.actions if a.requires_confirmation}
      assert len(confirmable) == 3
      assert all(
          any(
              s < pe and ps < e
              for s, e in action.source_ranges
              for ps, pe in plan.preview_ranges
          )
          for action in plan.actions
          if action.id in confirmable
      )


  def test_global_actions_conflicting_with_locks_are_review_gated() -> None:
      plan = build_plan_with_rotation_offset_and_lock((1.0, 2.0))
      assert {a.kind for a in plan.actions}.isdisjoint(
          {
              RescueActionKind.NORMALIZE_ROTATION,
              RescueActionKind.CORRECT_FIXED_AV_OFFSET,
          }
      )
  ```

- [ ] **Step 2: Run the focused tests and verify RED**

  Run:

  ```text
  python -m pytest tests/rescue/test_planner.py tests/rescue/test_pipeline.py tests/rescue/test_preview.py -q
  ```

  Expected: the new tests fail because unsupported and uncovered actions are
  still issued as confirmable actions.

- [ ] **Step 3: Implement the deterministic private capability policy**

  Add a frozen internal decision record and finite reason enum:

  ```python
  class ActionCapabilityReason(StrEnum):
      ELIGIBLE = "eligible"
      PREVIEW_RENDERER_UNAVAILABLE = "preview_renderer_unavailable"
      PREVIEW_RANGE_UNCOVERED = "preview_range_uncovered"
      LOCKED_RANGE_CONFLICT = "locked_range_conflict"
      RANGE_MAPPING_UNAVAILABLE = "range_mapping_unavailable"


  @dataclass(frozen=True, slots=True)
  class ActionCapabilityDecision:
      action_id: str
      preview_supported: bool
      preview_covered: bool
      range_exact: bool
      verification_mode: Literal["native", "needs_review"]
      automatic: bool
      reason: ActionCapabilityReason
  ```

  Declare actual preview support by action kind. `SELECT_TRACKS` and
  `STABILIZE` are unavailable in the current renderer. Rotation and fixed
  offset require a full-duration action and no lock. Local visual actions may
  use lock-subtracted ranges. Every confirmable action must overlap an actual
  selected preview window.

- [ ] **Step 4: Gate actions and stabilize preview selection before digesting**

  In `build_rescue_plan()`:

  1. build proposed actions;
  2. select bounded preview windows;
  3. evaluate capabilities;
  4. retain non-content actions plus automatic content actions;
  5. reselect windows for the retained set and reevaluate until the action IDs
     and windows are stable;
  6. append `Automatic <kind> action needs review: <reason>.` for each omitted
     content action;
  7. compute artifacts and digest from the final set only.

  Extend pipeline manual-review collection to include those plan warnings,
  deduplicated in stable first-seen order against assessment warnings. Never
  encode an unsupported content action as `requires_confirmation=False`.

- [ ] **Step 5: Verify GREEN and run scoped static checks**

  Run:

  ```text
  python -m pytest tests/rescue/test_planner.py tests/rescue/test_pipeline.py tests/rescue/test_preview.py -q
  python -m ruff check src/videoscope/rescue/capabilities.py src/videoscope/rescue/planner.py src/videoscope/rescue/pipeline.py tests/rescue/test_planner.py tests/rescue/test_pipeline.py tests/rescue/test_preview.py
  python -m mypy src/videoscope/rescue/capabilities.py src/videoscope/rescue/planner.py src/videoscope/rescue/pipeline.py
  ```

- [ ] **Step 6: Commit**

  ```text
  git add src/videoscope/rescue/capabilities.py src/videoscope/rescue/planner.py src/videoscope/rescue/pipeline.py tests/rescue/test_planner.py tests/rescue/test_pipeline.py tests/rescue/test_preview.py
  git commit -m "fix: gate Rescue actions by executable capability"
  ```

### Task 2: Shared retained timeline and faithful preview lineage

**Files:**
- Create: `src/videoscope/rescue/timeline.py`
- Modify: `src/videoscope/rescue/executor.py`
- Modify: `src/videoscope/rescue/commands.py`
- Modify: `src/videoscope/rescue/preview.py`
- Modify: `src/videoscope/rescue/__init__.py`
- Test: `tests/rescue/test_commands.py`
- Test: `tests/rescue/test_executor.py`
- Test: `tests/rescue/test_preview.py`

**Interfaces:**
- Produces `SourceMapping`, `retained_source_ranges(plan)`,
  `mappings_for_ranges(ranges, output_relative_path)`, and
  `preview_source_mappings(plan, window, output_relative_path)`.
- Existing imports of `SourceMapping` remain valid through re-export.
- Preview variants record the faithful local mapping that their media command
  actually renders.

- [ ] **Step 1: Add failing structural-lineage tests**

  ```python
  def test_preview_mapping_removes_middle_damage_and_rebases_output() -> None:
      mappings = preview_source_mappings(
          plan_deleting_2_to_3(), (1.0, 4.0), "faithful-00.mp4"
      )
      assert [
          (m.source_start, m.source_end, m.output_start, m.output_end) for m in mappings
      ] == [
          (1.0, 2.0, 0.0, 1.0),
          (3.0, 4.0, 1.0, 2.0),
      ]


  def test_improved_preview_duration_uses_retained_duration() -> None:
      commands = build_preview_commands(plan_deleting_2_to_3(), source, root)
      improved = commands[2]
      assert improved[improved.index("-t") + 1] == "2"
  ```

  Add a builder test asserting the faithful variant records both mappings and
  that every mapping names the corresponding private faithful preview.

- [ ] **Step 2: Run focused tests and verify RED**

  Run:

  ```text
  python -m pytest tests/rescue/test_commands.py tests/rescue/test_executor.py tests/rescue/test_preview.py -q
  ```

- [ ] **Step 3: Extract the timeline source of truth**

  Move the existing `SourceMapping` value object and retained-range algorithm
  into `timeline.py`. The retained algorithm continues to authorize deletion
  only through bound damage IDs. Add a pure affine mapping builder:

  ```python
  def mappings_for_ranges(
      ranges: Sequence[tuple[float, float]],
      output_relative_path: str,
  ) -> tuple[SourceMapping, ...]:
      cursor = 0.0
      result = []
      for start, end in ranges:
          result.append(
              SourceMapping(
                  start, end, cursor, cursor + end - start, output_relative_path
              )
          )
          cursor += end - start
      return tuple(result)
  ```

  Make final execution and preview import this function instead of maintaining
  separate removal logic.

- [ ] **Step 4: Render preview filters from the shared local mapping**

  For every preview window, intersect the plan's retained ranges with the
  window, rebase them into faithful-preview output time, and pass those
  mappings to `_improvement_filters()`. Keep source preview duration equal to
  the source window; set improved preview duration to the sum of faithful
  mapped durations. Add mappings to `RescuePreviewVariant` without exposing
  absolute paths or changing persisted schema.

- [ ] **Step 5: Verify GREEN and static checks**

  ```text
  python -m pytest tests/rescue/test_commands.py tests/rescue/test_executor.py tests/rescue/test_preview.py -q
  python -m ruff check src/videoscope/rescue/timeline.py src/videoscope/rescue/commands.py src/videoscope/rescue/executor.py src/videoscope/rescue/preview.py tests/rescue/test_commands.py tests/rescue/test_executor.py tests/rescue/test_preview.py
  python -m mypy src/videoscope/rescue/timeline.py src/videoscope/rescue/commands.py src/videoscope/rescue/executor.py src/videoscope/rescue/preview.py
  ```

- [ ] **Step 6: Commit**

  ```text
  git add src/videoscope/rescue/timeline.py src/videoscope/rescue/commands.py src/videoscope/rescue/executor.py src/videoscope/rescue/preview.py src/videoscope/rescue/__init__.py tests/rescue/test_commands.py tests/rescue/test_executor.py tests/rescue/test_preview.py
  git commit -m "fix: share faithful timeline with Rescue previews"
  ```

### Task 3: Exact action ranges and remapped deflicker curves

**Files:**
- Modify: `src/videoscope/rescue/capabilities.py`
- Modify: `src/videoscope/rescue/commands.py`
- Modify: `src/videoscope/rescue/visual.py`
- Modify: `src/videoscope/rescue/executor.py`
- Test: `tests/rescue/test_commands.py`
- Test: `tests/rescue/test_stabilization.py`
- Test: `tests/rescue/test_executor.py`

**Interfaces:**
- Produces
  `remap_flicker_correction(correction, authorized_ranges, mappings) -> FlickerCorrectionPlan | None`.
- Produces
  `require_executable_action_scopes(plan, mappings) -> None` as a forged-plan
  runtime backstop in addition to planner gating.
- Fixed A/V correction remains faithful-only and is never applied a second
  time by the improved-viewing command.

- [ ] **Step 1: Add failing range and remapping tests**

  ```python
  def test_deflicker_curve_is_remapped_after_middle_deletion() -> None:
      mapped = remap_flicker_correction(
          correction_with_points_at_1_2_3_4(),
          ((1.0, 4.0),),
          mappings_deleting_2_to_3(),
      )
      assert mapped.intervals == ((1.0, 2.0),)
      assert [time for time, _gain in mapped.gains] == [1.0, 2.0]


  def test_locked_deflicker_gap_is_not_present_in_filter() -> None:
      command = build_improved_viewing_command(
          plan_with_locked_middle(), source, output, source_mappings=mappings
      )
      filter_text = command[command.index("-vf") + 1]
      assert "between(t,1,2)" in filter_text
      assert "between(t,2,3)" not in filter_text


  def test_fixed_offset_is_not_applied_again_to_improved_candidate() -> None:
      command = build_improved_viewing_command(
          plan_with_offset_and_luma(), faithful, output, source_mappings=mappings
      )
      assert "asetpts" not in command
  ```

  Add forged-plan executor tests proving scoped/global stabilization, rotation,
  or fixed offset with a lock or incomplete mapping raises a sanitized
  `RescueMediaError` before invoking a media runner.

- [ ] **Step 2: Run focused tests and verify RED**

  ```text
  python -m pytest tests/rescue/test_commands.py tests/rescue/test_stabilization.py tests/rescue/test_executor.py -q
  ```

- [ ] **Step 3: Implement curve remapping as a pure transformation**

  Intersect correction intervals with `action.source_ranges` and each faithful
  mapping. Affinely map interval boundaries and gain timestamps. Insert
  hand-derived boundary gains using the existing interpolation rule, discard
  points inside removed or locked gaps, sort/deduplicate finite points, and
  return `None` when no exact interval remains. Remap excluded fade ranges by
  the same rule.

- [ ] **Step 4: Build action-specific filters and fail closed at execution**

  Special-case deflicker before generic filter construction so its internal
  second outer source-time `enable`. Continue generic mapped enables for luma,
  denoise, and sharpen. Remove `CORRECT_FIXED_AV_OFFSET` from improved audio
  filters. Before global execution, require full mapped output and no lock;
  stabilization remains non-executable unless its capability decision allowed
  it.
  embedded FFmpeg time-range timestamps are already faithful-local; do not
  append a second outer source-time `enable`. Continue generic mapped enables
  for luma, denoise, and sharpen. Remove `CORRECT_FIXED_AV_OFFSET` from improved
  audio filters. Before global execution, require full mapped output and no
  lock; stabilization remains non-executable unless its capability decision
  allowed it.
  second outer source-time `enable`. Continue generic mapped enables for luma,
  denoise, and sharpen. Remove `CORRECT_FIXED_AV_OFFSET` from improved audio
  filters. Before global execution, require full mapped output and no lock;
  stabilization remains non-executable unless its capability decision allowed
  it.

- [ ] **Step 5: Verify GREEN and static checks**

  ```text
  python -m pytest tests/rescue/test_commands.py tests/rescue/test_stabilization.py tests/rescue/test_executor.py -q
  python -m ruff check src/videoscope/rescue/capabilities.py src/videoscope/rescue/commands.py src/videoscope/rescue/visual.py src/videoscope/rescue/executor.py tests/rescue/test_commands.py tests/rescue/test_stabilization.py tests/rescue/test_executor.py
  python -m mypy src/videoscope/rescue/capabilities.py src/videoscope/rescue/commands.py src/videoscope/rescue/visual.py src/videoscope/rescue/executor.py
  ```

- [ ] **Step 6: Commit**

  ```text
  git add src/videoscope/rescue/capabilities.py src/videoscope/rescue/commands.py src/videoscope/rescue/visual.py src/videoscope/rescue/executor.py tests/rescue/test_commands.py tests/rescue/test_stabilization.py tests/rescue/test_executor.py
  git commit -m "fix: enforce exact Rescue action ranges"
  ```

### Task 4: Idempotent core retained-source lifecycle

**Files:**
- Modify: `src/videoscope/rescue/pipeline.py`
- Test: `tests/rescue/test_pipeline.py`

**Interfaces:**
- `VideoRescuePipeline.abort(preparation: RescuePreparation | None = None) -> None`
  releases an unexecuted preparation or all unexecuted preparations.
- `VideoRescuePipeline.close() -> None` cancels and idempotently releases all
  retained sources.
- Registry removal always happens before `os.close()`.

- [ ] **Step 1: Add failing lifecycle tests**

  ```python
  def test_abort_releases_awaiting_confirmation_descriptor(tmp_path: Path) -> None:
      pipeline, preparation, descriptor = prepare_with_observable_descriptor(tmp_path)
      pipeline.abort(preparation)
      with pytest.raises(OSError):
          os.fstat(descriptor)
      pipeline.abort(preparation)


  def test_execute_then_prepare_cannot_close_reused_descriptor(tmp_path: Path) -> None:
      pipeline, first = completed_preparation(tmp_path)
      unrelated = os.open(tmp_path / "unrelated.bin", os.O_RDONLY)
      pipeline.prepare(tmp_path / "second.mp4")
      assert os.fstat(unrelated).st_size >= 0
      os.close(unrelated)
  ```

  Add tests for cancel-before-confirmation, failed execute, replacement prepare,
  repeated close, and close while no preparation exists. Patch only the
  descriptor-opening boundary where OS reuse must be deterministic; assertions
  remain on real `os.fstat` behavior.

- [ ] **Step 2: Run focused tests and verify RED**

  ```text
  python -m pytest tests/rescue/test_pipeline.py -q
  ```

- [ ] **Step 3: Replace bare descriptor cleanup with an owned registry**

  Add private helpers that pop `_issued[id(preparation)]` before closing the
  descriptor. Mark an entry executing before media work; cancellation releases
  awaiting entries immediately and leaves executing entries for `finally`.
  `prepare()` releases superseded preparations through the same helper.
  `execute()` and every error branch call that helper exactly once. Catching a
  duplicate close is not the safety mechanism; absence from the registry is.

- [ ] **Step 4: Verify GREEN and static checks**

  ```text
  python -m pytest tests/rescue/test_pipeline.py -q
  python -m ruff check src/videoscope/rescue/pipeline.py tests/rescue/test_pipeline.py
  python -m mypy src/videoscope/rescue/pipeline.py
  ```

- [ ] **Step 5: Commit**

  ```text
  git add src/videoscope/rescue/pipeline.py tests/rescue/test_pipeline.py
  git commit -m "fix: own retained Rescue sources explicitly"
  ```

### Task 5: Web cancellation, deletion, TTL, and shutdown release contract

**Files:**
- Modify: `src/videoscope/web/rescue_jobs.py`
- Modify: `src/videoscope/web/app.py` only if the manager contract requires no
  route-neutral wrapper
- Test: `tests/web/test_rescue_api.py`

**Interfaces:**
- Extend the Web `RescuePipeline` protocol with
  `abort(preparation: RescuePreparation | None = None) -> None` and
  `close() -> None`.
- `RescueJobManager` calls core lifecycle methods; it continues to own and
  separately close only `record.input_descriptor`.

- [ ] **Step 1: Add failing Web lifecycle regressions**

  Extend the real-pipeline contract test and complete fake pipelines with
  observable `abort`/`close` state. Assert behavior, not merely call counts:

  ```python
  def test_cancel_before_confirmation_releases_core_source(
      client, manager, captured_descriptor
  ) -> None:
      job_id = upload_and_wait_for_confirmation(client)
      assert os.fstat(captured_descriptor).st_size > 0
      client.delete(f"/api/rescue/jobs/{job_id}")
      with pytest.raises(OSError):
          os.fstat(captured_descriptor)


  def test_ttl_and_shutdown_leave_no_core_retained_sources(
      manager, descriptor_probe
  ) -> None:
      terminal_job, ttl_descriptor = create_terminal_job_with_pipeline(manager)
      manager.cleanup_expired(now=future_time())
      assert descriptor_probe(ttl_descriptor) == "closed"
      active_job, shutdown_descriptor = create_awaiting_confirmation_job(manager)
      manager.shutdown()
      assert descriptor_probe(shutdown_descriptor) == "closed"
  ```

  Keep fake state complete enough to mirror the real protocol, including
  prepare, confirm, execute, cancel, abort, and close.

- [ ] **Step 2: Run focused tests and verify RED**

  ```text
  python -m pytest tests/web/test_rescue_api.py -q
  ```

- [ ] **Step 3: Wire every terminal path to core release**

  Add one manager helper that detaches the pipeline/preparation under
  `record.lock`, then calls `abort(preparation)` or `close()` outside ownership
  mutation. Use it from failed preparation, cancellation, terminal deletion,
  TTL cleanup, and shutdown. Execution completion remains safe because the core
  release is idempotent. Preserve SSE ordering and existing terminal statuses.

- [ ] **Step 4: Verify GREEN, Web regressions, and static checks**

  ```text
  python -m pytest tests/web/test_rescue_api.py tests/web/test_api.py -q
  python -m ruff check src/videoscope/web/rescue_jobs.py src/videoscope/web/app.py tests/web/test_rescue_api.py
  python -m mypy src/videoscope/web/rescue_jobs.py src/videoscope/web/app.py
  ```

- [ ] **Step 5: Commit**

  ```text
  git add src/videoscope/web/rescue_jobs.py src/videoscope/web/app.py tests/web/test_rescue_api.py
  git commit -m "fix: release Rescue sources across Web lifecycle"
  ```

### Task 6: Native packet-timestamp A/V residual verification

**Files:**
- Modify: `src/videoscope/rescue/verification.py`
- Modify: `src/videoscope/rescue/commands.py`
- Test: `tests/rescue/test_verification.py`
- Test: `tests/rescue/test_audio.py`

**Interfaces:**
- `NativeMediaMeasurementProvider` measures the first usable audio and video
  packet timestamps from bounded FFprobe JSON, not stream metadata alone.
- The `fixed_av_offset` check applies to faithful output and records method,
  tool, tolerance, planned shift, and measured residual.

- [ ] **Step 1: Add failing packet and artifact-role tests**

  ```python
  def test_packet_timestamps_determine_audio_video_residual() -> None:
      provider = native_provider_with_ffprobe_json(packet_fixture(video=0.04, audio=0.31))
      snapshot = provider.measure(media, "faithful-rescue.mp4", never_cancel)
      assert snapshot.av_offset_seconds == pytest.approx(0.27)


  def test_faithful_fixed_offset_check_uses_native_residual() -> None:
      report = _verify(
          tmp_path,
          faithful_updates={
              "av_offset_seconds": 0.01,
              "av_offset_method": "first_usable_packet_timestamp",
              "av_offset_tool_version": "ffprobe test-version",
          },
          actions=(fixed_offset_action,),
      )
      check = _check(report, "faithful", "fixed_av_offset")
      assert check.status is RescueVerificationStatus.PASSED
      assert check.measured == {
          "applicable": True,
          "measurement_method": "first_usable_packet_timestamp",
          "tool_version": "ffprobe test-version",
          "planned_offset_seconds": 0.4,
          "planned_shift_seconds": -0.4,
          "observed_residual_seconds": 0.01,
          "tolerance_seconds": 0.04,
      }
  ```

  Add missing timestamps, malformed packets, and residual exactly over the
  tolerance as `needs_review`/failed cases. Add a command test proving the
  improved candidate does not receive the faithful shift again.

- [ ] **Step 2: Run focused tests and verify RED**

  ```text
  python -m pytest tests/rescue/test_verification.py tests/rescue/test_audio.py tests/rescue/test_commands.py -q
  ```

- [ ] **Step 3: Implement bounded FFprobe packet measurement**

  Add `av_offset_method: str | None` and
  `av_offset_tool_version: str | None` to the private measurement snapshot.
  Build an argument-vector probe using `-show_streams`, `-show_packets`, and a
  bounded `-read_intervals`/packet count. Parse stream indexes and the first
  finite non-negative `pts_time`, falling back to `dts_time` only when PTS is
  absent. Obtain and cache a sanitized first-line FFprobe version through a
  separate bounded argument-array call. Return `None` evidence if either
  stream has no unambiguous usable packet. Never retain raw probe JSON in
  public output.

- [ ] **Step 4: Verify fixed offset on the faithful artifact**

  Split verification parameters by artifact role. Faithful parameters include
  `CORRECT_FIXED_AV_OFFSET`; improved parameters include enhancements and may
  inherit the fixed-offset verification expectation without re-executing the
  filter. A missing native residual is `needs_review`; it never passes because
  `shift == -offset` alone.

- [ ] **Step 5: Verify GREEN and static checks**

  ```text
  python -m pytest tests/rescue/test_verification.py tests/rescue/test_audio.py tests/rescue/test_commands.py -q
  python -m ruff check src/videoscope/rescue/verification.py src/videoscope/rescue/commands.py tests/rescue/test_verification.py tests/rescue/test_audio.py tests/rescue/test_commands.py
  python -m mypy src/videoscope/rescue/verification.py src/videoscope/rescue/commands.py
  ```

- [ ] **Step 6: Commit**

  ```text
  git add src/videoscope/rescue/verification.py src/videoscope/rescue/commands.py tests/rescue/test_verification.py tests/rescue/test_audio.py tests/rescue/test_commands.py
  git commit -m "fix: verify faithful A V residual natively"
  ```

### Task 7: Real FFmpeg evidence for sync, locks, curves, and stabilization

**Files:**
- Modify: `scripts/generate_test_videos.py`
- Modify: `tests/fixtures/manifest.json`
- Modify: `tests/rescue/test_fixture_rescue.py`
- Modify: `tests/rescue/test_stabilization.py`
- Modify: `tests/rescue/test_verification.py`

**Interfaces:**
- Generated fixtures remain offline, deterministic, <=320x180, short, and
  overwriteable with `--force`.
- Real-media tests skip with an actionable reason only when FFmpeg/FFprobe or
  generated fixtures are absent; CI generates them before this gate.

- [ ] **Step 1: Add failing real-media acceptance tests**

  ```python
  def test_real_fixed_offset_correction_reduces_packet_residual(
      real_rescue_fixture,
  ) -> None:
      result = run_real_balanced_rescue_with_bound_offset("rescue_fixed_av_offset.mp4")
      check = next(
          item
          for item in result.verification.checks
          if item.artifact == "faithful" and item.check_id == "fixed_av_offset"
      )
      assert (
          abs(check.measured["observed_residual_seconds"])
          <= check.measured["tolerance_seconds"]
      )


  def test_real_shake_never_claims_measured_crop_without_native_evidence(
      real_rescue_fixture,
  ) -> None:
      preparation = prepare_real_shake_with_measured_assessment()
      assert all(
          a.kind is not RescueActionKind.STABILIZE for a in preparation.plan.actions
      )
      assert "preview_renderer_unavailable" in " ".join(
          preparation.plan.assessment_warnings
      )
  ```

  Add a combined flicker-plus-middle-deletion fixture or a deterministic local
  derivative. Decode representative frames and assert correction occurs only
  in mapped authorized intervals; locked and clean intervals stay within the
  manifest's numeric codec tolerance.

- [ ] **Step 2: Run the real tests and verify RED**

  ```text
  python scripts/generate_test_videos.py --force
  python -m pytest tests/rescue/test_fixture_rescue.py tests/rescue/test_stabilization.py tests/rescue/test_verification.py -q
  ```

- [ ] **Step 3: Extend deterministic fixture contracts**

  Keep the existing 0.4-second fixed-offset and shake recipes. Add only the
  combined structural/deflicker recipe needed to test time remapping, with
  hand-authored manifest fields for source deletion, authorized correction,
  locked interval, residual tolerance, and decoded-frame tolerance. Do not
  change annotations to fit observed output.

- [ ] **Step 4: Make real tests exercise production boundaries**

  Use the real planner, preview builder, native executor, and native verifier.
  Inject only deterministic assessment evidence when the synthetic signal does
  not exercise the assessor itself. Assert artifacts, mappings, check status,
  and measured numeric values rather than runner calls.

- [ ] **Step 5: Verify GREEN and force-regenerate twice**

  ```text
  python scripts/generate_test_videos.py --force
  python -m pytest tests/rescue/test_fixture_rescue.py tests/rescue/test_stabilization.py tests/rescue/test_verification.py -q
  python scripts/generate_test_videos.py --force
  python -m pytest tests/rescue/test_fixture_rescue.py -q
  python -m ruff check scripts/generate_test_videos.py tests/rescue/test_fixture_rescue.py tests/rescue/test_stabilization.py tests/rescue/test_verification.py
  ```

- [ ] **Step 6: Commit**

  ```text
  git add scripts/generate_test_videos.py tests/fixtures/manifest.json tests/rescue/test_fixture_rescue.py tests/rescue/test_stabilization.py tests/rescue/test_verification.py
  git commit -m "test: prove native Rescue safety on real media"
  ```

### Task 8: Rescue schema 0.2 compatibility and release gates

**Files:**
- Modify: `src/videoscope/rescue/models.py`
- Modify: `src/videoscope/rescue/serialization.py` if canonical loading needs a
  presence-preserving entry point
- Modify: `src/videoscope/rescue/report.py`
- Modify: `docs/rescue-schema.md`
- Modify: `docs/architecture.md`
- Modify: `docs/release-checklist.md`
- Modify: `CHANGELOG.md`
- Test: `tests/rescue/test_models.py`
- Test: `tests/rescue/test_serialization.py`
- Test: `tests/rescue/test_report.py`
- Test: `tests/web/test_rescue_api.py`

**Interfaces:**
- `RescueChangeLog.action_execution_state_known` and
  `RescueTechnicalReport.action_execution_state_known` distinguish a missing
  legacy field from an explicitly emitted empty ledger while retaining the
  tuple default required by schema 0.2.
- New writers always pass `action_executions`; renderers label legacy absence
  as unknown, never as successful.

- [ ] **Step 1: Add failing legacy and stale-binding tests**

  ```python
  def test_legacy_v02_without_action_ledger_is_unknown() -> None:
      payload = legacy_v02_change_log_without("action_executions")
      parsed = RescueChangeLog.model_validate(payload)
      assert parsed.action_executions == ()
      assert parsed.action_execution_state_known is False


  def test_explicit_empty_ledger_is_known_and_new_writers_emit_it() -> None:
      parsed = RescueChangeLog.model_validate(
          {**legacy_payload(), "action_executions": []}
      )
      assert parsed.action_execution_state_known is True
      assert "action_executions" in json.loads(rescue_change_log_to_json(parsed))


  def test_report_labels_missing_legacy_ledger_unknown() -> None:
      html = render_legacy_report_without_ledger()
      assert "Execution state unknown" in html
      assert "All actions succeeded" not in html
  ```

  Add an unknown-field rejection test and a stale preview/confirmation binding
  test that requires a new preparation rather than accepting an old document.

- [ ] **Step 2: Run focused tests and verify RED**

  ```text
  python -m pytest tests/rescue/test_models.py tests/rescue/test_serialization.py tests/rescue/test_report.py tests/web/test_rescue_api.py -q
  ```

- [ ] **Step 3: Preserve ledger field presence without weakening strictness**

  Keep
  `action_executions: tuple[RescueActionExecution, ...] = ()`. Add a computed
  property that checks Pydantic's `model_fields_set` for `action_executions`.
  New pipeline writers continue to pass the tuple explicitly. Canonical
  serialization keeps emitting the field; unknown extra fields remain
  forbidden. Render unknown legacy state as a limitation, not a successful
  ledger.

- [ ] **Step 4: Document the compatibility and confirmation decision**

  State that schema remains 0.2 because the ledger is additive; absent means
  unknown, explicit empty means no planned executable action, and neither means
  all actions succeeded. State that newly issued confirmations bind the exact
  previewed action set and persisted previews without that binding must be
  regenerated. Update the release checklist and changelog with measured facts
  only.

- [ ] **Step 5: Run complete release gates**

  Run from the worktree with local FFmpeg on `PATH` and `PYTHONPATH=src`:

  ```text
  python scripts/validate.py
  python scripts/generate_test_videos.py --force
  python -m pytest tests/rescue/test_fixture_rescue.py tests/privacy/test_fixture_privacy.py tests/resolve/test_fixture_publish.py -q
  python -m build
  cd web
  npm test
  npx tsc --noEmit -p tsconfig.app.json
  npx tsc --noEmit -p tsconfig.node.json
  npm run build
  ```

  Then return to the repository root and run
  `python scripts/audit_distribution.py dist`. Record the known external
  NumPy/mypy stub incompatibility separately if it remains; do not call the
  unified gate passed while it exits nonzero.

- [ ] **Step 6: Commit**

  ```text
  git add src/videoscope/rescue/models.py src/videoscope/rescue/serialization.py src/videoscope/rescue/report.py docs/rescue-schema.md docs/architecture.md docs/release-checklist.md CHANGELOG.md tests/rescue/test_models.py tests/rescue/test_serialization.py tests/rescue/test_report.py tests/web/test_rescue_api.py
  git commit -m "docs: finalize Rescue safety compatibility"
  ```

## Final Review Requirements

After Task 8, generate a whole-plan review package from commit `e5aa933` to
`HEAD` and dispatch an independent highest-capability reviewer. The review must
check every approved design requirement, every deferred finding in the plan
ledger, source immutability, offline behavior, and truthful verification. If
findings exist, use the one permitted final fix wave and one scoped re-review.
The branch is not merge-ready or release-ready unless zero Critical and zero
Important findings remain.
