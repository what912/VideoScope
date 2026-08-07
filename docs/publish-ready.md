# Publish Ready contract

状态：v0.3 开发线的规范性契约

Publish Ready 独立开发线使用 `0.3.0.dev0`；包含后续 Safe Sharing 的组合开发线
使用 `0.4.0.dev0`。在单独发布审计明确批准候选号之前，不得使用正式版本号。

## 1. Scope and boundary

VideoScope Resolve is an opt-in processing workflow built on VideoScope Check.
It never changes the v0.1 analysis contract, never overwrites the source, and
does not make processing dependencies part of the base diagnostic path.

Publish Ready 是 Resolve 的 A MVP。它生成单独的、可发布的本地文件，不修改
源视频。`AnalysisReport` 仍是 Check 的诊断事实来源；Publish Ready 不修改历史
Finding，也不改变 `videoscope analyze`。

## 2. Profiles and permitted processing

唯一有效的 Profile 标识为：

- `compatible_mp4`
- `social_vertical_9_16`
- `social_horizontal_16_9`

Profile pass 是版本化兼容性结果，不是全局、跨 Profile 或艺术质量分。A MVP 仅可
执行兼容 MP4、9:16 scale-and-pad、16:9 scale-and-pad、元数据剥离、fast-start
布局、代表性封面、预览、变更记录和输出后验证。竖屏和横屏必须保留完整源画面，
不得裁剪。

不允许远程后端、网络、GPU、AI、模型下载、人脸或身份识别、自动裁剪、片段删除、
插帧、稳定、音乐或生成式增强。不得在仓库或发行物中捆绑 FFmpeg 二进制文件或
WASM。

## 3. Lifecycle, confirmation, and exits

生命周期为：

```text
created -> inspecting -> planning -> awaiting_confirmation -> processing -> verifying -> completed|needs_review|failed|cancelled
```

确认是处理的前置条件；`awaiting_confirmation` 中不得写入最终输出。取消停止新
动作，状态为 `cancelled`，退出码为 130。

```text
0   output exists and verification passed
2   input, profile, configuration, or confirmation error
3   FFmpeg/ffprobe could not process the media
4   internal orchestration or artifact failure
5   output exists but verification requires human review
130 user cancellation
```

验证失败时，任务只能为 `needs_review` 或 `failed`；不得成为 `completed` 或被
展示为 Publish Ready。

## 4. Privacy, paths, and execution

所有处理都在本地执行，源视频只读。外部 FFmpeg/ffprobe 命令必须使用参数数组和
`shell=False`。公开 JSON 中的每条路径必须相对于输出根目录，使用正斜杠，且只
能引用输出根目录内的产物。公开报告不得含绝对路径、用户名、令牌、未过滤 stderr
或源文件路径。

测试必须离线、CPU-only、确定性且覆盖含空格、中文和其他 Unicode 字符的路径。

## 5. Artifact publication

成功或需人工复核的任务可使用如下输出根目录布局：

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

最终文件只在输出存在且验证通过时才可标记为 `completed`。输出存在但复检要求
人工判断时保留产物并标记 `needs_review`；处理或产物故障时标记 `failed`。

## 6. Current implementation limits

- FFmpeg 和 ffprobe 必须由用户单独安装并能从 `PATH` 调用。当前转码路径要求该
  FFmpeg 构建提供 H.264 `libx264` 和 AAC 编码器；缺少编码器或输入解码器时任务
  会以媒体处理错误结束，VideoScope 不会自动安装另一套二进制文件。
- 可读取的输入容器和编解码器由本机 FFmpeg 构建决定。当前实现不会绕过受损、
  加密、DRM 或本机解码器不支持的媒体。
- Profile 只验证输出容器、编解码器、像素格式、画布、平均帧率、时长、音频流和
  两项 detector 回归摘要。VFR 输入需要在目标播放器人工检查节奏和音画同步；
  “平均帧率不超过 60”不等于完整的逐帧时基认证。
- 长视频会进行完整本地转码，并运行处理前后诊断。运行时间、CPU、内存和临时
  磁盘占用尚未在统一的真实长视频集合上基准测试；单条 FFmpeg 命令默认超时为
  3600 秒。
- 本地 Dashboard 的预览和下载依赖浏览器对生成 MP4 的原生播放能力。发布前仍需
  在真实目标平台、Firefox、Chromium 和所需移动设备上人工播放检查。
- `passed` 仅表示当前版本 Profile 的技术检查通过，不证明艺术质量、内容正确性、
  无障碍合规或任何平台未来规则的永久兼容性。
