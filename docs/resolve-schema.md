# Resolve schema contract

状态：v0.3 Publish Ready MVP 的规范性数据与产物契约

## 1. Versioning and compatibility

所有 `PublishPlan`、Profile、动作、确认、产物和 `VerificationReport` 都必须有
稳定版本标识。Profile 仅限 `compatible_mp4`、`social_vertical_9_16` 和
`social_horizontal_16_9`。它们的 pass 结果只表示该版本 Profile 的兼容性，不能
合成为全局或艺术质量分。

本契约不修改 v0.1 `AnalysisReport` 的任何字段或 `videoscope analyze` 行为。
前后分析报告是独立的、只读的 Check 产物。

## 2. PublishPlan

`plan.json` 表示确认前的 `PublishPlan`，至少记录：schema 版本、任务标识、Profile
标识及版本、源文件只读承诺、请求的允许动作、预览产物、确认要求、预期公开产物
路径和生效配置。计划不得包含绝对个人路径，不得包含未经允许的处理动作。

允许的 MVP 动作是兼容 MP4、scale-and-pad、元数据剥离、fast-start、封面和预览。
竖屏或横屏的 scale-and-pad 不得裁剪任何源画面。

## 3. Confirmation and lifecycle

确认记录必须关联一个具体的计划版本。没有有效确认时，任务停留在
`awaiting_confirmation`，不得进入 `processing`。合法生命周期为：

```text
created -> inspecting -> planning -> awaiting_confirmation -> processing -> verifying -> completed|needs_review|failed|cancelled
```

`completed` 要求输出存在且验证通过；验证失败只能转换到 `needs_review` 或
`failed`。`cancelled` 使用退出码 130。

## 4. Artifacts and public paths

标准相对产物路径为：

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

所有公开 JSON 路径均以任务输出根目录为基准，必须使用正斜杠，并且只能解析到该
根目录内部。`changes.json` 记录实际执行的版本化动作及其参数；
`technical-report.json` 包含 `VerificationReport`，包括 Profile 版本、输出检查、
产物路径和需要人工复核的原因（如有）。

## 5. Verification result

`VerificationReport` 必须区分通过、需要人工复核和失败，且记录实际检查项而不是
全局质量分。输出存在并通过验证时，CLI 退出码为 0；输出存在但需要人工复核时为
5；输入/Profile/配置/确认错误为 2；FFmpeg/ffprobe 媒体处理失败为 3；内部编排
或产物失败为 4。

验证及公开报告不得泄露源视频绝对路径、用户名、令牌或未脱敏的外部命令输出。
