# Changelog

本项目的显著变更记录在此。格式参考
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本号遵循
[Semantic Versioning](https://semver.org/)。

## [Unreleased]

## [0.8.1] - Pending publication

### Added

- Reproducible, project-authored public case studies and a deterministic local
  four-mode demonstration with explicit media provenance and contract checks.
- Bounded Video Rescue V15 qualification for measured tonal, clarity, audio,
  and stabilization candidates while preserving preview, confirmation, source
  immutability, and independent verification gates.
- Deterministic, no-clobber release evidence generation and verification for
  the exact wheel, sdist, Windows installer, frozen commit and checksums.

### Changed

- The public site now leads with verified rescue outcomes and exposes focused
  download, examples, developer, community, roadmap, and case-study routes.
- CI and native release validation pin FFmpeg and ffprobe 8.1.2 for the
  version-sensitive media qualification gates.

### Fixed

- Hardened Windows atomic publication and bounded retry behavior across Safe
  Sharing, Video Rescue, Long Video to Useful Content, demo verification, and
  validation-controller paths without treating target collisions as success.
- Removed a Publish Ready SSE test race by waiting for callback registration
  before delivering the synthetic event.
- Retries a bounded tonal probe only when a nominally successful result is
  semantically unusable, while unsupported complete stream layouts still fail
  closed without being reinterpreted.

### Security and privacy

- Updated the public-site lockfile to a non-vulnerable transitive `nanoid`
  version without changing the declared dependency surface.
- Tightened public-case provenance, allowlists, path containment, serialized
  evidence, and no-clobber validation roots.
- Audits the frozen Python/npm license closure and requires Windows bundles to
  carry and verify the project license, `NOTICE` and third-party license
  materials; public installer links require an exact official URL/SHA pair.

## [0.8.0] - 2026-08-11

### Added

- A fixed-origin local connector for the public GitHub Pages entry, with
  expiring pairing sessions and complete A/B/C/D workbench links.
- Memory-only BYOK provider profiles and an OpenAI-compatible structured-text
  adapter for the existing evidence-grounded Advanced AI review flow.
- An encrypted local device account with register/unlock, encrypted backup,
  import, sign-out and deletion, while preserving anonymous browser use.

### Security and privacy

- Public-site requests require an exact allowlisted origin and connector
  session. Provider secrets can be written only from the loopback UI, do not
  appear in OpenAPI or responses, and are cleared when the process exits.
- Remote AI requires explicit per-run data-transfer consent. Provider failures
  remain isolated from CPU results and never authorize an automatic edit.
- The official deployment continues to provide no central video upload,
  storage, inference queue or mandatory subscription.

### Fixed

- Repository validation runs the real native Rescue fixture acceptance in a
  dedicated pytest process while preserving the complete test set. This keeps
  long-lived OpenCV/FFmpeg process state out of the strict structural outcome
  gate and prints the exact non-passing verification checks on failure.

### Known limitations

- Full Python/FFmpeg workflows require a one-time local connector installation;
  a static site cannot safely provide those capabilities by itself.
- The default account is device-local and has no server-side recovery or
  automatic cross-device sync. Optional cloud auth remains deployment-owned.
- Initial BYOK support targets bounded structured text on explicitly compatible
  endpoints. Native Claude, Gemini, media-generation and other protocols need
  dedicated adapters before they can be advertised as supported.

### 0.7.0 development line

### Added

- Optional local Advanced AI for C: trusted transcript or explicitly authorized
  Faster Whisper ASR, loopback-only Ollama semantic suggestions, and grounded
  chapter, highlight, summary and title drafts.
- Strict suggestion/review contracts, deterministic identities, canonical
  private JSON, shared lazy model runtime, Fake Providers and capability-specific
  grounding metrics without an aggregate quality score.
- CLI and bilingual local Web review flows with reject-by-default decisions,
  editable evidence ranges, revision binding, and a bridge into C's existing
  private-preview, exact-confirmation and verification gates.

### Security and privacy

- The base install remains offline and model-free. Ollama is loopback-only and
  never pulled automatically; Faster Whisper download is denied unless enabled
  explicitly for one run.
- AI provider failure cannot erase or falsify CPU results. Private transcripts,
  suggestions and reviews stay outside public artifact routes.

### Known limitations

- Suggestions are heuristic and model-dependent. They do not establish truth,
  importance, popularity, identity, copyright or legal compliance.
- Fake and synthetic tests establish engineering behavior only. Real-video
  usefulness requires the documented held-out human evaluation protocol.

### 0.6.0 development line

### Added

- Long Video to Useful Content, a local CPU workflow with Faithful Clean,
  Chaptered Full, and Selected Clips goals using one shared evidence pipeline.
- Strict content maps, storyboards, exact action ranges, bounded private join
  previews, digest-bound confirmation, source maps, change logs, chapters,
  optional subtitle/clip exports, offline reports, and fail-closed verification.
- Shared CLI and local Web orchestration with bilingual review, persistence,
  recovery, cancellation and deletion; deterministic media/transcript fixtures;
  native FFmpeg gates; and exact-wheel smoke coverage for all three goals.

### Security and privacy

- Source media remains byte-for-byte read-only. Transcript evidence, waveforms,
  thumbnails and previews stay under `content-review-private/`; only verified
  allowlisted artifacts can enter `content-output/`.
- Every content change requires the exact current action set and preview-bound
  plan digest. Locked ranges, source order and complete source mappings are
  independently checked after native rendering.
- The base install remains CPU-only and offline-capable. This feature adds no AI,
  OCR, GPU, model download, remote API, upload or telemetry requirement.

### Known limitations

- The CPU MVP does not transcribe, summarize, rank semantic highlights, generate
  titles, creatively reorder content, or infer user intent. Structural signals
  are heuristic and every removal requires human review.
- Synthetic fixtures prove engineering behavior, not real-world usefulness or
  accuracy. Representative long-video playback, performance and cross-platform
  review remain human release gates.

### 0.5.0 development line

### Added

- Video Rescue, an opt-in local CPU workflow with Conservative and Balanced
  strategies, a private same-range preview, exact plan-digest confirmation,
  independent faithful/improved artifacts, and post-processing verification.
- Observable symptom hints for container/timeline, video, and audio problems;
  deterministic damage/action/plan JSON; partial salvage with explicit
  unrecovered source ranges; and an offline Rescue HTML report.
- Shared CLI and local Web orchestration, English and Simplified Chinese Rescue
  review screens, cancellation/recovery/deletion controls, deterministic local
  fixtures, real FFmpeg end-to-end tests, archive gates, and clean-wheel smoke.

### Security and privacy

- Source media remains byte-for-byte read-only. Private previews, staging, and
  public `rescue-output/` are physically separated and public JSON uses only
  output-relative POSIX paths.
- The base install remains CPU-only and offline-capable; Video Rescue does not
  install AI/OCR extras, download models, use GPU, or upload media.
- Rescue schema 0.2 now distinguishes a legacy missing action-execution ledger
  from an explicitly empty ledger. Canonical writers emit the ledger; HTML
  labels legacy execution state as unknown, and stale preview/confirmation
  state requires a new preparation.

### Fixed

- Bound NumPy below 2.5 so the declared Python 3.11 support and strict mypy target
  remain compatible with installed dependency stubs.
- Split clean-wheel smoke media into purpose-built Publish Ready, Safe Sharing,
  and Video Rescue inputs, preventing one workflow's fixture assumptions from
  creating a false failure or false pass in another workflow.
- Closed strict typing gaps across Rescue runtime, fixture generation, CLI, Web,
  and failure-injection tests without narrowing the repository mypy scope.

### Known limitations

- Filtering can reduce observable noise, darkness, flicker, softness, shake, or
  audio problems when supported by measurements; it cannot recreate source
  detail, frames, samples, or content that was never present or was destroyed.
- Synthetic fixtures establish engineering regressions, not real-world recovery
  rates. Real phone/camera/meeting/player and Linux/macOS acceptance remains a
  human release gate.

### 0.4.0 development line

### Added

- Safe Sharing 本地 CPU 工作流：私有风险图、受众 Profile、人工视觉区域与
  静音区间、私有预览、确定性计划摘要、显式确认、原子公开包和保守输出验证。
- 匿名人脸区域和 QR/条码 CPU 建议；可选 OCR 文字建议保持延迟加载和失败隔离。
- `videoscope privacy`、`/api/privacy` 本地 API、SSE 恢复，以及英语/简体中文
  React 复核工作台；`what912` 标志不随语言切换。
- 五个本地生成的隐私回归视频和真实 FFmpeg 端到端 Safe Sharing 验证。
- clean-wheel 手工区域 Safe Sharing 烟雾测试和更严格的 wheel/sdist 隐私产物审计。

### Security and privacy

- Safe Sharing now publishes its six-file public package only for a fully
  `completed` verification outcome. `needs_review`, `partial`, `failed`, and
  `cancelled` remove pending candidates and expose no public downloads.
- Private previews are rendered from an FFmpeg-bounded source clip using the
  configured `preview_seconds`; preview generation never creates a public package.
- 源视频保持只读，媒体修改只写入新副本；确认摘要绑定完整计划、预览身份和
  验证策略，并通过跨进程声明与生命周期门防止重复或陈旧执行。
- `privacy-review-private/` 与 `share-package/` 物理分离；公开包只允许六个固定
  产物，不含原始 OCR、未脱敏证据、用户名、GPS 或个人绝对路径。
- 扫描器、验证器、恢复或取消失败不能伪装成无风险或 `completed`。

### Known limitations

- 自动区域和可选文字扫描都是启发式工程能力，尚无真实独立标注集准确率；
  遮挡、采样、分辨率和 OCR 错误会导致误报或漏报。
- CPU MVP 不自动识别敏感语音。用户必须人工标记静音区间，并在分享前完整播放、
  检查预览和最终副本。
- `completed` 仅表示记录的必需检查通过，不是匿名性、安全性或合规认证。

### 0.3.0 development line (Publish Ready foundation)

### Added

- Publish Ready 本地处理流水线，带确定性计划摘要、显式确认、预览、封面、
  变更记录和输出后技术验证。
- `compatible_mp4`、`social_vertical_9_16` 和
  `social_horizontal_16_9` 三个版本化 Profile；社交画布使用 scale-and-pad
  保留完整源画面，不裁剪。
- `videoscope publish` 命令、本地 Publish Ready Web API 和双语 Dashboard
  工作台。
- 含音频的确定性 `publish_av.mp4` 工程回归 fixture，以及三个 Profile 的
  真实 FFmpeg 端到端验证。

### Security and privacy

- Publish Ready 保持源视频只读，产物写入单独目录；所有处理继续在本机完成。
- 计划确认绑定确定性摘要，公开 JSON 只记录输出根目录内的相对 POSIX 路径。
- 基础 wheel 烟雾测试不安装 AI、OCR 或 Web extras，也不下载模型。

### Known limitations

- Publish Ready 验证只证明当前 Profile 的技术检查通过，不证明艺术质量或平台
  永久兼容性。
- 处理依赖系统 FFmpeg 提供 H.264 (`libx264`) 与 AAC 编码能力；长视频的资源
  消耗、VFR 输入和不同播放器仍需在目标环境人工检查。

## [0.2.0-rc1] - 2026-07-29

GenVideoScope v0.2.0 的首个发布候选。PyPI distribution 名称改为
`genvideoscope`，Python 包和 CLI 继续使用 `videoscope`。

### Added

- Shared lazy model runtime, singleton providers, batched inference, bounded LRU
  memory cache, local embedding disk cache, and explicit download permission.
- Optional OpenCLIP prompt/frame similarity detector.
- Optional OpenCLIP or DINOv2 within-scene visual semantic drift detector.
- Optional PaddleOCR temporal text stability detector with evidence boxes.
- Optional loopback-only FastAPI analysis jobs with SSE progress and retention.
- Optional React and TypeScript dashboard with local upload, detector selection,
  progress, interactive Finding timeline, evidence review, and packaged static
  assets.
- Release examples, dependency/license inventory, offline test network guard,
  and final release audit documentation.

### Security and privacy

- Reject untrusted Host headers and non-loopback browser Origin headers by
  default on the local Web service.
- Keep optional model downloads opt-in and non-interactive downloads disabled
  unless explicitly authorized.
- Preserve CPU findings when an optional provider or detector fails.

### Known limitations

- Optional AI and OCR findings remain heuristic and are not calibrated semantic
  correctness, identity, or defect probabilities.
- Real model weights and PaddleOCR integration are not exercised by base CI.
- The local Web service has no account system; non-loopback binding explicitly
  broadens the trust boundary.

## [0.1.0] - 2026-07-28

首个可发布的 CPU 版本。

### Added

- 本地视频元数据探测、SHA-256 输入哈希和确定性抽帧。
- 镜头划分及失败时的固定窗口 fallback。
- 四个无需 GPU、网络或 AI 模型的检测器：
  - `near_black`
  - `possible_freeze`
  - `scene_relative_blur`
  - `global_flicker`
- 统一、版本化的 Finding 与 AnalysisReport JSON 结构。
- `videoscope analyze` CLI、原子 JSON 写入和离线 HTML 时间轴报告。
- 证据帧、检测器失败隔离和 `detector_error` 记录。
- 可复现的合成回归视频、时间区间 Benchmark 和阈值比较工具。
- Windows 与 Linux、Python 3.11 与 3.12 的 CI 配置。

### Security and privacy

- 默认在本机分析，不上传输入、提示词、帧或报告。
- 基础安装不下载模型，检测器不需要网络或 GPU。
- FFmpeg 与 ffprobe 作为外部系统依赖调用，不随发行包分发。

### Known limitations

- 这些检测器使用可解释启发式，不是对创作意图或真实故障的确定判断。
- 静态镜头、夜景、淡入淡出和有意风格可能引起误报。
- 合成 fixtures 只用于工程回归，不能代表真实生成视频准确率。
- v0.1 不包含视频生成、修复、身份识别、AI/VLM 或 Web 服务。
