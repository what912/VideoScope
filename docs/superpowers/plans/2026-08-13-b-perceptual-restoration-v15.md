# B 模块感知修复 V15 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 B 模块自动定位并真实改善持续卷积型模糊、局部新增窄带尖锐声和
场景内高频全局震动，同时以 V14 的三个可见/可听失败作为新的回归基线。

**Architecture:** 保留现有“扫描 → 私有预览 → 明确确认 → 执行 → 独立验证”
流程。新增三个彼此隔离的原生 CPU 恢复边界：受约束盲去卷积、短窗窄带干扰
恢复、逐帧锚点稳定；规划器只复制测量生成的严格参数，预览与最终执行复用同一
实现，验证器从源视频重新独立测量，不相信执行器声明。

**Tech Stack:** Python 3.11、Pydantic 2、NumPy、OpenCV、FFmpeg/ffprobe、
pytest、Ruff、mypy；离线、CPU-only、无模型下载。

## Global Constraints

- 失败基线固定为 `VideoScope-B-Improved-Fixed-Final-V14.mp4`；不得再用 V13
  证明成功。
- 生产逻辑不得读取 fixture 名、固定哈希、演示绝对路径或预先知道的核半径、
  880 Hz、正弦位移参数。
- 所有外部命令使用参数数组和 `shell=False`；错误信息不泄露个人绝对路径。
- 原视频只读，处理区间为半开区间，区间外差异必须保持在编码基线内。
- 所有阈值来自严格 Pydantic 配置，所有测量参数进入 plan digest。
- preview 与 final 必须复用同一实现和同一参数；inherited action 不得重复执行。
- 无可靠证据或任一副作用门槛失败时必须 `needs_review`，不得伪造修复成功。
- 基础路径不得联网、下载模型或依赖 GPU。
- 每次修改后运行 `python scripts/validate.py`；真实媒体结果必须另外逐段测量。
- 未获明确授权时，不执行本计划中的 commit、push、PR、发布或部署检查点。

---

## 文件结构

- Create: `src/videoscope/rescue/deblur.py` — 模糊核估计、去卷积、视频流式渲染。
- Create: `src/videoscope/rescue/tonal.py` — 短窗谱测量、目标频率选择、流式陷波与边界混合。
- Modify: `src/videoscope/rescue/stabilization.py` — 增加逐帧锚点测量和直接校正。
- Modify: `src/videoscope/rescue/models.py` — 新增 `deblur` 动作和值稳定配置。
- Modify: `src/videoscope/rescue/assessment.py` — 注入三类测量，不硬编码演示信息。
- Modify: `src/videoscope/rescue/planner.py` — 将测量结果复制进确定计划。
- Modify: `src/videoscope/rescue/executor.py` — 编排三个 native restorer。
- Modify: `src/videoscope/rescue/preview.py` — 复用 native restorer 生成同范围预览。
- Modify: `src/videoscope/rescue/verification.py` — 新感知与短窗独立门禁。
- Modify: `src/videoscope/rescue/artifacts.py` — 公开动作/验证说明与 allowlist。
- Modify: `src/videoscope/rescue/__init__.py` — 导出稳定公共类型。
- Modify: `docs/rescue-schema.md` — 动作、参数、限制和完成语义。
- Create: `scripts/verify_b_v15_demo.py` — 真实演示的本地工程门禁，不参与生产决策。
- Create: `tests/rescue/test_deblur.py` — 去卷积 RED/GREEN。
- Create: `tests/rescue/test_tonal.py` — 短窗窄带 RED/GREEN。
- Modify: `tests/rescue/test_stabilization.py` — 锚点稳定 RED/GREEN。
- Modify: `tests/rescue/test_assessment.py` — 自动定位和低置信回退。
- Modify: `tests/rescue/test_planner.py` — 参数/digest/范围绑定。
- Modify: `tests/rescue/test_executor.py` — 执行顺序、单次应用和失败清理。
- Modify: `tests/rescue/test_preview.py` — preview/final 等价性。
- Modify: `tests/rescue/test_verification.py` — 新门槛及 `needs_review`。
- Create: `tests/scripts/test_verify_b_v15_demo.py` — 真实门禁脚本契约。

---

### Task 1：建立严格公共测量与计划契约

**Files:**
- Modify: `src/videoscope/rescue/models.py`
- Modify: `src/videoscope/rescue/__init__.py`
- Modify: `tests/rescue/test_models.py`

**Interfaces:**
- Produces: `RescueActionKind.DEBLUR = "deblur"`。
- Produces: `RescueEffectiveConfig.deblur_algorithm_version`、
  `tonal_algorithm_version`、`anchor_stabilization_algorithm_version`。
- Consumes later: Task 2–7 将这些值和每个动作的全部参数绑定进 canonical plan。

- [ ] **Step 1：写 enum 和配置的失败测试**

```python
def test_v15_native_restoration_versions_are_strict_and_digest_bound() -> None:
    original = RescueEffectiveConfig()
    assert original.deblur_algorithm_version == "1"
    assert original.tonal_algorithm_version == "1"
    assert original.anchor_stabilization_algorithm_version == "1"
    with pytest.raises(ValidationError):
        RescueEffectiveConfig(deblur_algorithm_version="2")
    first = make_plan_payload()
    second = make_plan_payload()
    second_config = dict(second["effective_config"])
    second_config["deblur_algorithm_version"] = "2"
    second["effective_config"] = second_config
    assert make_rescue_plan_digest(first) != make_rescue_plan_digest(second)


def test_deblur_is_a_stable_rescue_action_kind() -> None:
    assert RescueActionKind("deblur") is RescueActionKind.DEBLUR
```

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_models.py -k "v15_native or deblur_is" -q
```

Expected: enum/字段不存在而失败，不得是导入或拼写错误。

- [ ] **Step 3：实现最小严格模型**

```python
class RescueActionKind(StrEnum):
    # existing values stay byte-for-byte stable
    DEBLUR = "deblur"


class RescueEffectiveConfig(RescueModel):
    deblur_algorithm_version: Literal["1"] = "1"
    tonal_algorithm_version: Literal["1"] = "1"
    anchor_stabilization_algorithm_version: Literal["1"] = "1"
```

- [ ] **Step 4：运行 GREEN 与兼容测试**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_models.py tests/rescue/test_artifacts.py -q
```

Expected: 全部通过，旧 JSON 仍能用默认值解析，未知额外字段仍拒绝。

- [ ] **Step 5：检查点（只有另行授权后才提交）**

Suggested commit: `feat: define perceptual Rescue contracts`

---

### Task 2：实现受约束盲去卷积测量与单帧恢复

**Files:**
- Create: `src/videoscope/rescue/deblur.py`
- Create: `tests/rescue/test_deblur.py`
- Modify: `src/videoscope/rescue/__init__.py`

**Interfaces:**
- Produces: `DeblurConfig`。
- Produces: `BlurKernelEstimate(kernel_kind, radius, regularization,
  confidence, edge_width_before, predicted_edge_width_after,
  ringing_ratio, noise_gain_ratio)`。
- Produces: `estimate_blur_kernel(frames, config) -> BlurKernelEstimate | None`。
- Produces: `restore_deblurred_frame(frame, estimate, config) -> NDArray[np.uint8]`。
- Consumes: only input pixels/config; no filename/hash/path branch。

- [ ] **Step 1：写已知核和副作用 RED 测试**

构造 720p 以下的本地高对比文字/线条图，分别应用 box/Gaussian 半径 1–5。
测试必须断言：

```python
estimate = estimate_blur_kernel((blurred_a, blurred_b), DeblurConfig())
assert estimate is not None
restored = restore_deblurred_frame(blurred_a, estimate, DeblurConfig())
assert edge_spread_width(restored) < edge_spread_width(blurred_a) * 0.72
assert structural_similarity(restored, clean) > structural_similarity(blurred_a, clean)
assert ringing_ratio(restored, clean) <= 0.08
assert temporal_difference(restored_a, restored_b) <= configured_limit
```

另写：纯色、几乎无边缘、极强模糊、运动不一致帧返回 `None`；非法半径、
NaN/Infinity、噪声或振铃越界拒绝。

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_deblur.py -q
```

Expected: `videoscope.rescue.deblur` 不存在而 collection 失败。

- [ ] **Step 3：实现纯测量函数**

`DeblurConfig` 精确包含：候选核类型、半径 1–5、正则候选、最少边缘数、
最大噪声增益、最大振铃、最小边缘宽度改善、最小 SSIM/重模糊一致性改善、
边缘渐消宽度和区间过渡秒数。候选按固定 `(kind, radius,
regularization)` 排序；分数并列按顺序选择。

估计流程：亮度通道 → 边缘 mask → 每候选去卷积 → 重模糊一致性 →
边缘宽度/连续性 → 振铃/噪声 → 跨帧一致性。任何硬门槛失败的候选不进入
排序。

- [ ] **Step 4：实现最小去卷积**

使用反射边缘渐消和正则化频域 Wiener，只改 Y 通道：

```python
restored_fft = (
    np.conj(kernel_fft)
    * source_fft
    / (np.abs(kernel_fft) ** 2 + estimate.regularization)
)
```

恢复后限制到合法 8-bit 范围，不附加多重 unsharp；通过输入/输出边缘混合
抑制边界振铃。

- [ ] **Step 5：运行 GREEN、确定性与属性测试**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_deblur.py -q
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\ruff.exe check `
  src/videoscope/rescue/deblur.py tests/rescue/test_deblur.py
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\mypy.exe `
  src/videoscope/rescue/deblur.py
```

Expected: 测试、lint、类型检查全通过；相同帧/配置产生相同 estimate。

- [ ] **Step 6：检查点（只有另行授权后才提交）**

Suggested commit: `feat: add bounded CPU deconvolution`

---

### Task 3：实现流式去卷积视频恢复器

**Files:**
- Modify: `src/videoscope/rescue/deblur.py`
- Modify: `tests/rescue/test_deblur.py`
- Modify: `tests/rescue/test_executor.py`

**Interfaces:**
- Produces: `render_deblurred_video(source, output, ranges, estimate, config,
  runner, ffmpeg, cancellation_callback) -> None`。
- Guarantees: 原音频复用、CFR 时间戳/帧数/时长保持、同目录临时文件和原子发布。

- [ ] **Step 1：写范围、边界和生命周期 RED**

测试使用短合成视频并断言：只在 `[start,end)` 调用恢复；区间外帧等于
codec-aligned reference；边界过渡连续；取消、解码失败、写入失败不留下
partial；source/output alias 与已有 destination fail closed。

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_deblur.py -k "video or range or boundary or cancel" -q
```

Expected: renderer 不存在而失败。

- [ ] **Step 3：实现流式帧处理**

OpenCV 逐帧读取，按真实 CFR 时间戳选择半开区间；恢复帧与干帧在配置边界
内使用 smoothstep/raised-cosine 权重混合。用 FFV1 本地无损中间文件，随后
通过共享 runner 以显式 `libx264 -preset slow -crf 0 -pix_fmt yuv420p` 和
原音频 mux；全部输出先写私有临时路径，再原子替换。

- [ ] **Step 4：运行 GREEN 与真实短视频检查**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_deblur.py tests/rescue/test_executor.py -k "deblur or range" -q
```

Expected: 全部通过；FFmpeg 缺失时结构化错误；中文/空格路径通过。

- [ ] **Step 5：检查点（只有另行授权后才提交）**

Suggested commit: `feat: render bounded deblur restoration`

---

### Task 4：实现短窗窄带干扰检测和无爆音恢复

**Files:**
- Create: `src/videoscope/rescue/tonal.py`
- Create: `tests/rescue/test_tonal.py`
- Modify: `src/videoscope/rescue/audio.py`
- Modify: `src/videoscope/rescue/executor.py`
- Modify: `src/videoscope/rescue/__init__.py`

**Interfaces:**
- Produces: `TonalInterferenceConfig`。
- Produces: `InterferenceTone(frequency_hz, start_seconds, end_seconds,
  source_peak_dbfs, baseline_peak_dbfs, confidence)`。
- Produces: `detect_interference_tones(samples, sample_rate, config)`。
- Produces: `restore_tonal_interference(source, output, tones, config,
  runner, ffmpeg, cancellation_callback)`。

- [ ] **Step 1：写频率选择和短窗边界 RED**

构造 48 kHz PCM：全程 220 Hz，25–32 秒新增 880 Hz。断言 detector 只选择
880 Hz；恢复后 880 Hz 至少下降配置值，220 Hz 衰减不超过 1 dB；
31.8–32.2 秒每 50 ms 窗口没有 click/峰值回弹。另测漂移音、多音、宽带噪声、
弱孤立谱峰和无局部基线。

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_tonal.py -q
```

Expected: 新模块不存在而失败。

- [ ] **Step 3：实现 50 ms 重叠谱测量**

使用配置化窗长/步长、Hann 窗和局部前后基线。只有候选区间谱峰相对基线
增量、持续窗数、频率稳定性和置信度全部通过时才生成 `InterferenceTone`。
持续存在于控制区间的 220 Hz 不得进入动作参数。

- [ ] **Step 4：实现流式双二阶陷波与 raised-cosine 混合**

为每个目标频率由配置 Q 值生成稳定 biquad 系数，按通道保留滤波器状态；
输出为 `dry * (1 - weight) + wet * weight`，weight 在区间两端按配置毫秒数
连续变化。处理后写 PCM 临时文件，再用共享 runner 与原视频 mux，固定
48 kHz/源采样率和受限 AAC 码率。不得使用硬 `enable` 切换。

- [ ] **Step 5：运行 GREEN 与命令边界测试**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_tonal.py tests/rescue/test_audio.py `
  tests/rescue/test_executor.py -k "tone or tonal or noise" -q
```

Expected: 目标频率、非目标频率、边界、采样率、取消和清理测试全通过。

- [ ] **Step 6：检查点（只有另行授权后才提交）**

Suggested commit: `feat: remove measured tonal interference safely`

---

### Task 5：实现逐帧锚点稳定

**Files:**
- Modify: `src/videoscope/rescue/stabilization.py`
- Modify: `src/videoscope/rescue/assessment.py`
- Modify: `tests/rescue/test_stabilization.py`
- Modify: `tests/rescue/test_assessment.py`

**Interfaces:**
- Produces: `select_stable_anchor(frames, config) -> int | None`。
- Produces: `estimate_anchor_corrections(frames, config,
  scene_boundaries=()) -> tuple[MotionTransform, ...]`，每项 semantics 为
  `frame_correction`。
- Consumes: existing `render_stabilized_video` after tightening coverage rules。

- [ ] **Step 1：写 24 fps 周期位移 RED**

用固定网格加 `x=14*sin(2π*2t)`、`y=7*sin(2π*1.5t)` 的 24 fps 帧序列。
先证明旧 8 fps 相邻轨迹插值的 median/P90 超出 `0.5/1.0 px`，再断言新 API
对每一帧直接锚点校正后达到门槛。加入轻微旋转/缩放、物体局部运动、场景切换、
有意单向平移和连续低置信失败样例。

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_stabilization.py -k "anchor or full_rate or residual" -q
```

Expected: anchor API 不存在或旧实现超过门槛而失败。

- [ ] **Step 3：实现锚点选择和直接配准**

按特征覆盖、RANSAC inlier、residual、场景边界距离选择确定锚点；每帧直接
估计相对锚点的部分仿射变换，不累计相邻误差。只有背景全局一致性通过时才
推荐；连续低置信或单向相机运动返回 neutral/`needs_review`。

- [ ] **Step 4：把 refinement 提升到源帧率**

`RescueAssessmentConfig` 增加严格上限 `maximum_anchor_sample_rate=30` 和
`maximum_anchor_frames`。对于 24/30 fps 输入，抖动候选范围按源帧率抽取；
超过预算时停止自动执行并记录 limitation，不静默降到 8 fps。

- [ ] **Step 5：收紧 renderer 覆盖与插值**

锚点 correction 必须覆盖确认区间的每个源帧时间戳；仅允许单个低置信帧
做有界插值。整个区间使用统一安全 crop；不再让 `range_padding_seconds=1`
掩盖 scene 外处理。

- [ ] **Step 6：运行 GREEN**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_stabilization.py tests/rescue/test_assessment.py -q
```

Expected: 所有既有稳定测试和新增 anchor 测试通过；有意运镜/切镜不产生动作。

- [ ] **Step 7：检查点（只有另行授权后才提交）**

Suggested commit: `feat: stabilize shake against scene anchors`

---

### Task 6：整合扫描、规划、预览与最终执行

**Files:**
- Modify: `src/videoscope/rescue/assessment.py`
- Modify: `src/videoscope/rescue/planner.py`
- Modify: `src/videoscope/rescue/executor.py`
- Modify: `src/videoscope/rescue/preview.py`
- Modify: `src/videoscope/rescue/commands.py`
- Modify: `src/videoscope/rescue/artifacts.py`
- Modify: `tests/rescue/test_assessment.py`
- Modify: `tests/rescue/test_planner.py`
- Modify: `tests/rescue/test_executor.py`
- Modify: `tests/rescue/test_preview.py`
- Modify: `tests/rescue/test_commands.py`

**Interfaces:**
- Consumes: Task 2–5 的 estimates/restorers。
- Produces: 一个 plan 中互不重复的 `deblur`、`denoise_audio`（含
  `interference_profiles`）和 `stabilize(method="anchor_v1")` 动作。
- Produces: faithful 的 `applied_action_ids`，improved 按该集合跳过 inherited。

- [ ] **Step 1：写自动定位和计划绑定 RED**

两组不同像素/频谱/运动观测必须产生不同参数和 digest；相同输入重复运行
必须相同。断言动作范围来自测量而非 fixture 名：软化范围、音频范围、抖动
范围各自精确；未知/低置信不生成动作。

- [ ] **Step 2：写 preview/final 等价和单次应用 RED**

注入 recording restorers，断言 private faithful/improved preview 与 final 使用
相同参数、范围和算法版本；faithful 已执行的三个动作进入
`applied_action_ids`，improved 不得再次执行；任一 native stage 失败清理所有
partial 且原源文件 hash 不变。

- [ ] **Step 3：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_assessment.py tests/rescue/test_planner.py `
  tests/rescue/test_executor.py tests/rescue/test_preview.py `
  tests/rescue/test_commands.py -k "deblur or tonal or anchor or inherited" -q
```

- [ ] **Step 4：最小整合**

评估服务产生完整严格参数；planner 只复制，不重新推导。executor 顺序固定为：
结构修复 → 去卷积 → 窄带恢复 → 锚点稳定；每一步输出为下一步输入并原子替换。
preview 调用同一三个 restorer。旧 `SHARPEN` 仅保留给轻微软化，严重持续软化
只能走 `DEBLUR` 或 `needs_review`。

- [ ] **Step 5：运行 GREEN 和跨平台路径测试**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_assessment.py tests/rescue/test_planner.py `
  tests/rescue/test_executor.py tests/rescue/test_preview.py `
  tests/rescue/test_commands.py -q
```

Expected: 全部通过，包括空格、中文路径、取消、失败清理和 source hash 不变。

- [ ] **Step 6：检查点（只有另行授权后才提交）**

Suggested commit: `feat: integrate perceptual Rescue restorers`

---

### Task 7：建立独立感知验证与诚实状态门禁

**Files:**
- Modify: `src/videoscope/rescue/verification.py`
- Modify: `tests/rescue/test_verification.py`
- Modify: `src/videoscope/rescue/artifacts.py`
- Modify: `docs/rescue-schema.md`

**Interfaces:**
- Adds checks: `deblur_edge_recovery`、`deblur_ringing`、
  `deblur_temporal_consistency`、`tonal_interference_reduction`、
  `tonal_boundary_transient`、`anchor_stabilization_residual`。
- Changes: these checks are required for any artifact containing the corresponding
  confirmed action; failure forces that artifact to `needs_review`。

- [ ] **Step 1：写旧指标误判 RED**

构造“Laplacian 很高但文字有 halo”的 candidate，断言旧
`perceptible_sharpness_improvement` 会误判而新门禁必须失败。构造平均事件数
为零但 50 ms 末端有尖峰的 candidate，新门禁必须失败。构造运动下降 80%
但残余 median=4 px 的 candidate，新门禁必须失败。

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_verification.py -k "deblur or tonal_boundary or anchor_stabilization" -q
```

Expected: 新 check 缺失或状态错误而失败。

- [ ] **Step 3：扩展独立 measurement provider**

增加：多尺度边缘宽度/连续性/振铃/时序差异；50 ms 短窗频率峰值、边界
crest/能量跳变；逐帧锚点残余 motion median/P90。验证器只接收源、候选、
确认范围和严格配置，不能复用 executor 的中间测量结果。

- [ ] **Step 4：实现 required checks 与状态传播**

去卷积必须同时通过边缘改善、振铃、噪声、时序一致性；音频必须同时通过
目标频率下降、非目标保真和边界无瞬态；稳定必须满足 median ≤ 0.5 px、
P90 ≤ 1.0 px、crop 和可靠变换数。任一失败使对应 faithful/improved
`needs_review`，但不得删除另一份独立通过的 artifact。

- [ ] **Step 5：运行 GREEN、报告和 schema 测试**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue/test_verification.py tests/rescue/test_artifacts.py `
  tests/rescue/test_report.py -q
```

Expected: 检查顺序稳定、数值有限、报告无绝对路径、失败状态真实。

- [ ] **Step 6：检查点（只有另行授权后才提交）**

Suggested commit: `feat: verify perceptual Rescue outcomes`

---

### Task 8：建立 V14 对照的真实演示门禁

**Files:**
- Create: `scripts/verify_b_v15_demo.py`
- Create: `tests/scripts/test_verify_b_v15_demo.py`
- Modify: `.gitignore` only if generated review artifacts are not already ignored

**Interfaces:**
- CLI:

```text
python scripts/verify_b_v15_demo.py \
  --source SOURCE \
  --v14 V14 \
  --candidate CANDIDATE \
  --clean-reference CLEAN \
  --output REVIEW_ROOT \
  --ffmpeg FFMPEG \
  --ffprobe FFPROBE
```

- Produces local-only: `metrics.json`、`frame-contact-sheet.png`、
  `audio-short-windows.json`、`motion-residual.json`。

- [ ] **Step 1：写 CLI、原子写入和负例 RED**

测试无文件、错误 hash/时长、缺音频、candidate=source、绝对路径输出、NaN、
旧门槛“仅 sharpness/event count/50% reduction”均 fail closed；输出 JSON 只含
相对 artifact 路径。

- [ ] **Step 2：运行 RED**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/scripts/test_verify_b_v15_demo.py -q
```

- [ ] **Step 3：实现本地门禁脚本**

脚本只测量，不改变生产判断：

- 从同一 HyperFrames composition 生成未加异常的 private clean reference；
- 抽取 6.0 秒 source/V14/candidate/clean 同分辨率帧和局部放大；
- 31.8–32.2 秒每 50 ms 报告 220/880 Hz、RMS、crest 和相邻窗跳变；
- 32–36 秒逐帧估计 anchor residual，报告 median/P90/可靠数；
- 完整 decode、duration、fps、48 kHz、A/V、区间外 MAE、源 hash。

- [ ] **Step 4：运行 GREEN 与确定性测试**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/scripts/test_verify_b_v15_demo.py -q
```

Expected: 两次输入相同生成 byte-stable JSON；图片 SHA 稳定；失败不覆盖旧结果。

- [ ] **Step 5：检查点（只有另行授权后才提交）**

Suggested commit: `test: gate B restoration against V14 failures`

---

### Task 9：完整验证、真实准备和候选专属人工确认

**Files:**
- No production edits unless a fresh failing test identifies a real defect.
- Generated local-only: `runs/b-v15-*` and user review copies under Downloads.

**Interfaces:**
- Consumes: Task 1–8 complete implementation。
- Produces: fresh preparation/preview; exact plan digest/action IDs/ranges for user
  confirmation; only after confirmation, new faithful/improved candidates。

- [ ] **Step 1：运行聚焦与统一验证**

Run:

```powershell
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe -m pytest `
  tests/rescue tests/scripts/test_verify_b_v15_demo.py -q
& C:\Users\吴少泽\Documents\VideoScope\.venv\Scripts\python.exe scripts/validate.py
```

Expected: Ruff、format、mypy、base pytest、isolated native Rescue 均 exit 0。

- [ ] **Step 2：用原始 42 秒源重新 prepare**

不得复用 V14 plan/confirmation。使用仓库固定 FFmpeg/ffprobe、原始 source 和
全新 `runs/b-v15-*` 工作区。记录源 SHA、plan digest、三个动作完整 ID/参数/
范围、limitations 和 private preview 路径。

- [ ] **Step 3：停止并请求候选专属确认**

向用户展示同范围 source/faithful/improved preview 以及三个动作的精确内容。
在用户明确接受 exact digest 和 action IDs 前，不调用 execute。

- [ ] **Step 4：确认后执行一次**

生成独立 faithful/improved，不覆盖任何旧文件；完整 decode 后运行 Task 8
真实门禁。源视频执行前后 SHA 必须完全一致。

- [ ] **Step 5：逐项阅读真实证据**

必须同时满足：

- 第 6 秒 candidate 相对 source 的边缘扩散下降、相对 clean 的结构相似度
  提升、ringing/noise/temporal 均通过；
- 25–32 秒 880 Hz 显著下降、220 Hz 保真，31.8–32.2 秒 50 ms 窗无新瞬态；
- 32–36 秒 median ≤ 0.5 px、P90 ≤ 1.0 px、crop/区间外/帧数/时间戳通过；
- 两份输出状态与每个 required check 一致，不能用 optional pass 掩盖 required
  failure。

- [ ] **Step 6：交付用户播放验收**

复制为新文件名（不得称 Final，直到用户观看通过），同时提供 contact sheet 和
三份 JSON 测量。用户若仍指出具体残留，先新增能复现该残留的失败测试，再进入
下一轮，不继续盲调参数。

- [ ] **Step 7：最终检查点（只有另行授权后才提交/发布）**

Suggested commit: `fix: deliver perceptually gated Rescue restoration`

禁止在本任务中自动 push、PR、发布或部署。

---

## 计划自审结果

- **规格覆盖：** 三个根因、CPU/offline、参数绑定、同实现 preview/final、
  `needs_review`、V14/真实门禁、人工确认均有对应 Task。
- **反作弊：** 生产代码没有演示文件名、固定 hash、固定频率或固定运动公式；
  已知 clean reference 只属于 Task 8 工程验收。
- **类型一致性：** Task 2–5 产生的 models/restorers 由 Task 6 消费，Task 7
  独立测量，Task 8 只审计产物。
- **安全：** 原视频只读、原子输出、相对公开路径、参数数组、取消清理和确认
  绑定均有明确失败测试。
- **无占位符：** 计划没有 TBD/TODO/“类似前项”步骤。
