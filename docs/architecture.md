# VideoScope 架构

状态：v0.1 已实现架构

## 1. 架构目标

VideoScope v0.1 采用 local-first、CPU-first、detector-oriented 架构。
核心目标是让每个诊断结论可定位、可解释、可复现，并让局部检测器
失败不会破坏整份报告。

## 2. 系统边界

### 2.1 系统内

- CLI 参数解析与配置解析；
- 本地媒体探测和解码；
- 镜头边界与基础帧指标计算；
- 独立检测器运行；
- Finding 规范化、校验和稳定排序；
- 证据帧提取；
- JSON 与离线 HTML 渲染；
- Benchmark 和自动化测试入口。
- 可选 AI provider 协议、共享模型运行时和本地 embedding 缓存。

### 2.2 系统外

- 视频生成和修复；
- 人脸或真实人物身份判断；
- 复杂物理规律判断；
- 未校准总质量分；
- 默认网络服务、上传或模型下载；
- v0.2 才规划的语义和 Web 能力。

## 3. 处理流水线

```text
CLI
 └─ 输入与配置校验
     └─ 媒体探测（ffprobe）
         └─ 解码与统一帧时间轴（FFmpeg）
             ├─ 帧级基础特征缓存
             └─ 镜头边界
                 └─ 检测器编排
                     ├─ 元数据检查
                     ├─ 黑屏区间
                     ├─ 冻结/近重复帧
                     ├─ 场景内相对模糊
                     └─ 切镜排除后的全局亮度闪烁
                         └─ Finding 规范化与校验
                             ├─ 证据提取
                             ├─ report.json
                             └─ report.html
```

镜头边界是下游场景内检测器的共享上下文，不作为某个可选检测器的
隐式副作用。全局亮度闪烁检测必须读取镜头边界和切镜保护窗口。

## 4. 组件职责

### 4.1 CLI

- 接收单个本地视频、可选提示词、配置和输出目录；
- 验证参数但不实现检测逻辑；
- 使用稳定退出码区分成功、部分成功、输入失败和内部失败；
- 默认不联网。

### 4.2 配置解析器

- 合并内置默认值、配置文件和 CLI 覆盖；
- 生成完整、显式、可序列化的最终配置；
- 拒绝未知键和无效阈值；
- 计算配置摘要，参与确定性运行标识。

### 4.3 媒体探测器

- 通过 ffprobe 获取容器与流元数据；
- 将外部工具输出转换为内部规范模型；
- 验证视频流、时长、时间基和解码前提；
- 不把 ffprobe 的绝对文件路径或未脱敏 stderr 直接写入报告。

### 4.4 解码与时间轴层

- 使用 FFmpeg 以展示时间戳为基准读取帧；
- 为可变帧率视频保留真实帧时间，不假定固定 FPS；
- 提供按时间访问帧、提取证据和计算共享特征的统一接口；
- 限制内存占用，避免要求一次载入完整视频。

### 4.5 共享分析层

- 计算亮度、模糊度、帧间差异等可复用的确定性特征；
- 生成镜头边界和镜头区间；
- 缓存键包含输入摘要、算法版本和完整配置；
- 缓存不存在或损坏时可安全重建。

### 4.6 检测器编排器

- 根据依赖关系以稳定顺序调用检测器；
- 给每个检测器传入只读上下文；
- 单独捕获超时、配置错误和运行异常；
- 把失败转换为 `detector_error`，继续执行无依赖的检测器；
- 汇总结果，但不计算总质量分。

### 4.7 Finding 规范化器

- 校验 Finding 必需字段和时间边界；
- 合并同一检测器定义下的相邻区间；
- 按开始时间、结束时间、检测器 ID、Finding ID 稳定排序；
- 生成基于内容的稳定 ID；
- 拒绝缺少证据或解释的 Finding。

### 4.8 证据存储

- 使用固定规则选择代表帧或帧对；
- 文件名来自稳定 Finding ID 和证据序号；
- 只使用输出目录内相对路径；
- 写入前后校验证据与时间戳对应关系。

### 4.9 报告渲染器

- JSON 渲染器输出规范数据，是事实来源；
- HTML 渲染器只消费已校验 JSON 模型，不重新执行检测；
- HTML 内置 CSS 和必要脚本，不加载 CDN、字体或分析服务；
- 两种报告必须表达相同的 Findings 和检测器状态。

### 4.10 Benchmark 与测试运行器

- 从显式清单加载本地样例和区间标注；
- 固定工具版本、配置摘要和样例内容摘要；
- 输出机器可读结果，不硬编码准确率承诺；
- 分离正确性回归、契约测试和性能测量。

## 5. 插件架构

### 5.1 插件类型

v0.1 预留以下扩展点：

- `detector`：产生 Findings；
- `feature_provider`：产生可复用特征；
- `report_renderer`：从规范报告模型生成额外格式。

核心发行版只依赖内置 CPU 插件。v0.1 CLI 不需要插件市场或自动下载。

### 5.2 插件清单

`Detector` 协议要求插件声明：

- 唯一 `id`、`display_name`、`version` 和 `description`；
- `default_enabled`；
- 独立 Pydantic `config_model`；
- `DetectorRequirements`，包含是否需要提示词、GPU、网络、可选包以及
  `low`、`medium` 或 `high` 成本类别。

这些字段是运行时可检查的插件清单。阈值和算法参数只能出现在独立配置
模型中，不能散落在实现代码里。

### 5.3 检测器契约

检测器通过：

```text
analyze(context: AnalysisContext, config: BaseModel) -> list[Finding]
```

接收只读的运行上下文、显式配置和已声明依赖的产物。`AnalysisContext`
包含输入路径与哈希、视频元数据、可选提示词、抽样帧、镜头列表、工作
目录、显式共享缓存和可选取消回调。检测器只返回：

- 零个或多个符合报告模式的 Findings；

执行状态和耗时由 `DetectorRunner` 记录，不能由检测器伪造。Runner 还会
校验 Finding 的 `detector_id`、`detector_version` 和返回类型。

检测器不得：

- 修改源视频或其他检测器结果；
- 直接写最终报告；
- 隐式访问网络；
- 生成跨检测器总分；
- 把异常吞掉后伪装成“无问题”。

### 5.4 发现与启用

- 内置插件通过 `detectors/builtins.py` 中的固定清单显式加入
  `DetectorRegistry`，重复 ID 拒绝启动；
- 注册表查询和默认列表按 ID 排序；
- 第三方插件必须由用户显式安装并在配置中启用；
- Python entry points 是未来扩展点，v0.1 不扫描或加载第三方 entry
  points；
- 插件加载顺序不影响输出排序；
- 未启用插件不会被导入、下载或执行；
- 报告记录所有已请求插件及其状态。

### 5.5 故障隔离

`DetectorRunner` 按显式顺序或注册表 ID 顺序串行执行。v0.1 不并行运行
检测器，保证相同配置下的执行与 Finding 排序可复现。单个检测器或其
配置校验失败时，Runner 继续执行其他检测器并记录：

- `status: "detector_error"`；
- 异常类型；
- 脱敏、可操作的错误消息；
- 受影响的检测器 ID、耗时和零 Findings。

Runner 只捕获普通 `Exception`，不会捕获 `KeyboardInterrupt` 或
`SystemExit`。异常消息不得保留输入路径、工作目录、令牌、密码或密钥。

其他无依赖检测器继续运行。未来可增加进程级隔离，但不是 v0.1
发布的必要前提。

### 5.6 共享可选 AI 模型运行时

`videoscope.ai` 提供共享基础设施，并惰性注册可选的 OpenCLIP provider。
`prompt_alignment` 只存在于显式 AI detector profile 中。基础包导入、
默认 CPU 分析和基础 CI 不导入 torch、OpenCLIP、DINOv2 或 PaddleOCR。

`ModelProvider` 声明稳定的 `provider_id`、`model_id`、解析后的 device
和 precision，并提供显式 `load()`、`unload()` 与无副作用 `health()`。
`EmbeddingProvider` 在此基础上提供批量 `encode_images()` 和
`encode_text()`，统一返回二维 NumPy float 数组和 JSON 兼容 metadata。

`ModelRuntimeManager` 是 provider 生命周期的唯一所有者：

- provider factory 注册本身不构造或加载模型；
- 第一次真正缺少 embedding 时才解析 CPU/CUDA/auto 并调用 `load()`；
- 同一 provider、模型、device 和 precision 只保留一个已加载实例；
- batch size、precision、device 和内存预算来自集中配置；
- detector 只能从 `AnalysisContext.shared_cache["model_runtime"]` 获取
  已注入的 Manager，不能自行实例化 provider；
- 缓存全命中时不需要加载模型；
- 每次 encode 记录 provider、model、device、precision、batch size、
  推理时间、命中数和命中率。

帧 embedding 使用内存 LRU 和可选磁盘缓存。缓存键至少包含：

```text
video SHA-256
+ frame timestamp (IEEE-754 hex)
+ provider ID
+ model ID
+ preprocessing version
```

键和文件名不包含原视频文件名或个人路径。磁盘内容是从视频派生的
特征，仍按敏感数据处理；缓存损坏时应视为 miss 并重建。

模型下载权限在 Manager 调用 provider `load()` 前统一检查。已有本地
缓存可以默认使用；若 provider 声明本地文件缺失：

- 交互调用必须通过显式确认 callback；
- 非交互调用必须配置 `allow_model_download=true`，对应 CLI
  `--allow-model-download`；
- 未授权时抛出明确错误，不调用 `load()`，也不创建网络连接。

当前正式 provider 清单包含 OpenCLIP 的
`ViT-B-32/laion2b_s34b_b79k` 惰性注册项。实际权重只有在
`--enable-ai` 后被请求，且通过下载权限检查后才加载。
`FakeEmbeddingProvider` 只用于离线测试和故障注入。

`prompt_alignment` 为每个 scene 选择多个代表帧，并一次性向共享
Manager 请求帧 embedding。默认 `descriptive` 模式只在
`runtime.detector_diagnostics` 记录相似度曲线和最低场景；只有用户
显式配置 `threshold` 模式和阈值时才生成 Finding。无 prompt 时 runner
记录 `skipped`，provider 异常记录 `detector_error`，均不删除 CPU
Findings。

## 6. CPU 基础版与 AI 插件边界

| 能力 | CPU 基础版 v0.1 | 可选 AI 插件 |
| --- | --- | --- |
| 元数据、镜头、黑屏、冻结、模糊、闪烁 | 必须 | 不需要 |
| GPU | 不需要 | 可选 |
| 大型模型 | 不下载、不需要 | 显式安装 |
| 网络访问 | 默认禁止 | 显式授权 |
| 提示词匹配 | 不包含 | OpenCLIP 可选 |
| 语义漂移 | 不包含 | OpenCLIP 或 DINOv2 可选 |
| OCR 文字稳定性 | 不包含 | PaddleOCR 可选 |
| 本地 Web API/Dashboard | 不需要 | FastAPI/React 可选 |
| 报告契约 | 完整支持 | 必须兼容 |
| 失败影响 | 基础报告可部分成功 | 不得破坏基础结果 |

可选依赖组边界：

- `genvideoscope[ai]`：OpenCLIP 与 DINOv2 本地 embedding 运行库；
- `genvideoscope[ocr]`：PaddleOCR 本地文字识别运行库；
- `genvideoscope[web]`：本地 FastAPI 与 Dashboard 服务运行库；
- `genvideoscope[all]`：以上可选运行库的并集。

安装 extra 只安装 Python 运行库，不授权下载模型，不启用 detector，
也不改变默认 CPU 分析路径。

发行 distribution 使用 `genvideoscope` 以降低与无关项目的名称混淆；
Python import 和 CLI 继续使用稳定的 `videoscope`。

## 7. 数据与时间模型

- 时间统一使用非负有限浮点秒；
- 区间使用半开区间 `[start_seconds, end_seconds)`；
- 帧保留解码得到的展示时间戳；
- 可变帧率视频不通过帧序号反推时间；
- 镜头区间有序、不重叠并覆盖可分析时间范围；
- 视觉 Finding 必须落在视频时长内；
- 报告不写绝对输入或输出路径。

## 8. 确定性设计

确定性边界由输入内容摘要、输入文件名、工具版本、插件版本和完整
配置共同定义。实现必须：

- 禁用未固定种子的随机选择；
- 对浮点数使用规定精度和舍入方式；
- 为相同分数规定时间优先等固定择优规则；
- 稳定排序镜头、Findings、证据和检测器记录；
- 使用规范化内容生成 Finding ID 和证据文件名；`analysis_id` 可以
  随运行变化；
- `created_at` 和 `analysis_id` 作为显式运行信封字段；确定性比较
  忽略它们以及耗时、临时路径和进程信息；
- 在并行执行时先收集，再按规范顺序序列化。

## 9. 错误模型

### 9.1 全局错误

输入不存在、无法读取、没有视频流、媒体探测完全失败或输出不可写，
会使报告状态为 `failed`，CLI 返回非零退出码。只要输出目录可写，
仍应写出描述失败原因的最小 JSON 报告。

### 9.2 检测器错误

单个检测器失败时报告状态为 `partial`，并记录
`detector_error`。成功结果不得丢失，也不得把失败解释为检测器返回
零个 Findings。

### 9.3 渲染错误

JSON 是首要产物。HTML 渲染失败时保留已成功写入的 JSON 和证据，
CLI 返回非零退出码，并明确指出 HTML 产物不完整。

## 10. 隐私与安全

- 默认不创建网络连接；
- 外部命令使用参数数组，不拼接 shell 字符串；
- 把媒体和插件输出视为不可信输入；
- 输出路径必须限制在用户选择的输出目录；
- HTML 对动态文本转义，并采用离线资源；
- 错误报告过滤用户名、绝对路径、环境变量和密钥；
- 源文件以只读方式处理。

## 11. 可测试性

组件之间使用明确模型边界，使测试可替换媒体探测、解码器、检测器
和文件写入。至少需要：

- 报告模式与序列化快照测试；
- 人工合成短视频的检测器正例、负例和边界测试；
- 可变帧率与异常元数据测试；
- 切镜后亮度变化排除测试；
- 检测器故障注入测试；
- 同输入同配置重复运行确定性测试；
- 离线 HTML 完整性和无外链测试。

## 12. 关键架构决策

1. JSON 规范模型是单一事实来源，HTML 只负责展示；
2. 先生成统一时间轴和镜头上下文，再运行下游检测器；
3. 每个检测器独立评分，不生成总质量分；
4. CPU 基础能力覆盖全部 v0.1，AI 永远是显式可选插件；
5. 检测器错误采用部分成功报告，而不是整次分析失败；
6. 时间使用浮点秒和半开区间，兼容可变帧率；
7. 报告默认排除易变字段和个人绝对路径；
8. v0.1 预留插件契约，但不实现自动下载或插件市场。

## 13. 已知技术风险

- 不同 FFmpeg 构建的解码细节可能影响逐帧数值；
- 可变帧率、损坏时间戳和异常容器会增加时间轴归一化难度；
- 静态镜头与真正冻结、艺术性模糊与异常模糊之间存在歧义；
- 镜头边界误差会传递到模糊和闪烁检测；
- 长视频的 CPU 时间、内存和证据存储需要明确上限；
- 第三方插件的确定性和隐私声明需要运行时约束；
- embedding 缓存可能包含可关联原视频的派生信息，需要本地访问控制、
  容量上限和显式清理策略；
- CUDA 与半精度支持受 provider、驱动和硬件组合影响，`auto` fallback
  必须可观察且可测试；
- 单文件离线 HTML 与大量证据帧之间存在体积权衡；
- Windows、Linux、macOS 的字体和浏览器渲染可能产生视觉差异。

这些风险必须通过测试集、配置透明度和报告解释来管理，不能通过
虚构准确率或隐藏失败来规避。

## Step 17 addendum: optional visual consistency

The explicit AI profile registers both OpenCLIP and DINOv2 lazily.
`visual_semantic_drift` receives scene boundaries and the shared
`ModelRuntimeManager`; it never constructs a model provider itself. It compares
adjacent and longer-gap embeddings only within one scene and excludes guarded
boundary samples. Its scene-relative median/MAD baseline, distance series,
provider, model, and peaks are report diagnostics.

When two detectors request the same provider, model, preprocessing version,
video hash, and timestamp, the manager supplies one cached frame embedding.
Provider failure remains an isolated `detector_error`, leaving CPU results
intact. The detector is a heuristic visual-consistency plugin, not face
recognition, identity recognition, or person re-identification.

## Step 18 addendum: optional temporal OCR

`OCRProvider` extends the shared provider boundary with batched,
timestamp-preserving detection and recognition. `PaddleOCRProvider` is a lazy
Chinese/English local implementation under the `ocr` extra. The base package
and base CI do not import Paddle or download OCR models.

`text_stability` receives the shared runtime, sampled frames and scene
boundaries. It tracks text only inside each scene using box overlap, edit
similarity and temporal continuity. Normal single subtitle changes, scene
cuts, monotonic rolling text and isolated low-confidence OCR results are
excluded by explicit configuration gates. Provider errors remain isolated
`detector_error` records and do not remove CPU findings.

OCR evidence keeps normalized boxes and recognition metadata in the existing
`Evidence.metadata` extension point. The offline HTML renderer validates and
draws those boxes without changing the report schema or loading remote
resources.

## Step 19 addendum: optional local Web API

The `web` extra adds a FastAPI adapter around the existing
`AnalysisPipeline`. Upload handling, job state, SSE, retention and safe
artifact delivery live in `videoscope.web`; probing, sampling, detectors,
evidence and report rendering remain exclusively in the core pipeline.

Jobs use random IDs and application-data directories. Uploaded names do not
become paths, and resolved artifact paths must remain inside a completed job's
artifact root. CPU and optional-model work use separate bounded thread pools;
the default heavy-model pool has one worker. The Pipeline receives the job's
progress and cooperative cancellation callbacks.

The default server binds to `127.0.0.1` on an operating-system-selected port.
Non-loopback binding requires explicit CLI permission. No wildcard CORS,
accounts, external database, or cloud upload is included.

## Step 20 addendum: local React dashboard

The optional dashboard is a typed client of `videoscope.web`; it does not
reimplement media or detector logic. Vite produces static assets that are
copied into the Python package and mounted after all `/api`, `/docs`, and
OpenAPI routes. Development mode proxies only to the loopback API.

The dashboard keeps the random job ID in the URL, resumes lifecycle state
through the job endpoint, and reconnects SSE from the last event sequence.
Source video streaming remains bounded to the retained job input path, while
evidence and reports continue through the artifact containment checks.

## 14. A：Publish Ready architecture

本节为 v0.3 开发线增加可选的本地处理架构。它与 v0.1 Check 的诊断路径并存，
不修改 `AnalysisReport` 的任何字段，也不改变既有 `videoscope analyze` 行为、
安装依赖或离线 CPU 测试边界。

### 14.1 Publish Ready data flow

```text
input -> Check baseline -> PublishPlan -> confirmation -> native FFmpeg
      -> output Check -> VerificationReport -> artifact publication
```

处理器只在确认后运行。本地 FFmpeg/ffprobe 调用必须使用参数数组和
`shell=False`；不存在远程执行、GPU 或模型运行时。源文件只读，处理器只向任务
输出根目录写入独立产物。

### 14.2 Versioned artifacts

一个完成或可复核的 Publish Ready 任务在输出根目录中使用以下稳定相对路径：

```text
plan.json
preview/publish-preview.mp4
publish-ready.mp4
cover.jpg
changes.json
technical-report.json
analysis-before/report.json
analysis-after/report.json
```

`plan.json` 是确认前的版本化 `PublishPlan`，`changes.json` 是已执行动作的记录，
`technical-report.json` 承载版本化 `VerificationReport`。分析前后报告分别位于
`analysis-before/report.json` 和 `analysis-after/report.json`。JSON 报告只能引用
输出根目录内的相对路径，路径分隔符必须为正斜杠，且不得泄露输入、临时目录或
其他个人绝对路径。

### 14.3 Resolve lifecycle and CLI outcome

Publish Ready 生命周期固定为：

```text
created -> inspecting -> planning -> awaiting_confirmation -> processing -> verifying -> completed|needs_review|failed|cancelled
```

Publish CLI 使用如下退出码；这组退出码独立于既有分析命令的退出码：

```text
0   output exists and verification passed
2   input, profile, configuration, or confirmation error
3   FFmpeg/ffprobe could not process the media
4   internal orchestration or artifact failure
5   output exists but verification requires human review
130 user cancellation
```

验证失败绝不产生 `completed`；它只能进入 `needs_review`（输出存在但需要人工
复核）或 `failed`（无法安全发布可用结果）。

## 15. D：Safe Sharing privacy-domain contract

Safe Sharing 是独立于 v0.1 `AnalysisReport` 的 Resolve 领域。其核心模型位于
`videoscope.privacy`，不被既有检测器导入或调用；未来扫描器、计划器、执行器和
验证器仅通过这个版本化边界交换数据。详细的字段、验证、确定性 ID、摘要与 JSON
规则见 [`docs/privacy-schema.md`](privacy-schema.md)。

## 16. B：Video Rescue domain contract

Video Rescue 位于独立的 `videoscope.rescue` 包，与 v0.1 Check、Publish Ready 和
Safe Sharing 领域通过版本化模型而非隐式副作用交互。它的首个负载承重边界包括
`MediaDamageMap`、`RescuePlan`、`RescueConfirmation`、`RescueChangeLog`、独立的
`RescueVerificationReport` 与公开 `RescueTechnicalReport`。字段、枚举、路径验证、
确定性排序及 canonical JSON 规则定义于
[`docs/rescue-schema.md`](rescue-schema.md)。

后续扫描器、计划器、预览器、执行器和验证器只能通过这些模型传递路径安全、无个人
绝对路径的数据。源媒体只读；faithful 和 improved 产物独立验证，improved 失败不能
使已通过验证的 faithful 产物失效。该领域不导入模型运行时、不探测 GPU、也不改变
`AnalysisReport` 序列化。

Rescue 公共契约当前为 schema 0.2。0.2 为验证产物增加必填的
`artifact_role`，使终态消费者不必也不得通过文件名猜测 faithful 或 improved。
由于 0.1 没有足够信息安全重建该角色，核心读取器和 Web 作业恢复对 0.1 记录
fail-closed；不进行隐式升级或重标版本，需重新运行本地 Rescue 生成 0.2 记录。

Schema 0.2 also preserves action-ledger presence: a missing
`action_executions` field means unknown execution state, while an explicit
empty ledger means no executable action was recorded. Neither means every
action succeeded. Canonical writers emit the field and still reject unknown
fields. Confirmations bind the exact previewed action set; persisted previews
or confirmations without that binding require regeneration before execution.

```text
private input -> PrivacyRiskMap -> reviewed PrivacyPlan -> confirmation
              -> staged sharing copy -> PrivacyVerificationReport
              -> public PrivacyTechnicalReport + share-package
```

The final arrow is permitted only when the verification outcome is `completed`.
Every other terminal outcome removes the pending candidate and leaves the public
package empty; private preview evidence remains physically separate.

`PrivacyRiskMap` 是私有复核文档。风险中的 `private_evidence` 只能留在私有地图；
构造公开摘要时必须移除，公开技术报告和分享包均不得重新引入。所有公共产物路径
都要是输出根目录内的正斜杠相对路径。模型拒绝额外字段、非有限或负秒数、无面积
归一化框、无效审核决定和逃逸路径。

每个风险 ID 基于输入哈希、扫描器、风险类型、时间范围和可选归一化框生成；风险
地图以时间、严重程度、扫描器和 ID 固定排序。计划摘要覆盖输入哈希、Profile、有效
配置、已审核风险、动作及公开产物，不覆盖 `reviewed_at` 审计时间。所有 JSON 使用
UTF-8、未转义 Unicode、排序键、稳定数组、禁止 NaN/Infinity 和同目录临时文件的
原子替换。

## 17. C：Long Video to Useful Content architecture

C 位于独立的 `videoscope.content` 包，不扩展或改写 v0.1 `AnalysisReport`，也不
把 A、B 或 D 的终态报告当作可变存储。它复用经过版本化适配的只读媒体探测、镜头、
抽帧和可观察 CPU 特征，并产生自己的 `ContentMap`、`Storyboard`、
`ContentPlan`、`ContentConfirmation`、`ContentSourceMapping`、变更记录、独立验证
报告与技术报告。

```text
local source + optional local timed transcript
  -> deterministic content map
  -> Faithful Clean | Chaptered Full | Selected Clips storyboard
  -> user locks and edits
  -> bounded private join previews
  -> exact digest confirmation
  -> staged native FFmpeg execution
  -> independent verification
  -> atomic content-output publication
```

结构特征 provider 只能返回可观察证据、版本、有效参数和 warning，不能自行删除
内容或调用另一个 Detector。内容计划器是确定性的纯领域层：锁定保留区间优先，
静音本身不能自动触发删除，目标时长不能强迫不安全删减，默认保持来源顺序。

执行层只接受与来源哈希、可选字幕哈希、配置、锁定区间、故事板、预览身份和验证
策略完全匹配的确认。所有外部进程使用参数数组和 `shell=False`，源视频只读，媒体
先写入 staging。独立验证通过后，固定 allowlist 才能原子发布到
`content-output/`；私有 `content-review-private/` 永不通过公开 artifact 路由提供。

每个内容改变输出都必须有覆盖完整输出时间线的半开区间来源映射。必需验证失败为
`failed`，结论不充分为 `needs_review`；不能用执行成功标志代替验证。CLI 和本地
Web API 调用同一核心 pipeline。基础安装、基础测试和三种 CPU 工作流不导入 AI、
不探测 GPU、不访问网络且不下载模型。Advanced AI 是 C CPU 门禁之后的独立阶段。

## 18. Advanced AI architecture

Advanced AI 使用独立的 `videoscope.intelligence` 版本化领域，不修改 v0.1
`AnalysisReport`、Detector 协议或既有 A/D/B/C 序列化。共享
`ModelRuntimeManager` 仍是 provider 生命周期的唯一所有者；新的 ASR 和内容智能
provider 只增加能力协议，不复制模型实例、下载政策或设备解析。

```text
ContentMap + trusted transcript | optional local ASR
  -> bounded evidence request
  -> local structured intelligence provider
  -> schema validation + grounding audit
  -> private suggestion batch
  -> user review decisions
  -> accepted ranges as ordinary ContentUserRange values
  -> existing C preview / confirmation / execution / verification
```

模型不能直接持有 `LongVideoContentPipeline`，不能执行 FFmpeg，也不能写公开产物。
Grounding 层拒绝越界区间、未知 cue、缺少证据、超长文本、额外字段和非有限数字。
生成响应被规范化以后，建议 ID、排序、复核记录和 C bridge 必须确定；不同硬件上的
原始推理本身不宣称逐字节确定。

`FasterWhisperASRProvider` 和 loopback-only 的
`OllamaContentIntelligenceProvider` 属于可选运行库。基础安装只包含协议和 Fake
provider。真实权重不进入仓库或 wheel，非交互下载必须显式授权；Ollama provider
不执行 pull，也不接受非 loopback endpoint。

AI 私有复核树包含转写、证据请求、原始验证后建议和拒绝项。公共技术报告只包含
接受项的脱敏来源摘要、provider/model 标识、限制和执行状态，不包含原始 prompt、
完整字幕、用户路径或私有缩略图。
