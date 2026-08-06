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

## 7. A：Publish Ready

v0.3 是在保留 v0.1 Check 的分析契约和 `videoscope analyze` 行为前提下，单独
演进的 opt-in 本地处理线。它保持基础安装和测试离线、CPU-only，不把处理依赖
引入默认诊断路径。

### R0：Publish Ready contract

- 定义版本化 Profile、PublishPlan、确认、产物、变更记录、复检和隐私契约；
- 明确源视频只读，成功输出为独立的 `publish-ready.mp4`；
- 固定公开 JSON 的输出根目录相对路径和正斜杠表示；
- 保持 `AnalysisReport` 和 `videoscope analyze` 不变。

退出条件：正式产品、架构、路线图和 Resolve schema 文档一致，且没有将
Publish Ready 描述为 v0.1 能力或全局质量评分。

### R1：A Publish Ready MVP

- 仅实现 `compatible_mp4`、`social_vertical_9_16` 和
  `social_horizontal_16_9` 三个 Profile；
- 竖屏和横屏使用 scale-and-pad 保留完整画面，不自动裁剪；
- 实现元数据剥离、fast-start、预览、代表性封面、变更记录和输出后复检；
- 只通过本地原生 FFmpeg/ffprobe 的参数数组执行，且不随仓库或发行物分发
  FFmpeg 二进制文件或 WASM。

退出条件：成功产物通过版本化 Profile 验证；验证不通过时状态只能是
`needs_review` 或 `failed`，不能显示为完成或 Publish Ready。

R1 不加入远程后端、网络、GPU、AI、模型下载、人脸或身份识别、自动裁剪、片段
删除、插帧、稳定、音乐或生成式增强。这些能力需要后续独立范围变更。

## 8. D：Safe Sharing CPU MVP

Safe Sharing 是 Publish Ready 之后的独立 Resolve 交付线；它不扩展 v0.1 Check、
`AnalysisReport` 或 `videoscope analyze`。完整的版本化数据契约在
[`docs/privacy-schema.md`](privacy-schema.md)，产品和架构边界必须与该文档同步。

### S0：正式范围、隐私模型与 JSON

- 定义私有 `PrivacyRiskMap`、审核决定、有效配置、计划、公开产物、变更记录和验证
  报告；
- 为风险和计划提供确定性 identity/digest，拒绝额外字段与不安全路径；
- 使用 UTF-8、稳定排序和原子 JSON 写入；
- 让私有证据停留在私有复核区，公开摘要和分享包不包含原始敏感文本或绝对个人路径；
- 保持基础测试离线、CPU-only、无模型、无网络。

退出条件：模型测试验证 ID、归一化区域、审核边界、公开摘要去私有证据、路径安全、
JSON 往返与 Unicode 原子替换；既有 v0.1 schema 测试保持不变。

后续 S1 及其后的增量将分别处理元数据与人工风险、CPU 匿名区域建议、可选 OCR、
审核计划和确认、渲染、验证、CLI/API 与工作台。任何扫描器失败必须可见，不能伪装
为零风险；任何无法通过的必需隐私检查均不能产生 `completed` 结果。

## 9. B：Video Rescue Balanced CPU MVP

Video Rescue 是 Publish Ready 与 Safe Sharing 之后的独立 Resolve 开发线。它不扩展
v0.1 Check、`AnalysisReport` 或 `videoscope analyze`，并保持基础安装和测试离线、
CPU-only、无 GPU、无模型和无网络。完整版本化契约见
[`docs/rescue-schema.md`](rescue-schema.md)。

### V0：范围、领域模型与 canonical JSON

- 定义路径安全、版本化的损坏映射、动作、计划、确认、产物、变更记录和独立验证报告；
- 将确认绑定到完整计划摘要，源视频保持只读；
- 使用 UTF-8、稳定排序、禁止 NaN/Infinity 与同目录原子 JSON 替换；
- 保持 faithful 与 improved 产物、验证状态和限制独立，不产生总体质量或恢复分数；
- 不改变现有 v0.1、Publish Ready 或 Safe Sharing 行为。

退出条件：模型测试验证确定性 damage ID、反向时间拒绝、过期摘要拒绝、相对路径安全，
以及 Unicode 目录中的 JSON 原子往返；冻结的报告 schema 测试保持不变。

## 10. C：Long Video to Useful Content CPU MVP

C 在稳固底座、A Publish Ready、D Safe Sharing 和 B Video Rescue 全部门禁之后
实施。它不修改 v0.1 Check、`AnalysisReport` 或 `videoscope analyze`，并保持
基础路径离线、CPU-only、无 GPU、无模型和零默认上传。

### C0：公开契约与领域模型

- 统一公开 A/B/C/D 命名，同时保持既有序列化兼容；
- 建立 strict `videoscope.content` 模型、canonical JSON、确定性 ID 和计划摘要；
- 固定私有 `content-review-private/` 与公开 `content-output/` 边界；
- 定义本地 SRT/WebVTT 证据的校验、哈希和隐私规则。

### C1：内容地图与三种故事板

- 复用探测、镜头、抽帧、静音/响度、近黑和重复帧等只读结构特征；
- 实现 Faithful Clean、Chaptered Full 和 Selected Clips；
- 实现锁定区间、上下文保护、目标时长非强制约束、来源顺序和显式重排警告；
- 不产生未经校准的重要性、内容质量或传播潜力总分。

### C2：预览、执行、来源映射与独立验证

- 为每个内容改变动作生成有界私有剪接预览；
- 将确认绑定到完整故事板、锁、计划、预览、配置和输入哈希；
- 使用本地 FFmpeg 参数数组流式生成新媒体，不修改来源；
- 为每个输出区间写入精确 `source-map.json`；
- 独立验证解码、时长、流、锁、顺序、映射、剪接回归、音频连续性、A/V 残差、
  public allowlist 和来源哈希。

### C3：CLI、Web、真实媒体门禁与分发

- CLI 和 loopback Web API 复用同一 pipeline；
- 中英双语工作台支持键盘编辑、移动端复核、恢复、取消和删除；
- 确定性本地 fixtures 覆盖会议、教程、锁定内容和剪接回归；
- clean base wheel 在无 AI、OCR、GPU、网络和模型下载条件下运行三个目标；
- 构建、分发审计和真实媒体门禁全部通过后才可进入 Advanced AI。

## 11. v0.7：Advanced AI

### AI0：契约、隐私和共享运行时

- 独立智能建议 schema、确定性 ID、canonical JSON 和私有/公开边界；
- ASR 与结构化内容智能协议、Fake provider、共享懒加载和下载控制；
- provider 失败不得破坏 C CPU 准备结果。

### AI1：本地转写与语义建议

- 可选 Faster Whisper 本地转写；
- loopback-only 本地语义 provider；
- 章节、精华、摘要和标题建议；
- 严格 grounding、证据引用和限制说明。

### AI2：人工复核与 C bridge

- 中英双语接受、拒绝和编辑；
- 只把已接受的章节/精华转换为普通 C 用户范围；
- 保留 C 的预览、确认、来源映射与独立验证门禁。

### AI3：评测、分发和发布

- Fake provider 默认 CI、可选本地模型测试和 clean-wheel smoke；
- 授权真实视频的人工 rubric 与 held-out 评测说明；
- 不发布未测量的准确率、节省时间或传播效果承诺；
- 公共处理服务必须另行通过认证、配额、留存、滥用防护和隐私审计。
