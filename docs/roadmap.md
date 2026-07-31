# VideoScope 路线图

状态：v0.1 CPU 功能已实现；发布门槛以实际验证结果为准

## 1. 版本原则

- v0.1 先交付可发布的 CPU 基础版；
- 每个里程碑都以可验证产物和测试作为退出条件；
- 不以未经测量的准确率或主观总质量分作为发布宣传；
- local-first、确定性和故障隔离是所有版本的兼容性原则；
- 新能力不能绕过产品规格中的非目标。

## 2. v0.1：CPU 基础版

### M0：范围与契约

目标：

- 冻结产品边界；
- 定义目标架构、报告模式和路线图；
- 明确 CPU/AI 边界、隐私原则和错误模型。

退出条件：

- `docs/product-spec.md` 明确
  输入 → 分析 → Finding → JSON/HTML 报告；
- 报告模式要求每个 Finding 有时间区间、严重程度、分数、证据和
  解释；
- `detector_error` 和确定性规则已写入规范。

### M1：项目骨架与媒体契约

目标：

- 建立 Python 包、CLI 和配置模式；
- 集成 ffprobe/FFmpeg；
- 实现规范化媒体模型和时间轴；
- 建立报告模型校验。

退出条件：

- 有效视频可被探测并建立真实时间轴；
- 无效输入产生结构化失败；
- 单元测试不依赖网络或大型模型。

### M2：镜头与基础检测器

目标：

- 镜头边界；
- 黑屏/近黑屏；
- 冻结/卡帧/连续近重复帧；
- 场景内相对模糊；
- 排除切镜后的全局亮度闪烁；
- 元数据检查。

退出条件：

- 每个检测器有人工合成的正例、负例和边界测试；
- Findings 满足报告模式；
- 闪烁检测的切镜保护窗口有专门回归测试；
- 不发布未经 Benchmark 支撑的准确率数字。

### M3：报告与故障隔离

目标：

- Finding 内容、ID 与排序确定的版本化 JSON；
- 离线 HTML 时间轴与证据展示；
- 稳定证据提取；
- 检测器部分失败处理。

退出条件：

- 相同输入和配置重复运行得到稳定结果；
- HTML 断网可用且无外链；
- 注入一个检测器错误仍生成 `partial` 报告；
- JSON 与 HTML 的 Finding 和检测器状态一致。

### M4：Benchmark、打包与发布

目标：

- 自动化 Benchmark；
- 跨平台测试；
- CPU 性能和资源边界记录；
- 安装、使用、隐私和故障排查文档；
- v0.1 发布包。

退出条件：

- 产品规格中的所有 v0.1 验收项通过；
- 支持的平台有可重复安装验证；
- 发布包默认不联网、不下载模型；
- 已知限制和 Benchmark 条件被公开记录。

## 3. v0.2：可选语义与工作流扩展

候选能力：

1. 文本提示词与视频内容匹配度；
2. 场景内视觉语义漂移；
3. 视频中文字的跨帧稳定性；
4. 可插拔的本地或远程 VLM 诊断器；
5. 本地 Web 界面；
6. 批量分析和模型对比。

进入 v0.2 前必须先定义：

- 新 Finding 类型和分数语义；
- AI 模型、版本和配置的可追溯方式；
- 远程插件的数据传输确认机制；
- 批量报告如何避免产生未经校准的总分；
- Web 界面的本地访问和文件边界。

这些能力必须保持可选，不能成为打开 v0.1 CPU 报告的前置条件。

## 4. 持续非目标

除非未来产品规格经过明确变更，VideoScope 不规划：

- 视频生成；
- 自动修复或替用户改写视频；
- 人脸身份识别或真实人物身份判断；
- 声称准确判断复杂物理规律；
- 未经校准的统一总质量分；
- 默认上传本地输入；
- 默认下载大型模型。

## 5. 质量门槛

每个发布版本都必须满足：

- 报告模式向后兼容或有明确迁移说明；
- 同一确定性边界内结果可复现；
- 检测器失败可见且不伪装为零 Finding；
- 所有 Finding 可定位并有证据；
- 默认离线和本地处理；
- Benchmark 条件、数据范围和限制透明；
- 不虚构准确率、性能或尚未实现的功能。

## 6. 主要技术风险与跟踪方向

| 风险 | 影响 | v0.1 跟踪方向 |
| --- | --- | --- |
| FFmpeg 构建差异 | 跨平台逐帧结果变化 | 记录版本，维护跨平台固定样例 |
| 可变帧率与坏时间戳 | 区间定位偏差 | 使用展示时间戳并增加异常样例 |
| 静态镜头与冻结歧义 | 误报 | 使用持续时间、近重复变化和解释性指标 |
| 艺术模糊与异常模糊歧义 | 误报 | 只做镜头内相对判断，公开阈值 |
| 镜头检测误差传播 | 闪烁/模糊结果偏差 | 共享边界、保护窗口和联合测试 |
| 长视频资源消耗 | 运行时间或内存过高 | 流式处理、缓存上限、性能 Benchmark |
| 证据过多 | 输出目录和 HTML 过大 | 确定性代表帧与每 Finding 上限 |
| 插件访问网络或敏感数据 | 隐私风险 | 显式启用、能力声明、默认禁用 |
| 检测器异常 | 整体报告不完整 | `detector_error`、部分成功和故障注入 |

风险表描述仍需持续验证的问题，不代表已被完全解决。

## Implemented optional AI milestones

- Shared lazy model runtime, batching, local embedding cache, and explicit
  download policy: implemented.
- Optional OpenCLIP prompt/frame similarity: implemented with descriptive and
  user-threshold modes.
- Optional within-scene visual semantic drift: implemented with OpenCLIP or
  DINOv2 image embeddings and scene-relative baselines.
- Optional within-scene temporal text stability: implemented with a shared
  OCR provider contract, lazy Chinese/English PaddleOCR adapter, FakeOCR tests,
  and evidence-box rendering.
- Optional local Web API: implemented with shared-pipeline jobs, SSE progress,
  bounded CPU/model worker pools, cooperative cancellation and local retention.
- Optional local React dashboard: implemented with typed API access, resilient
  SSE progress, interactive Finding timeline, local video review, and packaged
  production assets.

These additions do not change the complete CPU v0.1 installation. Real-world
calibration, broader annotated evaluation, and VLM plugins remain future work.
