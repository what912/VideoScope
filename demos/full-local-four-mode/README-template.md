# {title}

本指南只呈现本机实际审计状态；`needs_review`、`not_verified` 和失败不会被改写成成功。
This guide preserves actual local audit states; it never rewrites review-needed, unverified, or failed results as success.

## 当前结果 / Current results

{workflow_table}

## 零基础操作步骤 / Zero-beginner steps

{steps}

Video Rescue 的可用动作由实际扫描决定。本演示不保证总能找到或纠正所有异常。
Available Video Rescue actions depend on measured evidence; this demo does not promise that every anomaly is always found or corrected.

## 限制 / Limitations

{limitations}

Safe Sharing 即使完成技术检查，也仍要求分享前进行最终人工复核。
Safe Sharing still requires final human review before sharing, even after technical checks complete.

## 第三方运行时 / Third-party runtime

- GSAP 3.15.0, Copyright GreenSock. Licensed under the
  [GreenSock standard license](https://gsap.com/standard-license).
- The runtime is embedded offline in the demo composition; it is not fetched
  from a CDN during playback.
- Embedded runtime SHA-256:
  `92bb9a96476f983d212a2bc4f54c889039c1696dd4461d40a736860938570fbb`.
