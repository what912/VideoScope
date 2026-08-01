# VideoScope 离线报告阅读指南

`videoscope analyze INPUT` 默认在输出目录生成 `report.json`、
`report.html` 和 Finding 引用的 `evidence/`。直接用现代浏览器打开
`report.html` 即可阅读；页面不访问 CDN、远程字体、统计服务或其他
网络资源。

## 1. 先看检测器状态

“Detector execution status” 区分三种状态：

- `ok`：检测器正常结束；Finding 数为 0 表示该检测器没有观察到满足
  当前阈值的区间。
- `detector_error`：检测器没有完成。报告会在独立的
  “Detector errors” 区域展示脱敏错误，不能把它理解成“没有问题”。
- `skipped`：检测器因配置或要求未执行。

单个检测器失败不会隐藏其他检测器已经产生的 Findings。

## 2. 阅读时间轴

时间轴覆盖视频完整时长。每个彩色区间对应一个 Finding，位置和长度
来自报告里的 `start_seconds` 与 `end_seconds`。区间按钮的可访问名称
同时包含严重程度、标题和时间，因此颜色不是唯一信息来源。

点击时间轴区间会突出对应的 Finding 卡片。也可以用 Tab 键聚焦区间，
再按 Enter 或空格键选择。

## 3. 筛选和复核 Finding

可按 detector 和 severity 组合筛选。每张 Finding 卡片展示：

- 可观察现象的标题、解释和时间区间；
- detector 版本、severity、score 和 confidence；
- 完整生效参数；
- 启发式方法的限制；
- 前、中、后等代表性证据帧及其时间戳。

`score` 和 `confidence` 只在该检测器、版本和配置内部有意义。VideoScope
不会把它们合成为未经校准的总质量分。点击 “Copy start timestamp”
可以复制区间起点；点击证据帧可打开大图。证据文件丢失时，页面仍会
显示 Finding 文本和缺失提示。

## 4. 原视频与隐私

默认报告不复制或嵌入完整源视频，也不显示输入、工作区或用户目录的
绝对路径。只有显式传入 `--bundle-video` 时，VideoScope 才把源视频
复制为报告目录下的中性文件名，并在 HTML 中显示本地播放器：

```text
videoscope analyze input.mp4 --output runs/review --bundle-video
```

`--json-only` 只生成 JSON，不生成 HTML，也不能与 `--open-report` 或
`--bundle-video` 同时使用。`--open-report` 只是在分析成功后调用系统
浏览器打开本地 `report.html`，不会启动 Web 服务器。

## 5. 机器读取

`report.json` 是事实来源，HTML 只渲染同一份已校验模型，不重新运行
检测器或改变结论。自动化流程应读取 JSON；人工复核使用 HTML。复制
整个报告目录时要同时保留 `evidence/`，否则证据缩略图会显示为缺失。
