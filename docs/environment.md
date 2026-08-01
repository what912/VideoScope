# VideoScope 开发环境审计

审计日期：2026-07-28

本次审计只检查本机已有工具，不安装依赖、不修改系统配置。

## 当前环境

| 项目 | 检测结果 | 状态 |
| --- | --- | --- |
| 操作系统 | Microsoft Windows 10.0.26200，64 位 | 可用 |
| CPU | 13th Gen Intel(R) Core(TM) i9-13980HX，x64 | 可用 |
| Python | 3.12.1 | 满足 Python 3.11+ |
| Python 可执行文件 | `C:\Program Files\Python312\python.exe` | 可用 |
| pip | 23.2.1（Python 3.12） | 可用 |
| Python Launcher (`py`) | 未找到 | 非阻塞，可直接使用 `python` |
| 项目虚拟环境 | `.venv` 不存在 | 尚未创建 |
| Git | 2.45.1.windows.1 | 可用 |
| FFmpeg | 未找到 | **阻塞** |
| ffprobe | 未找到 | **阻塞** |
| Node.js | 25.9.0 | 可用，可选 |
| npm | 11.12.1 | 可用，可选 |
| PowerShell | 5.1.26100.8875 | 可用 |

PowerShell 的执行策略会阻止直接运行 `npm.ps1`；当前可使用
`npm.cmd --version` 调用 npm。Node.js 和 npm 仅用于未来可能的 Web
前端，不是当前阶段的阻塞项。

## 结论

Python 版本和 Git 满足开发要求，但当前环境缺少 `ffmpeg` 与
`ffprobe`，因此**还不能开始依赖视频探测与处理的下一阶段开发**。
安装 FFmpeg 并确保两个命令都可从 PATH 调用后，应重新执行本页的
验证命令。

本次审计没有创建虚拟环境，也没有安装任何 Python 包。进入 Python
开发阶段前，应再创建项目虚拟环境。

## 跨平台验证命令

### Windows PowerShell

```powershell
[System.Runtime.InteropServices.RuntimeInformation]::OSDescription
[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
$env:PROCESSOR_ARCHITECTURE
python --version
python -c "import sys; print(sys.executable)"
python -m pip --version
git --version
ffmpeg -version
ffprobe -version
node --version
npm.cmd --version
```

如果安装了 Python Launcher，也可以运行：

```powershell
py -0p
```

### Linux

```bash
uname -a
uname -m
python3 --version
python3 -c 'import sys; print(sys.executable)'
python3 -m pip --version
git --version
ffmpeg -version
ffprobe -version
node --version
npm --version
```

### macOS

```bash
sw_vers
uname -m
python3 --version
python3 -c 'import sys; print(sys.executable)'
python3 -m pip --version
git --version
ffmpeg -version
ffprobe -version
node --version
npm --version
```

在所有平台上，命令能输出版本号并不完全等同于项目兼容；最低要求
仍是 Python 3.11 或更高、Git 可用，且 `ffmpeg` 与 `ffprobe`
均可用。Node.js 是可选依赖。
