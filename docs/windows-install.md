# VideoScope Windows 安装与首次使用 / Windows setup

状态：安装器源码和自动构建流程已就绪；公开 Release 附件必须在 Windows
CI 安装、启动、健康检查和卸载全部通过后再提供下载。不要从非
`what912/VideoScope` 来源下载安装包。

## 零基础首次使用

### 1. 下载

1. 打开 `https://what912.github.io/VideoScope/connect`。
2. 点击“下载 Windows 安装包”。
3. 下载 Release 中的 `VideoScope-Setup-x64.exe` 和同名 `.sha256` 文件。
4. 确认 Release 发布者和仓库都是 `what912/VideoScope`。正式代码签名完成
   前，Windows 可能显示“未知发布者”；来源或校验值不一致时不要继续。

校验下载文件（把路径替换为实际下载位置）：

```powershell
Get-FileHash .\VideoScope-Setup-x64.exe -Algorithm SHA256
```

输出应与 Release 的 `.sha256` 内容完全一致。

### 2. 安装并启动

1. 双击 `VideoScope-Setup-x64.exe`。
2. 保留推荐选项并完成安装。安装范围只有当前 Windows 用户，不需要管理员
   权限，不创建 Windows 服务，不开放防火墙，也不允许局域网访问。
3. 安装完成后保持“启动 VideoScope 本地连接器”勾选。
4. 连接器窗口会自动检查 `ffmpeg` 和 `ffprobe`。二者都可用时显示就绪；缺少
   时显示具体原因，并在用户确认后才可调用 Winget 安装独立 FFmpeg。
5. 连接器确认本地服务已在 `127.0.0.1:8765` 启动后，才会自动打开官方连接页。

安装包包含 VideoScope 和所需 Python 运行时，但不捆绑 FFmpeg、AI 模型或
API Key。

### 3. 配对浏览器

1. 保持“VideoScope 本地连接器”窗口打开。
2. 在窗口中找到“浏览器配对码”，点击“复制”。
3. 回到连接页，把配对码粘贴到输入框并点击“配对当前浏览器”。
4. 配对码不是 8765、进程号、Windows 密码或 AI API Key。它只在本次启动后
   10 分钟内有效，只能成功使用一次；连续错误输入会暂时限速。
5. 配对成功后点击“开始第一次完整分析”，拖入视频并选择任务模式。

浏览器会保存一个有过期时间的会话令牌。关闭连接器会清除内存中的会话和
BYOK 密钥；重新启动后需要重新配对。

### 4. 以后再次使用

从开始菜单或桌面图标打开 VideoScope，再访问公开网站即可。网站的“启动已
安装的连接器”按钮使用 `videoscope://start`；浏览器首次询问是否允许打开
VideoScope 时选择允许。连接器只在当前用户会话中运行，关闭窗口即停止。

### 5. 开始解决问题

- **检查**：定位黑屏、近重复帧、相对清晰度下降和潜在全局闪烁；
- **A · 发布就绪**：生成经过验证的兼容 MP4、封面与技术报告；
- **D · 安全分享**：由用户复核隐私区间后生成脱敏分享副本；
- **B · 视频抢救**：按精确区间执行可回退的播放兼容与有限改善；
- **C · 有用内容**：生成可复核章节、精选片段和来源映射；
- **高级 AI / BYOK**：由用户自己的供应商账户承担费用，密钥只保存在本机
  连接器内存。远程发送前必须明确确认数据范围。

## 常见问题

### 网站一直显示“连接器未运行”

确认连接器窗口没有关闭；点击“启动已安装的连接器”；允许浏览器访问本地
网络；关闭占用 8765 端口的其他程序。公司设备策略可能阻止 HTTPS 页面访问
回环地址，此时可先在普通个人浏览器中测试。

### FFmpeg 显示缺失

点击连接器中的 Winget 安装按钮并确认，或按 FFmpeg 官方说明手动安装。
VideoScope 不会静默修改系统，也不会从不明地址下载二进制文件。

### 配对码无效

检查是否只复制了配对码本身。配对码已使用、超过 10 分钟或错误次数过多时，
关闭并重新打开连接器以生成新码。

### 如何卸载

在 Windows“设置 → 应用 → 已安装的应用”中卸载 VideoScope。卸载器会先请求
正在运行的连接器退出，再删除当前用户安装目录和 `videoscope://` 注册。

## Developer fallback / 开发者备用方式

公开 Release 尚未附加安装器时，开发者可以从源码运行：

```powershell
git clone https://github.com/what912/VideoScope.git
cd VideoScope
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[web]"
videoscope doctor
videoscope serve --port 8765
```

This fallback requires Python and FFmpeg. Keep the terminal open, copy the
pairing code printed immediately after startup, and never paste an AI API key
into the public website.
