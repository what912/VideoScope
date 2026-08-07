# Advanced AI: local, grounded, and reviewable

VideoScope Advanced AI is an optional assistant for **C · Long Video to Useful
Content**. It can transcribe a local video, suggest chapters and highlight
ranges, and draft a summary and title. It does not edit or publish the video by
itself. Every suggestion cites source-time evidence and must pass human review,
the ordinary C private preview, exact-plan confirmation, source mapping, and
post-render verification.

高级 AI 是 C 模式的可选本地助手。它可以转写视频、建议章节和精华区间，
并起草摘要与标题；它不会自行剪辑、删除或发布视频。每条建议都必须引用来源
时间区间，并经过人工逐项复核、私有预览、精确计划确认、来源映射和输出后验证。

## Privacy and trust boundary

- The base install remains CPU-only and never imports AI runtimes.
- Video, transcript, frames, prompts, suggestions and review files remain local.
- The semantic provider accepts only loopback Ollama endpoints and rejects
  credentials, remote hosts and query strings.
- VideoScope never pulls an Ollama model. Install the exact model yourself.
- Faster Whisper is optional. A missing model may be downloaded only when the
  user explicitly enables model download for that run.
- Private artifacts live below `ai-review-private/`; they may contain verbatim
  speech or sensitive summaries and are not public report artifacts.
- Provider failure leaves the C CPU workflow available and visible.

## Installation

Base C workflow with a trusted SRT/WebVTT file:

```bash
python -m pip install genvideoscope
```

Optional local ASR:

```bash
python -m pip install "genvideoscope[asr]"
```

Ollama is an external local application, not a Python dependency. Start it on
its default loopback address and make sure the model is already present with
`ollama list`. VideoScope does not recommend one universal model: quality,
memory, license, language support and context limits vary.

## CLI workflow

With a trusted local transcript (no ASR model needed):

```powershell
videoscope assist meeting.mp4 `
  --output runs\meeting-ai `
  --transcript meeting.srt `
  --semantic-model qwen2.5:7b `
  --locale zh-CN
```

Without a transcript, install the `asr` extra. A model download is still denied
unless explicitly authorized:

```powershell
videoscope assist meeting.mp4 `
  --output runs\meeting-ai `
  --semantic-model qwen2.5:7b `
  --asr-model small `
  --asr-language zh `
  --allow-model-download
```

In a non-interactive terminal, `assist` only writes private suggestions. In an
interactive terminal it asks about every item. `--accept-all` is explicit, is
not the default, and does not bypass C's later preview and confirmation gates.

Accepted chapter/highlight ranges can be bridged into C:

```powershell
videoscope content meeting.mp4 `
  --goal selected_clips `
  --output runs\meeting-content `
  --ai-batch runs\meeting-ai\ai-review-private\suggestions.json `
  --ai-review runs\meeting-ai\ai-review-private\review.json
```

The Web workflow is available through `videoscope serve`. Open C mode, create a
content map, expand **Optional / Local Advanced AI**, name an already installed
Ollama model, prepare suggestions, review each item, and apply only accepted
ranges. The UI then returns to the ordinary storyboard and private-preview flow.

## What the output means

- `chapter`: a proposed navigational boundary/range;
- `highlight`: a proposed keep range, not a claim of virality or importance;
- `summary` and `title`: editable text drafts; they never change the timeline;
- `confidence`: provider output, not calibrated probability of correctness;
- `rationale`, ranges and cue IDs: provenance for human verification;
- `limitations`: known reasons the suggestion may be incomplete or wrong.

The system does not detect truth, legal compliance, speaker identity, copyright
ownership, emotional intent, or audience response. It does not use face
recognition and does not create an overall quality or popularity score.

## Troubleshooting

- **Model not found:** run `ollama list`; VideoScope will not download it.
- **Remote endpoint rejected:** use a loopback Ollama endpoint.
- **ASR dependency missing:** install `genvideoscope[asr]`, or provide SRT/VTT.
- **ASR model unavailable:** use a cached model or explicitly allow the run to
  download it after reviewing its terms and size.
- **Provider failed:** continue in the CPU storyboard or correct local settings.
- **Stale review:** suggestions are bound to one C revision; prepare them again.

Faster Whisper and CTranslate2 are optional software dependencies with their
own licenses. Whisper checkpoints and Ollama models have separate model terms.
No model weight is bundled in the repository, wheel or source distribution.

