# VideoScope 千星增长设计规范

状态：已确认设计，等待书面审查后编写实施计划

日期：2026-08-11

适用版本：VideoScope v0.8.0 之后的增长与产品呈现迭代

负责人品牌：VideoScope，固定署名 `Created by what912`

## 1. 目的

本规范定义 VideoScope 从已发布但几乎没有外部流量的开源项目，成长为
拥有真实用户、真实案例、持续贡献和数千 GitHub Star 潜力的产品路径。

数千 Star 是方向性结果，不是交付承诺。项目不得通过购买、交换、机器人、
抽奖、强制登录或虚假宣传获得 Star。增长必须来自用户实际解决视频问题后
产生的推荐，以及开发者对可复现、local-first 架构的长期认可。

## 2. 已确认决策

1. 采用“双增长飞轮”：先服务国内普通视频创作者，再向全球开发者和
   AI 视频研究者扩散。
2. 前 90 天唯一主要传播产品是“视频抢救＋一键生成可发布版本”，即
   B Video Rescue 与 A Publish Ready 的连续用户旅程。
3. 首发使用高质量、项目原创且可复现的演示媒体；获得真实使用后，再
   征集用户明确授权的公开案例。
4. 对外以 VideoScope 品牌为主，所有语言环境固定显示
   `Created by what912`；重要发布由 what912 署名说明。
5. 目标周期分两段：前 90 天建立 300～800 个真实 Star 的方向性基础，
   随后用 6～12 个月的版本、案例和国际社区运营向数千 Star 推进。
6. 继续保持零项目方云计算支出：GitHub Pages 是公开入口，完整处理在
   用户本机完成，远程 AI 由用户自带 Key 并自行承担供应商费用。

## 3. 当前基线

2026-08-11 的公开 GitHub 数据为：

- 仓库 `what912/VideoScope`：1 Star、0 Fork、0 Watcher；
- v0.8.0 的 Windows 安装程序、wheel、sdist 和校验文件均为 0 下载；
- GitHub 最近统计窗口显示 44 次仓库浏览、2 位独立访客；
- Clone 数据包含 433 次、69 个独立来源，但自动化工作流和工具可能产生
  Clone，不能把它当作真实用户数；
- Community Profile 为 100%，Apache-2.0、README、贡献指南、安全策略、
  行为准则和 PR 模板已经存在；
- v0.8.0、主 CI、Windows 安装程序和 GitHub Pages 部署已经通过。

因此当前主要瓶颈不是自动化工程完整度，而是：

```text
几乎没有外部曝光
  -> 没有安装与真实处理
  -> 没有可传播的结果证据
  -> 没有 GitHub Star 转化
```

继续堆叠 Detector 或 AI Provider 不能直接解决这一瓶颈。

## 4. 产品传播定位

### 4.1 中文主张

> 视频打不开、格式不兼容、音画异常或观看效果较差？
> 拖入视频，先诊断，再生成一个经过验证、可以继续发布的新副本。

### 4.2 英文主张

> Rescue a problematic video. Export a verified, publish-ready copy.

### 4.3 必须同时表达的边界

- 不覆盖原视频；
- 默认在用户电脑本地处理；
- 先展示诊断、证据、处理计划和预览；
- 用户确认后才生成新副本；
- 输出经过技术验证，但不是艺术质量或平台永久兼容认证；
- 已丢失的画面细节、帧、声音或内容不能被承诺恢复；
- Balanced Rescue 只有在本地测量支持时才提出有界改善动作；
- 失败、`needs_review`、部分恢复和未解决问题必须清晰展示。

禁止使用“100% 修复”“万能画质修复”“任何视频一键恢复”“真实准确率”或
其他没有测量依据的文案。

## 5. 双增长飞轮

```text
普通创作者看到前后对比
  -> 从公开网站进入 Rescue
  -> 在本机获得经过复核的新副本
  -> 分享案例、链接或问题
  -> 开发者发现 GitHub
  -> Star、Issue、插件、文档或数据贡献
  -> 产品兼容性、效果和可信度提高
  -> 更多普通创作者获得结果
```

### 5.1 创作者入口

创作者入口只强调：抢救、发布、安全分享、长视频整理和本地隐私。
内部 A/B/C/D 名称只在选择目标或查看高级详情时出现，不作为首页理解门槛。

### 5.2 开发者入口

开发者入口强调：

- 帧级证据和确定性 Finding；
- Detector 插件协议；
- 时间区间 Benchmark；
- JSON Schema 和 Python API；
- Fake Provider 与可选 BYOK；
- 回环 Connector 的信任边界；
- 源视频只读、预览、精确确认、来源映射和独立验证。

## 6. 普通用户主旅程

首页主要按钮固定为“抢救并生成可发布版本”。次要入口为：

- 查看真实前后对比；
- 仅诊断视频；
- 开发者与研究入口；
- GitHub。

用户首先回答：

```text
你的主要问题是什么？
├─ 打不开、时间轴异常或声音异常
├─ 画面太暗、闪烁、噪声或抖动
├─ 上传平台失败或格式不兼容
└─ 我不确定，让 VideoScope 检查
```

系统把答案映射到已有 B/A 流程，但不能仅凭症状提示授权滤镜或生成
`completed`。完整步骤为：

```text
选择本地视频
  -> 本地检查
  -> 查看检测依据和限制
  -> 预览 Rescue 计划
  -> 用户确认精确计划
  -> 生成 faithful 和可选 improved 副本
  -> 用户播放复核
  -> 选择 Publish Ready Profile
  -> 用户确认发布计划
  -> 生成并验证可发布副本
```

任何阶段失败都必须说明：原文件未修改、失败阶段、可操作原因、可尝试的
保守路径，以及如何导出不含密钥和私人路径的诊断信息。

## 7. 公开站信息架构

应新增或重构以下页面：

| 路径 | 职责 |
| --- | --- |
| `/` | 结果导向首页，一个真实前后对比和主入口 |
| `/rescue` | 零基础 Rescue 与 Publish Ready 引导 |
| `/examples` | 可筛选案例库 |
| `/examples/:slug` | 单个案例的同区间前后证据、限制和复现信息 |
| `/download` | Windows 下载、SHA-256、FFmpeg、未知发布者和卸载说明 |
| `/developers` | CLI、Schema、Benchmark、插件与贡献入口 |
| `/roadmap` | 已实现、正在验证和未承诺能力 |
| `/community` | Discussion、Issue、案例授权与贡献指南 |

首页内容顺序固定为：

1. 真实前后对比；
2. “抢救并生成可发布版本”；
3. 三步使用流程；
4. 本地处理与隐私边界；
5. 三个代表性案例；
6. 下载安装；
7. 开发者入口；
8. `Created by what912`；
9. GitHub Star。

普通用户页面不得先展示插件、模型、Detector ID、JSON Schema 或大量
字母模式。开发者页面可以完整表达这些内容。

## 8. README 第一屏

README 首屏结构为：

```text
VideoScope
Rescue a problematic video. Export a verified, publish-ready copy.

[15-25 second same-range before/after demo]

[Download for Windows] [Open Web App] [View Examples]

Local-first · Source preserved · CPU available · Optional BYOK AI
Created by what912
```

随后依次为：三分钟上手、Rescue 实际效果、支持与不支持的问题、完整
A/D/B/C 工作流、隐私、安全、开发者架构、Benchmark、插件、路线图和贡献。

项目 Social Preview 使用 1280×640 的自有素材，包含 VideoScope、同区间
前后差异、local-first 和 what912，不使用第三方品牌或版权素材。

## 9. 案例系统

### 9.1 单一案例清单

网站、README、分享卡和后续 API 都必须读取同一份版本化案例清单，避免
多个组件复制不一致数据。每个案例至少包含：

- 稳定案例 ID 和 URL slug；
- 中英文标题与简短摘要；
- `project-authored`、`user-authorized` 或 `synthetic-regression` 来源；
- 可公开授权摘要；
- 原始症状和可观察证据；
- 使用的 VideoScope、FFmpeg、平台和配置版本；
- 用户确认的 Rescue 与 Publish Ready 动作；
- 同一来源时间区间的 before/after 媒体；
- 输出验证状态；
- 未解决问题、限制和复现步骤；
- 自有媒体哈希与构建来源。

`synthetic-regression` 只能作为工程回归证据，不能包装成真实用户效果。

### 9.2 首发案例

首发准备三个项目原创案例：

1. 容器、时间轴或兼容性异常，展示 faithful Rescue；
2. 本地测量支持的可观察画面/声音问题，展示 faithful 与独立 improved；
3. 普通横屏素材生成不裁剪的竖屏或兼容发布版本。

所有案例必须由当前公开版本实际处理。页面不得使用模拟成功状态替代
真实产物。

### 9.3 用户授权案例

采用“本地生成＋GitHub 人工提交”，不建立项目方付费上传服务器：

1. 用户在本地选择愿意公开的短片段；
2. VideoScope 生成去除私人路径的候选案例包；
3. 用户预览包内全部内容；
4. 用户明确选择授权范围；
5. 打开 Case Submission Issue Form；
6. 默认只提交描述和脱敏诊断摘要；
7. 公开视频由用户再次主动上传；
8. 维护者审核授权、隐私、复现性和文案后才能合并。

案例提交不是获得结果的前置条件，也不能与 Star、抽奖或优先修复绑定。

## 10. 前后对比和分享卡

成功结果页提供：

- 同时间段同步播放；
- before/after 拖动或并排对比；
- 执行的确切动作；
- 未解决问题与验证结果；
- 源文件未被修改的说明；
- 本地生成的分享卡。

分享卡默认不包含视频帧、原文件名、绝对路径、Prompt、字幕全文、API Key、
Provider 请求或私有证据。用户主动选择包含画面时必须预览最终图像。

只有 `completed` 且用户完成结果播放/下载步骤后，才可以显示温和的 Star
提示：

> VideoScope 帮你解决了这个视频吗？在 GitHub 点一个 Star，让更多人找到
> 这个免费、本地运行的工具。

`failed`、`cancelled`、`needs_review` 或没有生成结果时不得请求 Star。

## 11. 开发者与社区结构

开发者页面提供三个不超过十分钟的入门贡献：

1. 新增或改善一个 Detector 测试；
2. 改善一种语言翻译；
3. 提交一个脱敏的问题描述或案例元数据。

GitHub 配置应包含：

- `good first issue`、`help wanted`、`needs reproduction`、`case study`；
- Discussions 的 Help、Ideas、Show and Tell 和 Benchmark 分类；
- 中英文 Bug、误报、漏报、安装、案例和功能请求表单；
- 公开 Roadmap 和下一版本讨论；
- Release Notes 和 README 中的贡献者致谢；
- 相关且真实的 Topics，不超过 GitHub 的限制。

首批 Topics 候选：`video-repair`、`video-quality`、`ffmpeg`、
`local-first`、`privacy-tools`、`creator-tools`、`computer-vision`、
`video-analysis`、`video-processing`、`ai-video`。

## 12. 渠道与内容

### 12.1 国内阶段

主要渠道：B站、小红书、抖音、知乎。每个内容只解决一个问题：

- 为什么本机能播但上传平台失败；
- 如何处理容器或时间轴异常；
- 闪烁、过暗、噪声和音画问题可以改善到什么程度；
- 为什么丢失的信息无法凭空恢复；
- 本地处理对私人视频的意义；
- 一个完整成功或失败案例复盘。

每周发布两个短内容，每两周发布一个完整案例。不同平台根据受众重新
组织内容，不复制同一广告文本。

### 12.2 国际阶段

渠道顺序：GitHub Release/Discussions、Show HN、Product Hunt、合规的
Reddit 社区、Dev.to 或技术博客、相关 Awesome Lists 和开源社区。

Show HN 发布必须让用户直接尝试非平凡的产品，避免注册墙，并由 what912
留在讨论区回答技术、隐私和限制问题。不得请求朋友集中点赞或评论。

国际内容包括：

- 英文 30 秒同区间对比；
- 英文三分钟安装；
- GitHub Pages＋Loopback Connector 的零云成本架构；
- Detector、Benchmark、证据与确定性；
- 一个真实案例的完整技术复盘；
- 用户反馈版本的变更与限制。

## 13. 90 天执行节奏

### 第 1～2 周：传播基础

- 重构首页、README 和 Social Preview；
- 建立 Rescue、案例、下载和开发者入口；
- 制作三个真实运行的项目原创案例；
- 制作 30 秒演示和三分钟教程；
- 完善 Topics、Discussions、Issue Forms 和授权流程；
- 在全新 Windows 用户环境完成安装、FFmpeg、启动、配对、处理和卸载。

### 第 3～4 周：小范围验证

邀请 10～20 位测试者，包括无 Python 基础创作者和技术用户。记录他们是否
能够找到下载、理解未知发布者、解决 FFmpeg、完成配对、选对目标、找到输出、
保护源文件并判断结果价值。

三个以上用户卡在同一步时，暂停推广并修复流程。

### 第 5～6 周：国内首发

发布六类问题内容和至少一个完整案例。每篇都包含症状、过程、同区间前后
证据、限制、免费下载和 GitHub 链接。

### 第 7～8 周：反馈版本

优先修复安装、配对、FFmpeg、错误说明和输出查找，其次修复高频处理失败，
最后才增加新功能。发布 v0.8.1 或 v0.9 时公开成功、失败、未解决问题和真实
用户反馈带来的变化。

### 第 9～10 周：国际发布

在英文演示、教程、案例、架构说明、直接下载和贡献入口全部就绪后，按渠道
顺序发布。不得在同一天向大量社区灌入相同广告。

### 第 11～12 周：形成节奏

- 再发布两个授权案例；
- 发布一次 Benchmark 或工程复盘；
- 合并并感谢首批外部贡献；
- 发布 90 天透明总结；
- 根据实际漏斗决定下一季度方向。

## 14. 指标体系

Star 是结果指标。增长漏斗同时记录：

| 阶段 | 指标 |
| --- | --- |
| 被看见 | 内容播放、网站入口点击、GitHub 独立访问 |
| 愿意尝试 | Release 下载、教程完成、自愿测试报名 |
| 获得结果 | 自愿反馈的成功生成和播放验证 |
| 愿意推荐 | Star、分享、案例授权、外部引用 |
| 形成社区 | 有效 Issue、Discussion、外部贡献者和插件 |

前 90 天方向性目标：

- 300～800 个真实 Star；
- 300 次以上 Release 下载；
- 100 次可确认的真实尝试；
- 50 次成功处理；
- 10 个授权公开案例；
- 20 个有效 Issue 或 Discussion；
- 3～5 个外部贡献者；
- 1～3 个外部社区、博客或工具合集引用。

这些数字不得成为发布门禁、员工绩效式强制指标或宣传事实。

## 15. 零成本、无追踪测量

不添加广告追踪、用户指纹、远程字体、跟踪像素或付费分析服务。数据来自：

- GitHub Star、Traffic、Release 下载、Issue、Discussion 和贡献者；
- 各内容平台提供给发布者的聚合浏览和互动数据；
- 用户主动提交的成功、失败和授权案例；
- 维护者每周保存的聚合增长快照。

增长快照字段为：`date`、`stars`、`release_downloads`、`valid_issues`、
`contributors`、`authorized_cases`、`published_content`、`known_failures`。

快照不包含视频、文件名、路径、IP、设备指纹、API Key、Prompt 或用户身份，
也不能被解释为检测或修复准确率。

## 16. 诊断与调整规则

### 有曝光、没有下载

优先检查主张、同区间证据、未知发布者说明和下载入口，不增加 Detector。

### 有下载、没有成功结果

暂停大规模推广，修复 FFmpeg、配对、目标选择、兼容性、错误恢复和输出查找。

### 有成功结果、没有 Star

检查开源身份、成功后的提示时机和分享能力；不得强制 GitHub 登录或把结果
下载与 Star 绑定。

### 有 Star、没有持续用户

减少营销内容，优先处理 Issue、真实案例和实际失败，不用更频繁发布掩盖
产品价值问题。

### 开发者关注显著高于创作者

强化 SDK、Benchmark、Provider、Detector 和复现实验，把消费者工具保留为
框架的可信演示应用。

## 17. 暂停推广条件

出现以下任何情况时暂停推广并公开修复状态：

- 安装程序来源或校验无法确认；
- 输出视频无法正常播放；
- 存在覆盖源视频的风险；
- 分享卡或案例泄露私人路径、密钥或未授权内容；
- 配对、会话或 BYOK 信任边界出现安全缺陷；
- 多名用户在同一阶段失败；
- 案例授权范围不明确；
- 演示结果无法用同一公开版本复现；
- `failed` 或 `needs_review` 被展示为成功。

暂停推广是保护项目信誉的正常机制，不得通过删除失败报告或弱化测试绕过。

## 18. 错误处理与隐私

- 分享卡、案例包和增长快照均使用 allowlist 输出，不复制工作区树；
- 所有公开路径必须是输出根内相对 POSIX 路径；
- 用户媒体只能在明确授权范围内公开；
- 授权撤回后停止新增传播，并在可控仓库资产中移除对应案例；
- 浏览器分析、Connector、Rescue、Publish Ready 失败保持既有状态语义；
- 增长 UI 不能修改 Detector Finding、Rescue Verification 或 Publish
  Verification 的事实；
- Star CTA、分享卡失败或案例生成失败不能影响已完成的本地视频结果。

## 19. 国际化、可访问性与移动端

- 所有新增普通用户页面完整支持英文和简体中文；
- `what912` 不参与语言切换；
- 视频对比有文字标签、键盘控制和非颜色状态；
- 动画遵守 `prefers-reduced-motion`；
- 案例页和安装页在手机上无横向溢出；
- 视频、图片和按钮有替代文本、焦点状态和合理对比度；
- 失败、警告、通过不能只用颜色区分。

## 20. 测试与验收

### 20.1 自动化

- 路由刷新和 GitHub Pages base path；
- 中英文文案完整性和固定 what912；
- 案例清单 Schema、排序、来源类型和重复 ID；
- 同区间 before/after 数据一致；
- 未授权案例和 private evidence 不能构建；
- 分享卡默认脱敏与显式媒体选择；
- 只有成功终态显示 Star CTA；
- 下载链接、Release 版本和 SHA-256 文件存在；
- 无远程字体、跟踪像素和未允许域名；
- 社交元数据、sitemap 和结构化数据；
- 键盘、reduced motion、移动端和无 Finding/失败状态；
- README 中命令和链接契约；
- 基础 CI 不联网、不下载模型、不需要 GPU。

### 20.2 人工

- 零基础用户三分钟内找到正确安装包；
- 全新 Windows 用户完成 FFmpeg、Connector 和配对；
- Firefox、Chrome/Edge 的 HTTPS 到 Loopback 私有网络提示；
- 三个原创案例由正式版本复现；
- 用户能理解原文件不被覆盖；
- 用户能识别 faithful、improved 和 publish-ready 的差别；
- 真实失败状态提供可操作恢复；
- 分享卡不泄露个人信息；
- 手机端能查看案例和安装教程；
- README、网站、Release 和视频中的主张一致。

## 21. 非目标

本增长阶段不包括：

- 购买广告、Star、评价或榜单位置；
- 建立项目方付费的视频处理云；
- 强制云账户、邮箱或 GitHub 登录；
- 为传播而添加未经验证的视频修复算法；
- 生成虚构用户评价、下载量、准确率或案例；
- 自动上传用户视频、结果、日志或分析事件；
- 用一个未经校准的总体质量分简化传播；
- 绕过 A/B/C/D 的预览、确认、来源映射和验证。

## 22. 风险

- 未签名 Windows 安装程序会降低下载转化，必须保留未知发布者和校验说明；
- FFmpeg 是外部依赖，零基础流程可能仍有摩擦；
- 普通创作者获得价值后未必拥有 GitHub 账号；
- 高质量案例制作需要持续时间投入；
- 不同视频、编码器和播放器可能产生与演示不同的结果；
- 过多模式会稀释 Rescue＋Publish Ready 的首要定位；
- AI 话题可能带来关注，但不能掩盖 CPU 结果或诱导不必要的数据传输；
- 单次社区发布可能产生短期 Star，但不代表持续用户价值。

这些风险通过真实案例、透明失败、分阶段发布、稳定主入口和人工验收管理，
不能通过扩大宣传承诺规避。

## 23. 90 天后的决策

- 达到 300～800 Star 且真实反馈良好：扩大国际发布、插件生态、macOS
  体验、公开 Benchmark 和合作维护者，继续向 1k～3k Star 推进；
- Star 有限但真实用户满意：保留创作者方向，改善案例和分发；
- 曝光高但成功率低：停止增长活动，回到安装、兼容性和实际效果；
- 开发者关注明显更强：强化框架能力，把消费者工具作为可信演示；
- 任何情况下都不因未达 Star 目标而弱化隐私、验证或真实性边界。

## 24. 外部参考

- GitHub Topics：<https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics>
- GitHub Social Preview：<https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview>
- Show HN Guidelines：<https://news.ycombinator.com/showhn.html>

这些参考只用于平台规则和发现机制，不构成 Star 数量承诺。
