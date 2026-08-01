# Changelog

本项目的显著变更记录在此。格式参考
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，版本号遵循
[Semantic Versioning](https://semver.org/)。

## [Unreleased]

尚无变更。

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
