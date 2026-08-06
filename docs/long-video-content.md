# Long Video to Useful Content

Long Video to Useful Content 是 VideoScope 的 C 任务模式。它面向已经有长视频、
希望在较短时间内得到可用成品但又不愿让工具偷偷改写内容的普通用户与研究人员。
整个 CPU 工作流在本机运行，源视频只读，不上传视频，不下载模型。

## 选择目标

### Faithful Clean

适合会议、访谈和教程。VideoScope 会标出经过多种可观察信号共同支持的长低信息
区间，例如“长静音 + 低画面变化”或持续近黑/重复帧。静音本身不会成为删除理由。
用户必须检查精确范围和剪接前后预览，再决定是否移除。

### Chaptered Full

适合希望保留完整录像、只需要更容易浏览的人。完整时间线不删减，VideoScope 按
镜头、停顿和用户标记建议章节。没有可信本地字幕时，章节只使用中性标题，用户可在
确认前改名。

### Selected Clips

适合已经知道要保留哪些时刻的人。用户输入、拖动或用键盘调整多个保留范围，可选择
单独 clips 和一个合并成品。默认保持来源顺序；改变顺序需要单独开启并确认上下文可能
变化的警告。

## 使用流程

```text
选择本地视频
  -> 选择 Faithful Clean / Chaptered Full / Selected Clips
  -> 检查 content map 和可选本地字幕
  -> 编辑、保留、移除和锁定故事板区间
  -> 检查每个剪接的有界预览
  -> 确认页面显示的精确 plan digest
  -> 本地生成 useful-content.mp4
  -> 独立验证
  -> 下载视频、章节、source-map.json、变更记录和报告
```

如果没有足够证据安全缩短，Faithful Clean 会保持完整视频并建议 Chaptered Full 或
手动 Selected Clips，而不是虚构“精彩片段”或为了达到目标时长强行删除。

## 本地字幕

可以选择一个本地 SRT 或 WebVTT。VideoScope 只使用经过时间校验的 cue 作为结构
证据，不自动转录、不调用外部 API、不捏造引用。原始/规范字幕、缩略图、波形、证据、
预览和草稿只放在 `content-review-private/`。只有用户明确选择导出并且验证通过的
字幕才会进入公开结果。

## 结果

成功或可复核任务在 `content-output/` 下生成：

- `useful-content.mp4`：真实可播放的新视频；
- `chapters.json`：验证后的章节；
- `source-map.json`：每段输出对应的精确来源区间；
- `changes.json`：已确认且实际执行的改变；
- `technical-report.json`：机器可读终态与验证；
- `report.html`：完全离线的可读报告；
- 可选 `subtitles.srt` 和 `clips/`。

结果不会显示来源绝对路径。源视频不会被覆盖，其 SHA-256 在处理前后都要检查。

## 何时需要人工检查

- provider 失败或证据不足；
- 字幕时间无效；
- 某个剪接预览失败或已过期；
- 计划、锁、配置、来源或字幕在确认后变化；
- 输出能播放但必需验证结论不充分；
- 目标时长无法在安全规则内达到。

这些情况会明确显示 `needs_review`、`partial` 或 `failed`，不会伪装成完成。

## 明确限制

CPU MVP 不做自动语音识别、说话人或人物身份识别、语义精彩片段排名、生成标题/摘要/
旁白、默认创意重排、自动裁剪、插帧、超分辨率或生成式修复，也不提供总体内容质量、
重要性或传播潜力分数。

Advanced AI 是后续独立阶段。只有 C CPU MVP 的故事板确认、来源映射、隐私边界和
独立验证全部稳定后，可选 AI provider 才能进入，而且仍不能绕过人工确认。
