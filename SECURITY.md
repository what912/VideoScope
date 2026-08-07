# Security Policy

## Supported versions

The maintained CPU diagnostic line remains `0.1.x`; the current audited
development line is Video Rescue `0.5.x`. Development snapshots do not promise
long-term support.

### Video Rescue security boundary

- Video Rescue never overwrites, moves, or silently deletes the source. It
  writes previews and staging data under `rescue-review-private/`, and publishes
  only independently verified, fixed-name artifacts under `rescue-output/`.
- A Rescue plan must be previewed and bound to an exact digest before media
  processing. Failed, stale, or mismatched confirmation cannot authorize output.
- Rescue reports and video derivatives remain potentially sensitive. Delete the
  complete local job directory only after preserving any artifacts you need;
  filesystem backups and synchronization tools are outside VideoScope's deletion
  guarantee.
- Rescue filtering is not reconstruction: it cannot recover missing frames,
  clipped samples, black/overexposed detail, or other information absent from the
  readable source.

VideoScope 目前维护 `0.1.x` CPU 系列，并审查 `0.5.x` Video Rescue 开发线。
安全修复会进入最新的兼容补丁版本；预发布版本和开发快照不承诺长期支持。

## Reporting a vulnerability

请不要在公开 issue 中披露尚未修复的漏洞，也不要上传包含隐私的视频、
提示词、证据帧或报告。

仓库托管到 GitHub 后，请使用仓库的 **Security → Report a
vulnerability** 私密报告入口。若该入口尚未启用，请先向仓库维护者提交
一个不含漏洞细节的公开 issue，请求建立私密沟通渠道。

报告应尽量包含：

- 受影响的 VideoScope 版本、操作系统和 Python 版本；
- 最小复现步骤和预期影响；
- 已采取的缓解措施；
- 不含个人目录、密钥和私密媒体的日志摘要。

维护者会确认收到报告、评估影响并协调修复与披露时间。项目目前由志愿者
维护，因此不承诺固定响应时限，但会优先处理可能导致任意命令执行、路径
逃逸、敏感信息泄露或不安全外部调用的问题。

## Security model

- VideoScope 默认 local-first，不上传视频、提示词或报告。
- 基础安装不包含遥测、远程分析服务和自动模型下载。
- FFmpeg/ffprobe 通过参数数组调用，输入路径不会拼接进 shell 命令。
- HTML 报告是本地静态文件；打开不需要远程脚本、字体或统计服务。
- 本地 Web 服务默认绑定回环地址，并拒绝非回环浏览器 Origin 与不可信
  Host；使用 `--allow-network` 会明确扩大信任边界，且没有账户认证。
- 可选模型默认只使用已有本地缓存；下载模型必须得到明确授权。
- 用户仍应只分析有权访问的文件，并及时更新 Python、FFmpeg 和依赖。
- Safe Sharing 的 `privacy-review-private/` 是本机敏感区，可能包含未脱敏
  证据、风险图、计划和预览；不得把它当作公开分享包。
- Safe Sharing 只有在精确计划摘要确认后才修改新副本，并只从固定白名单
  提供 `share-package/` 产物。取消、失败、摘要不匹配或必需检查未通过不得
  产生“已完成”的公开结果。
- 显式删除任务会移除其本地上传与私有材料；使用 CLI 时，用户负责在复核后
  删除整个输出目录。文件系统备份、同步软件和取证恢复不在应用删除保证内。

请勿把 VideoScope 的启发式检测结论作为安全、合规、匿名性证明或身份判断依据。
Safe Sharing 需要人在目标受众语境下复核完整输出，不能保证发现全部风险或实现
绝对安全。
