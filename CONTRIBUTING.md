# Contributing to VideoScope

感谢参与 VideoScope。项目仍处于 v0.1 工程建设阶段；贡献前请先阅读
`AGENTS.md`、`docs/product-spec.md`、`docs/architecture.md`、
`docs/roadmap.md` 和 `docs/report-schema.md`。

## 1. 开发环境准备

最低要求：

- Python 3.11 或更高；
- Git；
- `ffmpeg` 和 `ffprobe`；
- CPU-only 环境可以运行基础功能和基础测试；
- Node.js 仅是未来 Web 工作的可选依赖。

先执行各平台检查：

```text
python --version
python -m pip --version
git --version
ffmpeg -version
ffprobe -version
```

Windows 如果使用 Python Launcher，也可以运行 `py -0p`。完整的
跨平台环境检查见 `docs/environment.md`。

在仓库中创建独立虚拟环境：

```text
python -m venv .venv
```

激活方式：

- Windows PowerShell：`.\.venv\Scripts\Activate.ps1`
- Linux/macOS：`source .venv/bin/activate`

安装 editable 开发版本：

```text
python -m pip install -e ".[dev]"
```

依赖以 `pyproject.toml` 和 README 为准。基础安装不会下载 AI 模型或
测试素材；synthetic fixtures 由本机 FFmpeg 按需生成。

## 2. 分支和提交规范

从最新目标分支创建短生命周期分支。建议前缀：

- `feat/`：新功能；
- `fix/`：缺陷修复；
- `docs/`：文档；
- `test/`：测试或 fixture；
- `refactor/`：不改变行为的重构。

一次提交只表达一个逻辑变更。提交信息使用命令式、可搜索的形式，
例如：

```text
feat: add black segment detector
fix: preserve timestamps for variable frame rate input
docs: explain offline evidence handling
```

不要提交虚拟环境、日志、私密视频、模型权重、生成报告或未经许可的
第三方数据。自动化代理只有在任务明确要求时才能 commit 或 push。

## 3. 测试、静态检查和类型检查

统一验证入口为：

```text
python scripts/validate.py
```

它将统一运行以下检查：

```text
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src tests
```

`scripts/validate.py` 委托给 `scripts/verify.py`，依次运行 Ruff lint、
Ruff 格式检查、mypy 和 pytest。只有实际命令成功时才能声明验证通过。

测试要求：

- 基础测试必须离线且 CPU-only；
- 网络访问必须被阻止或替换；
- 测试不能自动下载模型或素材；
- 临时路径必须覆盖空格、中文和非 ASCII 字符；
- 结果必须来自真实输入计算，不能按 fixture 名称或路径写死；
- 确定性测试应重复运行并比较规范结果和证据选择；
- 故障注入应验证单 Detector 失败产生 `detector_error`，而不是丢失
  整个报告。

## 4. 新增 Detector

新增 Detector 前先确认它属于 `docs/product-spec.md` 的当前范围。
不属于 v0.1 的能力必须先讨论产品和报告契约，不能直接实现。

一个 Detector 变更至少包含：

1. 实现 `Detector` 协议，声明稳定 `id`、显示名称、版本、说明和
   `default_enabled`；
2. 声明 `DetectorRequirements`，如实标记提示词、GPU、网络、可选包和
   成本类别；
3. 提供独立 Pydantic `config_model`，包含所有阈值、窗口和默认值；
4. 实现 `analyze(context, config) -> list[Finding]`，只从
   `AnalysisContext` 读取输入、抽样帧、镜头和显式共享产物；
5. 确保每个 Finding 的 Detector ID 和版本与插件清单一致；
6. 在内置注册表中显式注册；测试替身不得注册到生产注册表或 CLI；
7. 添加正例、负例、边界、空结果、配置错误和故障隔离测试；
8. 添加确定性及含空格、中文路径的测试；
9. 记录可观察现象、启发式限制、分数语义和必要文档；
10. 运行 `python scripts/validate.py`。

当前版本只支持显式注册的内置 Detector，不加载 Python entry points。
第三方 entry point 发现属于未来扩展，加入前必须定义兼容版本和启用
策略。Runner 采用确定性串行执行；Detector 不得自行并行调度其他
Detector。

Detector 不得直接依赖其他 Detector。共享特征应由独立
`feature_provider` 产生，通过编排器声明并注入。Detector 不得直接
写 JSON 或 HTML，也不得访问网络或隐式加载 AI 模型。

镜头切换是上下文，不是质量错误。使用镜头信息的 Detector 必须测试
切换邻域，尤其不能把切镜后的亮度跳变误报为闪烁。

## 5. 报告误报和漏报

提交误报或漏报问题时，请提供：

- VideoScope、Detector、FFmpeg 和 ffprobe 版本；
- 操作系统和 CPU 架构；
- 完整生效配置或其脱敏摘要；
- Finding 类型、时间区间和 Finding ID；
- 预期行为及其可观察依据；
- `report.json` 的最小脱敏片段；
- 是否可以用人工合成的短视频复现。

不要只写“结果不准”。也不要把启发式结果描述成已证明的事实。
维护者应把误报、漏报和无法判断分别记录，不得为了让 Benchmark
好看而重写标签或隐藏失败样例。

## 6. 隐私安全的问题样例

默认不要上传原始问题视频。优先选择：

1. 用 FFmpeg 或其他本地工具生成不含个人信息的最短合成样例；
2. 只提交脱敏的媒体元数据、配置和必要指标；
3. 裁剪到最小时间范围，并确认画面、音频、文件名和元数据均无隐私；
4. 用占位帧复现算法边界；
5. 私下保留原文件，只提交内容摘要以支持重复确认。

提交前检查人脸、声音、屏幕内容、地址、账号、地理位置、文件路径、
提示词和嵌入式元数据。公开 Issue、Pull Request 和 CI 日志都应视为
可能永久公开。需要私密披露渠道但仓库尚未提供时，先只描述问题，
不要自行上传敏感素材。

## 7. 许可证和第三方模型权重

VideoScope 源代码采用 Apache License 2.0，完整条款见根目录
`LICENSE`。除非贡献中另有明确且获维护者接受的说明，提交的代码和
文档应可按同一许可证分发。

第三方代码、数据、字体、媒体、模型和权重各自受其许可证约束：

- 在引入前记录来源、版本、许可证和再分发条件；
- 不得把模型代码许可证等同于模型权重许可证；
- 不得提交来源不明、禁止再分发或用途受限却未披露的权重；
- 基础安装不得自动下载权重；
- 可选权重必须由用户显式选择，并在文档中说明存储、校验、数据流向
  和许可要求；
- 测试优先使用自有的微型 fake provider，不把真实模型权重提交到
  仓库。

许可证存在疑问时，停止引入该资产并在评审中提出，不要猜测。

## 8. 新增 Advanced AI Provider

新增 ASR 或结构化内容智能 Provider 时，必须实现
`videoscope.intelligence.protocols` 中的对应协议，并通过共享
`ModelRuntimeManager` 注册和延迟加载。不得在模块导入阶段导入重型运行库、
探测 GPU、访问网络或下载模型。

每个真实 Provider 必须同时提供：

- 独立 optional extra 和缺失依赖时的可操作错误；
- 无网络、无权重、可控输出的 Fake Provider；
- 明确的 provider/model/device/precision 运行记录；
- 输入大小、输出大小、超时和并发上限；
- 下载许可、缓存键、卸载和失败隔离测试；
- 模型与权重许可证、数据流向和隐私说明；
- grounding 失败、畸形输出、取消和同一运行时共享测试。

语义 Provider 的输出必须经过严格 Pydantic 契约和来源证据校验。不能把模型
输出直接写成 C 范围，更不能绕过人工 review、私有预览、计划摘要确认、来源
映射和输出后验证。真实模型集成测试必须标记为 optional；基础 CI 只使用 Fake
Provider。评估方法见 `docs/advanced-ai-evaluation.md`。

## 9. Pull Request 检查清单

- [ ] 变更属于明确任务和当前产品范围；
- [ ] 没有隐藏网络访问、模型下载或 GPU 依赖；
- [ ] 外部命令使用参数数组且没有 `shell=True`；
- [ ] 新 Detector 有独立配置和单元测试；
- [ ] Findings 符合报告模式，文案没有把启发式写成事实；
- [ ] 空格、中文和跨平台路径得到覆盖；
- [ ] 实际运行了统一验证，或如实说明脚本尚不可用；
- [ ] 没有伪造 Benchmark、准确率、日志、截图或测试结果；
- [ ] 没有提交私密视频、个人路径或受限模型权重。
