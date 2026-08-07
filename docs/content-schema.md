# Long Video to Useful Content schema

状态：C CPU MVP 的公开契约；实现从 schema `0.1` 开始

实现位置：`src/videoscope/content/`

本 schema 属于独立的 C 工作流，不修改冻结的 v0.1 `AnalysisReport`、Finding、
Detector 协议或 `videoscope analyze`。JSON 是 C 结果的事实来源；离线 HTML 只能
渲染已经验证的模型。

## 1. 目标和枚举

`ContentGoal` 固定为：

- `faithful_clean`（Faithful Clean）；
- `chaptered_full`（Chaptered Full）；
- `selected_clips`（Selected Clips）。

终态 `ContentOutcome` 固定为 `completed`、`partial`、`needs_review`、`failed`
或 `cancelled`。任何必需验证不通过都不能成为 `completed`。

## 2. 通用约束

- 模型拒绝未知字段；
- 秒数必须是有限、非负浮点数；
- 内容区间使用半开区间 `[start_seconds, end_seconds)`，且内容区间必须有正长度；
- SHA-256 为 64 位小写十六进制；
- public artifact 路径只能是输出根目录内、正斜杠、无 `..` 的相对 POSIX 路径；
- 同一输入、字幕、配置和用户决定产生相同 ID、排序、计划摘要和 JSON；
- JSON 使用 UTF-8、`ensure_ascii=False`、排序键、禁止 NaN/Infinity，并原子写入；
- 用户原始绝对路径、用户名、私有证据和未明确导出的字幕文本不能进入公开 JSON。

## 3. ContentConfig

`ContentConfig` 记录全部生效配置，至少包括：

- `goal`；
- 场景、静音、低视觉变化、近黑和重复帧 provider 配置；
- 自动提议所需的最少互相印证信号数量；
- 最小候选时长、左右上下文保护、合并间隙；
- 章节最小/最大时长；
- 可选目标时长；
- 最大字幕 cue、章节、故事板项目、预览数量和预览时长；
- 硬剪或有界音频淡化参数；
- 来源顺序与显式重排设置；
- verification policy；
- 是否输出字幕或独立 clips。

所有阈值必须来自 strict 配置模型。目标时长只能排序安全候选，不能强迫删除。

## 4. ContentMap 和 ContentSegment

`ContentMap` 是私有、描述性的内容结构证据，包含 schema/tool 版本、来源 SHA-256、
可选规范字幕 SHA-256、视频元数据摘要、有效配置、provider 执行记录、有序
`ContentSegment`、用户范围、warnings 和确定性 map digest。

每个 `ContentSegment` 至少包含：

- 稳定 `id` 和 `source_order_index`；
- 来源时间范围和可选帧范围；
- signal 类型、可观察 measurements、provider/算法版本和有效参数；
- 可选字幕 cue ID 引用，不得捏造引用；
- selection eligibility、理由和 limitations；
- 私有相对证据；
- 与 keep、exclude、locked、chapter 用户范围的关系。

ContentMap 不包含总体质量、重要性、精彩度或传播潜力分数。一个 provider 失败时，
其状态和 warning 必须可见，其他合法证据仍可保留。

## 5. Storyboard 和章节

`Storyboard` 记录 `goal`、输入/字幕哈希、有序 `StoryboardItem`、章节、锁、用户决定、
估计输出时长、估计来源覆盖率和 storyboard digest。

`StoryboardItem` 至少包含稳定 ID、一个精确来源区间、来源顺序、输出顺序、状态
（keep/remove）、决定来源（proposal/user/lock）、理由、标签以及对应 segment/action
引用。默认输出顺序必须与来源顺序一致；重排必须有独立开关、警告和确认。

`ContentChapter` 包含稳定 ID、输出和来源边界、可编辑标题及标题来源。没有可信本地
文本时使用 `Chapter 01` 等中性标题，不生成摘要或标题。

## 6. ContentPlan、预览和确认

`ContentAction` 包含稳定 ID、版本、动作类型、精确来源范围、预期输出范围、参数、
内容是否改变、是否需要确认、依赖、fallback 和证据引用。

`ContentPlan` 包含来源/字幕哈希、有效配置、完整 storyboard digest、有序 actions、
锁、预期 artifacts、verification policy、所需 preview identities 和 canonical
`plan_digest`。

每个内容改变 action 都必须有一个成功的有界剪接预览。preview identity 覆盖来源
哈希、字幕哈希、action ID、精确范围、编码参数和预览文件哈希；预览只存在于
`content-review-private/preview/`。

`ContentConfirmation` 必须绑定：

- 来源和可选字幕哈希；
- 完整配置、storyboard 和 plan digest；
- 全部 locked ranges；
- 被接受 action 的精确 ID 和 preview identity；
- verification policy；
- 显式重排确认（若适用）。

计划、范围、锁、配置、字幕、来源或预览任一变化都会使旧确认失效。

## 7. 来源映射和变更记录

每个内容改变结果都必须发布 `source-map.json`。`ContentSourceMapping` 对每个输出
区间记录：

- 稳定 mapping ID；
- 半开 output range；
- 一个精确 source range；
- source/output order；
- transition（默认 hard join，可选有界 audio fade）；
- `unchanged` 或 `transformed`；
- storyboard item 和 action ID。

映射必须覆盖完整输出时间线，无空洞、重叠或越界；其输出时长总和必须在配置容差
内等于媒体时长。`ContentChangeLog` 逐项记录确认并实际执行的动作和未执行原因，
不能把未接受动作写成已执行。

## 8. 验证和技术报告

`ContentVerificationCheck` 记录稳定 check ID、版本、required、状态、可观察结果、
参数、limitations 和脱敏错误。`ContentVerificationReport` 汇总：

- 输出与每个 clip 可解码；
- 时长和 stream inventory 与确认计划一致；
- 来源映射合法并守恒；
- locked keep 完整存在、locked exclude 不存在；
- 默认来源顺序或有效显式重排；
- 剪接没有引入新的长黑/重复帧、音频连续性或固定 A/V 残差回归；
- 章节和可选字幕时间合法；
- public allowlist 和路径隐私通过；
- 来源 SHA-256 未改变。

必需检查失败产生 `failed`，不充分产生 `needs_review`。只有经过独立验证的缺失
clips 才能以 `partial` 和精确 missing ranges 表达。

`ContentTechnicalReport` 是公开终态信封，包含 schema/tool 版本、goal、outcome、
输入哈希、配置摘要、公开 artifacts、章节、来源映射、变更记录、验证报告、warnings、
limitations 和无个人路径的 runtime 信息。

## 9. 私有与公开产物

私有复核树：

```text
content-review-private/
  content-map.json
  storyboard.json
  preview/
  evidence/
  transcript-normalized.json
```

公开结果树固定为：

```text
content-output/
  useful-content.mp4
  chapters.json
  source-map.json
  changes.json
  technical-report.json
  report.html
  subtitles.srt
  clips/
```

最后两项只在用户明确请求且验证通过时出现。公开 artifact 路由不得访问私有树。

## 10. 迁移和 Advanced AI 边界

schema 使用 `major.minor`。兼容增加可选字段递增 minor；删除字段、改变字段语义或
必需性递增 major。未知版本必须拒绝，不能静默当作当前版本。

Advanced AI 不属于 schema `0.1` 的 CPU MVP。未来自动 ASR、语义亮点、生成标题或
摘要只能作为显式安装、延迟加载、无默认下载的可选 provider，并且不能绕过确认、
来源映射或独立验证。
