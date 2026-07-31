# VideoScope Benchmark 与阈值校准

VideoScope Benchmark 用带有明确时间区间标注的本地视频评估每个 CPU
detector。它是 detector 行为比较工具，不产生跨 detector 的总质量分，
也不会联网、下载模型或修改源视频。

## 1. 运行工程回归集

先用本机 FFmpeg 生成 synthetic fixtures：

```text
python scripts/generate_test_videos.py --force
videoscope benchmark tests/fixtures/manifest.json --output runs/benchmark
```

结果包括：

- `benchmark.json`：完整机器可读结果、配置、环境和逐视频匹配；
- 终端表格：每个配置、每个 detector 的独立摘要。

可重复指定 detector 和配置：

```text
videoscope benchmark dataset/manifest.json \
  --detector near_black \
  --detector possible_freeze \
  --config configs/default.json \
  --config configs/candidate.json \
  --output runs/comparison
```

不同配置不会合并评分。每一行只表示一个 detector 在一个配置下的
结果。

`result_fingerprint` 对 manifest、匹配规则、工具/运行时版本、配置和
全部 detector 指标生成稳定摘要，但排除实际总运行时间。同一输入和
环境的指标应得到同一 fingerprint；`total_runtime_seconds` 是实测
运行信封，允许随机器负载变化。

## 2. 区间匹配与指标

预测事件与同一视频、同一 detector 的标注事件进行确定性一对一匹配。
候选对先按 temporal IoU 降序，再按总边界误差、预测索引和标注索引
排序。一个标注最多匹配一个预测；多个预测覆盖同一标注时，多余预测
是 false positive event。

默认匹配要求 temporal IoU 至少为 `0.1`。如果预测的开始和结束误差
都不超过该视频的 `tolerance_seconds`，即使零长度事件的 IoU 为零，
也可以匹配。`--minimum-iou` 可显式修改 IoU 门槛。

每个 detector 独立报告：

- `temporal_iou`：所有已匹配事件的平均交并比；
- `event_precision = TP / (TP + FP)`；
- `event_recall = TP / (TP + FN)`；
- `event_f1`：precision 与 recall 的调和平均；
- `start_time_error_seconds`：已匹配事件开始时间绝对误差的平均值；
- `end_time_error_seconds`：已匹配事件结束时间绝对误差的平均值；
- `false_positive_events`：未匹配预测事件数；
- `false_positive_duration_seconds`：未匹配预测区间合并后的持续时间。

没有标注事件的负样本会另外汇总
`negative_false_positive_event_count` 和
`negative_false_positive_duration_seconds`。检测器出现
`detector_error` 时，该 case 不作为“无预测”计分，而是单独记录错误。

## 3. Manifest 标注格式

当前 synthetic manifest 使用旧版单一主要异常格式：

```json
{
  "duration_seconds": 6.0,
  "expected_anomaly_type": "black_segment",
  "expected_time_ranges": [
    {"start_seconds": 2.0, "end_seconds": 3.5}
  ],
  "tolerance_seconds": 0.11
}
```

旧格式中，主要异常只给对应 detector 提供正标注；`none` 视频是所有
detector 的负样本。其他 detector 在该异常视频上的结果仍会运行，但
因没有完整人工标注而从该 detector 的指标中排除，避免把可能存在的
次生现象误算为误报。

真实人工标注集应使用逐 detector 格式：

```json
{
  "schema_version": "1.0",
  "dataset_id": "generated-video-review-v1",
  "video_root": "videos",
  "videos": {
    "sample-001.mp4": {
      "duration_seconds": 8.4,
      "tolerance_seconds": 0.1,
      "split": "development",
      "annotations": {
        "near_black": [
          {"start_seconds": 2.1, "end_seconds": 2.9}
        ],
        "possible_freeze": []
      },
      "negative_detectors": [
        "scene_relative_blur",
        "global_flicker"
      ]
    }
  }
}
```

`annotations` 中的空数组和 `negative_detectors` 都表示标注者明确复核
过该 detector 且没有事件。没有出现在这两个位置的 detector 视为
未标注，不参与指标。视频路径必须相对于 manifest，不能是绝对路径或
包含 `..`。

## 4. 构建真实人工标注集

### 4.1 数据选择

应覆盖多个生成模型、版本、分辨率、帧率、时长、内容类型和运动强度。
同时保留没有目标异常的负样本。不要只收集 detector 已经容易发现的
案例，也不要用单个视频决定默认阈值。

记录素材来源、许可、去标识化方式和内容使用授权。隐私视频应保存在
受控本地存储中；manifest 只使用匿名相对文件名，不写用户名或个人
绝对目录。

### 4.2 标记问题区间

标注者应按同一 detector 定义阅读指南，使用浮点秒和半开区间
`[start_seconds, end_seconds)` 标记可观察现象。建议：

1. 先独立观看并标注，不查看 detector 预测；
2. 至少两名标注者处理一部分重叠样本；
3. 对边界或类别分歧进行复核并保留决议记录；
4. 明确记录“已复核且无事件”和“尚未标注”的区别；
5. 保存标注规范版本、标注工具版本和数据内容哈希。

### 4.3 开发集与测试集

先按生成来源或视频族分组，再划分：

- development：用于理解误报、漏报和搜索候选阈值；
- validation：用于选择参数网格中的候选配置；
- held-out test：在阈值冻结后只运行一次，报告最终结果。

同一提示词的变体、同一生成种子、同一源片段的裁剪或转码版本必须放
在同一 split，避免近重复内容泄漏。测试集不能用于反复调参；如果已经
查看测试结果并据此修改阈值，它就不再是独立测试集。

## 5. 小型参数网格校准

校准只在用户明确运行脚本时发生。网格文件把 detector 参数映射到候选
值数组：

```json
{
  "near_black": {
    "mean_luma_threshold": [0.06, 0.08, 0.10],
    "dark_pixel_ratio": [0.90, 0.95]
  }
}
```

运行：

```text
python scripts/calibrate_thresholds.py dataset/manifest.json \
  --detector near_black \
  --grid configs/near-black-grid.json \
  --objective event_f1 \
  --output runs/near-black-calibration
```

可选目标为 `event_f1`、`event_precision`、`event_recall`、
`temporal_iou`、`start_time_error` 或 `end_time_error`。脚本默认最多
运行 64 个组合，输出：

- `benchmark.json`：所有配置的完整 Benchmark；
- `calibration-results.json`：每个候选参数、目标值和 detector 结果；
- `suggested-config.json`：最佳候选的普通 AnalysisConfig。

脚本不会修改 detector 源码、默认配置或输入配置文件。建议配置仍需在
独立 validation 和 held-out test 上验证。

## 6. 防止过拟合和结果表述

`tests/fixtures/generated` 只验证工程链路和已知合成现象。它具有极低
分辨率、固定帧率、程序化图案和单一明确异常，不能代表真实生成视频的
内容分布、编码差异或主观歧义。

因此可以表述为：

> 在指定 synthetic regression manifest、工具版本和配置下，该
> detector 得到以下事件匹配结果。

不能表述为：

> VideoScope 在真实生成视频上的准确率是该 synthetic 结果。

任何真实准确率或性能声明都必须同时公开数据范围、标注方法、split、
配置、工具版本、FFmpeg 版本、硬件与限制。
