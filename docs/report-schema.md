# VideoScope 统一报告数据结构

状态：v0.1 已实现领域模型与 JSON 序列化

模式版本：`0.1`

实现位置：

- `src/videoscope/domain/models.py`
- `src/videoscope/domain/serialization.py`

统一领域模型和 JSON 是报告事实来源；离线 HTML 只渲染已校验的
`AnalysisReport`，不重新运行检测器或修改 Findings。

## 1. 设计原则

- 所有 Detector 最终只能通过统一 `Finding` 表达结果；
- 模型使用 Pydantic，拒绝未声明字段，并可生成 JSON Schema；
- 所有时间统一使用非负浮点秒；
- JSON 使用 UTF-8，中文保持原文而不是强制转义；
- JSON 对象键和 Finding 数组使用固定顺序；
- Finding ID 可确定性复现，不能使用随机 UUID；
- `analysis_id` 属于一次运行的信封信息，可以随机；
- `schema_version` 为未来迁移保留兼容边界；
- `score` 和 `confidence` 都是 Detector 内语义，不能合成总质量分。

## 2. 时间语义

`TimeRange` 字段：

| 字段 | 类型 | 必需 | 约束 |
| --- | --- | --- | --- |
| `start_seconds` | float | 是 | 有限且不小于 0 |
| `end_seconds` | float | 是 | 有限且不小于 `start_seconds` |
| `start_frame` | integer/null | 否 | 不小于 0 |
| `end_frame` | integer/null | 否 | 不小于 `start_frame` |

区间按半开区间 `[start_seconds, end_seconds)` 记录，允许用零长度区间
表达时间点标记。
具体 Detector 应在自身配置中定义端点采样语义，不能依赖帧率反推
可变帧率视频的时间。

步骤 4 将报告时间从旧设计稿中的整数毫秒迁移为浮点秒。自模式
`0.1` 起，规范字段只使用 `*_seconds`。

## 3. Severity

允许值及固定排序顺序：

1. `info`
2. `low`
3. `medium`
4. `high`
5. `critical`

Severity 是配置化 Detector 对可观察现象的分级，不代表已证明的客观
故障，也不能跨 Detector 当作统一质量尺度。

## 4. Evidence

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `evidence_type` | string | 是 | 例如 `frame`、`frame_pair`、`metadata` |
| `timestamp_seconds` | float | 是 | 非负、有限秒数 |
| `relative_path` | string/null | 否 | 输出目录内的相对证据路径 |
| `description` | string | 是 | 证据说明 |
| `metadata` | object | 是 | 结构化补充指标，默认 `{}` |

每个 Finding 的 `evidence` 必须非空。视觉 Finding 应提供可展示的
帧或帧对；纯元数据 Finding 可以使用结构化 `metadata` 证据。

## 5. Finding

| 字段 | 类型 | 必需 | 约束 |
| --- | --- | --- | --- |
| `id` | string | 是 | `finding_` 加 64 位小写 SHA-256 |
| `detector_id` | string | 是 | 非空稳定标识 |
| `detector_version` | string | 是 | 非空算法版本 |
| `title` | string | 是 | 简短的可观察现象 |
| `description` | string | 是 | 解释指标和启发式判断 |
| `severity` | Severity | 是 | 五级枚举 |
| `score` | float | 是 | `[0, 1]` |
| `confidence` | float | 是 | `[0, 1]` |
| `time_range` | TimeRange | 是 | 合法时间区间 |
| `evidence` | Evidence[] | 是 | 至少一项 |
| `tags` | string[] | 是 | 默认 `[]` |
| `parameters` | object | 是 | 全部相关生效参数，默认 `{}` |
| `limitations` | string[] | 是 | 启发式限制，默认 `[]` |

### 5.1 确定性 ID

`make_finding_id()` 只使用以下数据：

- 视频内容 SHA-256，即 `AnalysisReport.input_hash`；
- `detector_id` 的 UTF-8 字节；
- `start_seconds` 和 `end_seconds` 的 IEEE-754 双精度表示；
- 可选 `start_frame` 和 `end_frame`。

以上数据以固定二进制格式组成载荷，再计算 SHA-256：

```text
finding_<64 lowercase hex characters>
```

相同视频哈希、Detector 和区间始终产生相同 ID。区间、帧边界或
Detector ID 变化会产生不同 ID。

`AnalysisReport` 创建或反序列化时会重新计算每个 Finding ID。即使
传入字符串符合外观格式，只要与报告的 `input_hash`、Detector 或
区间不匹配，整份报告也会校验失败。重复 Finding ID 同样非法。

### 5.2 固定排序

`AnalysisReport` 自动按以下键升序保存 Findings：

1. `time_range.start_seconds`
2. Severity 固定等级：`info` → `low` → `medium` → `high` →
   `critical`
3. `detector_id`
4. `id`

调用方传入顺序不会改变最终 JSON 中的 Finding 顺序。

## 6. VideoMetadata

| 字段 | 类型 | 必需 | 约束 |
| --- | --- | --- | --- |
| `filename` | string | 是 | 非空文件名 |
| `container_format` | string | 是 | 非空 |
| `codec` | string | 是 | 非空 |
| `width` | integer | 是 | 大于 0 |
| `height` | integer | 是 | 大于 0 |
| `duration_seconds` | float | 是 | 非负、有限 |
| `average_frame_rate` | float | 是 | 非负、有限 |
| `estimated_frame_count` | integer | 是 | 非负 |
| `has_audio` | boolean | 是 | 是否存在音频流 |
| `file_size_bytes` | integer | 是 | 非负 |
| `creation_time` | datetime/null | 否 | 可用时提供 |
| `raw_probe` | object | 是 | 脱敏探测摘要，默认 `{}` |

`raw_probe` 不是 ffprobe 原始输出转储。未来探测实现必须先过滤绝对
路径、用户名和不需要的敏感元数据。

## 7. DetectorExecution

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `detector_id` | string | 是 | Detector 稳定标识 |
| `status` | enum | 是 | `ok`、`detector_error` 或 `skipped` |
| `elapsed_seconds` | float | 是 | 非负、有限 |
| `findings_count` | integer | 是 | 非负 |
| `error_type` | string/null | 否 | 失败错误类型 |
| `error_message` | string/null | 否 | 脱敏错误说明 |

当且仅当 `status` 为 `detector_error` 时，`error_type` 和
`error_message` 必须同时存在。`ok` 且 `findings_count == 0` 表示
确实完成且没有 Finding，不能用于掩盖执行错误。

## 8. AnalysisReport

| 字段 | 类型 | 必需 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | string | 是 | 当前默认 `0.1`，格式 `major.minor` |
| `tool_version` | string | 是 | VideoScope 版本 |
| `analysis_id` | string | 是 | 一次分析的非空 ID，可以随机 |
| `created_at` | datetime | 是 | 必须包含时区 |
| `input_hash` | string | 是 | 64 位小写 SHA-256 |
| `prompt` | string/null | 否 | 可选输入提示词 |
| `metadata` | VideoMetadata | 是 | 规范视频元数据 |
| `configuration` | object | 是 | 完整生效配置 |
| `detector_executions` | DetectorExecution[] | 是 | 执行记录 |
| `findings` | Finding[] | 是 | 自动规范排序 |
| `warnings` | string[] | 是 | 报告级警告 |
| `runtime` | object | 是 | 运行时版本等可复现信息 |

`prompt` 默认应为 `null`。只有调用方明确决定在本地报告中保留提示词
时才写入原文；序列化层不会上传或发送它。

可选 detector 可以把不构成 Finding 的描述性曲线写入
`runtime.detector_diagnostics.<detector_id>`。例如
`prompt_alignment` 的 `descriptive` 模式记录逐 scene 的余弦相似度、
最低 scene、provider/model ID 和限制说明。该数据必须可 JSON
序列化、不得含绝对路径，也不能被消费者误当作跨 detector 总质量分。

`analysis_id` 和 `created_at` 可以随运行变化，因此整份报告字节不保证
跨运行相同。确定性要求适用于 Findings 的 ID、内容与排序。需要
可重复比较时，消费者应忽略运行信封字段。

## 9. JSON 序列化

`report_to_json()`：

- 使用 UTF-8 字符串语义；
- `ensure_ascii=False`，保留中文；
- 所有对象键按字典序输出；
- 默认缩进 2 个空格；
- 禁止 NaN 和 Infinity。

`write_report_json()` 使用 UTF-8 和 `\n` 换行，并在文件末尾添加一个
换行。`report_from_json()` 与 `read_report_json()` 会执行完整
Pydantic 校验和 Finding ID 复算。

示例：

```python
from videoscope.domain import (
    AnalysisReport,
    analysis_report_json_schema,
    read_report_json,
    report_to_json,
    write_report_json,
)
```

## 10. JSON Schema

调用：

```python
from videoscope.domain import analysis_report_json_schema

schema = analysis_report_json_schema()
```

结果由 `AnalysisReport.model_json_schema()` 生成，包含所有嵌套模型、
枚举、必需字段和数值范围。实现与生成的 JSON Schema 是规范事实
来源；手写文档必须与之同步。

## 11. Schema 迁移

- `schema_version` 使用 `major.minor`；
- 兼容地新增可选字段时递增 minor；
- 删除字段、改变字段含义或修改必需性时递增 major；
- 未来迁移器必须显式接收源版本和目标版本；
- 不得静默把未知版本当成当前版本；
- 历史报告必须先校验源模式，再执行迁移。

当前只实现 `0.1` 模型，尚未实现迁移器。

## 12. 当前未实现

- 跨 schema 版本的报告迁移器。

## Optional visual-drift diagnostics

`runtime.detector_diagnostics.visual_semantic_drift` is JSON-compatible,
path-free diagnostic data. It records provider/model/preprocessing IDs,
per-scene robust baselines, adjacent and long-gap distance series, and a small
peak summary. A corresponding Finding stores the same effective configuration
under `parameters`; its two evidence items identify the samples before and
after the peak comparison. These fields are detector diagnostics, not an
identity decision or a cross-detector quality score.

## Optional OCR diagnostics

`runtime.detector_diagnostics.text_stability` records path-free scene-local
tracks and candidates, including OCR text, confidence, normalized boxes,
provider/model IDs and edit-distance summaries. A text-stability Finding uses
ordinary `Evidence` entries. Its `metadata.ocr_boxes` is a list of objects with
`text`, `confidence`, and a normalized `bounding_box` containing `x_min`,
`y_min`, `x_max`, and `y_max`.

Renderers must validate these coordinates before drawing overlays. Invalid or
absent box metadata must not break JSON or HTML output. OCR confidence is model
output and must not be presented as calibrated confidence that a video defect
exists.
