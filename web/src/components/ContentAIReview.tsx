import { useMemo, useState } from "react";
import {
  applyContentAI,
  cancelContentAI,
  prepareContentAI,
  reviewContentAI,
} from "../api";
import type { ContentLocale } from "../contentI18n";
import type {
  AdvancedAIPrepareOptions,
  AISuggestion,
  AISuggestionBatch,
  AIReviewDecision,
  AIReviewDecisionKind,
  ContentJobResponse,
  ConnectorProviderProfile,
} from "../types";
import { ProviderSettings } from "./ProviderSettings";

export interface ContentAIReviewApi {
  prepare(jobId: string, options: AdvancedAIPrepareOptions): Promise<AISuggestionBatch>;
  review(jobId: string, decisions: AIReviewDecision[]): Promise<unknown>;
  apply(jobId: string, revision: number): Promise<ContentJobResponse>;
  cancel(jobId: string): Promise<void>;
}

const DEFAULT_API: ContentAIReviewApi = {
  prepare: prepareContentAI,
  review: reviewContentAI,
  apply: applyContentAI,
  cancel: cancelContentAI,
};

type DraftDecision = {
  decision: AIReviewDecisionKind;
  content: string;
  start: number | null;
  end: number | null;
};

const COPY = {
  en: {
    eyebrow: "OPTIONAL / LOCAL ADVANCED AI",
    title: "Ask local models to structure the story",
    intro: "Generate grounded chapter, highlight, summary and title suggestions. Nothing is applied until you review every item.",
    localDisclosure: "Uses your local Ollama service and optional local Faster Whisper. Video and transcript stay on this computer. VideoScope never pulls an Ollama model automatically.",
    remoteDisclosure: "The selected BYOK provider receives only the bounded transcript and structural evidence required for this run after your explicit approval. Your API key stays in connector memory; provider charges are paid by your own account.",
    model: "Ollama model already installed",
    endpoint: "Loopback Ollama endpoint",
    language: "ASR language (optional)",
    asr: "Faster Whisper model",
    download: "Allow Faster Whisper model download for this run",
    prepare: "Prepare private AI suggestions",
    preparing: "Analyzing locally…",
    cancel: "Cancel local AI preparation",
    reviewTitle: "Review grounded suggestions",
    accept: "Accept",
    reject: "Reject",
    edit: "Edit",
    confidence: "Model confidence",
    evidence: "Source evidence",
    limitations: "Limitations",
    saveReview: "Save exact review",
    saving: "Saving review…",
    apply: "Apply accepted ranges to storyboard",
    applied: "Accepted ranges were added. Continue with the ordinary storyboard and private preview gates below.",
    noRange: "Text suggestion; it will not change the video timeline.",
    required: "Enter the exact ID of a model already available in Ollama.",
    provider: "Content intelligence provider",
    localProvider: "Local Ollama (no remote data transfer)",
    remoteConsent: "I approve sending the bounded transcript and structural ranges to this provider for this run.",
    remoteConsentRequired: "Approve the described remote data transfer before using BYOK AI.",
  },
  "zh-CN": {
    eyebrow: "可选 / 本地高级 AI",
    title: "让本地模型协助梳理视频内容",
    intro: "生成有来源证据的章节、精华、摘要和标题建议。你逐项审核前，任何建议都不会生效。",
    localDisclosure: "使用本机 Ollama 与可选的本地 Faster Whisper。视频和转写不会离开这台电脑；VideoScope 绝不会自动拉取 Ollama 模型。",
    remoteDisclosure: "只有在你明确同意后，所选 BYOK 供应商才会收到本次所需的有界转写和结构证据。API Key 仅留在连接器内存，调用费用由你的供应商账户承担。",
    model: "本机已安装的 Ollama 模型",
    endpoint: "Ollama 本机回环地址",
    language: "转写语言（可选）",
    asr: "Faster Whisper 模型",
    download: "允许本次运行下载 Faster Whisper 模型",
    prepare: "生成私有 AI 建议",
    preparing: "正在本地分析…",
    cancel: "取消本地 AI 分析",
    reviewTitle: "审核有证据的建议",
    accept: "接受",
    reject: "拒绝",
    edit: "编辑",
    confidence: "模型置信度",
    evidence: "来源证据",
    limitations: "限制",
    saveReview: "保存逐项审核",
    saving: "正在保存审核…",
    apply: "把已接受区间应用到故事板",
    applied: "已接受的区间已加入。请继续使用下方普通故事板和私有预览门禁。",
    noRange: "这是文本建议，不会直接改变视频时间轴。",
    required: "请输入 Ollama 中已经存在的准确模型 ID。",
    provider: "内容智能供应商",
    localProvider: "本机 Ollama（不传输远程数据）",
    remoteConsent: "我同意本次把有限字幕和结构区间发送给所选供应商。",
    remoteConsentRequired: "使用 BYOK AI 前，请确认上述远程数据传输。",
  },
} as const;

function initialDecision(suggestion: AISuggestion): DraftDecision {
  const range = suggestion.evidence.source_ranges[0] ?? null;
  return {
    decision: "reject",
    content: suggestion.content,
    start: range?.start_seconds ?? null,
    end: range?.end_seconds ?? null,
  };
}

export function ContentAIReview({
  jobId,
  revision,
  locale,
  busy: parentBusy,
  onApplied,
  api = DEFAULT_API,
}: {
  jobId: string;
  revision: number;
  locale: ContentLocale;
  busy: boolean;
  onApplied(snapshot: ContentJobResponse): Promise<unknown>;
  api?: ContentAIReviewApi;
}): React.JSX.Element {
  const text = COPY[locale];
  const [model, setModel] = useState("");
  const [endpoint, setEndpoint] = useState("http://127.0.0.1:11434");
  const [asrModel, setAsrModel] = useState("small");
  const [language, setLanguage] = useState("");
  const [allowDownload, setAllowDownload] = useState(false);
  const [providerProfiles, setProviderProfiles] = useState<ConnectorProviderProfile[]>([]);
  const [providerProfileId, setProviderProfileId] = useState("");
  const [remoteConsent, setRemoteConsent] = useState(false);
  const [batch, setBatch] = useState<AISuggestionBatch | null>(null);
  const [drafts, setDrafts] = useState<Record<string, DraftDecision>>({});
  const [reviewSaved, setReviewSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const ordered = useMemo(() => batch?.suggestions ?? [], [batch]);

  const prepare = async (): Promise<void> => {
    const remoteProfile = providerProfiles.find((item) => item.profile_id === providerProfileId);
    if (!remoteProfile && !model.trim()) {
      setError(text.required);
      return;
    }
    if (remoteProfile && !remoteConsent) {
      setError(text.remoteConsentRequired);
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const next = await api.prepare(jobId, {
        semantic_model_id: remoteProfile?.model_id ?? model.trim(),
        asr_model_id: asrModel.trim() || "small",
        asr_language: language.trim() || null,
        ollama_endpoint: endpoint.trim(),
        locale,
        device: "auto",
        allow_model_download: allowDownload,
        maximum_suggestions: 24,
        provider_profile_id: remoteProfile?.profile_id ?? null,
        remote_data_consent: remoteProfile ? remoteConsent : false,
      });
      setBatch(next);
      setDrafts(Object.fromEntries(next.suggestions.map((item) => [item.id, initialDecision(item)])));
      setReviewSaved(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Local AI preparation failed.");
    } finally {
      setBusy(false);
    }
  };

  const decisions = (): AIReviewDecision[] => ordered.map((suggestion) => {
    const draft = drafts[suggestion.id] ?? initialDecision(suggestion);
    if (draft.decision !== "edit") {
      return { suggestion_id: suggestion.id, decision: draft.decision };
    }
    const original = initialDecision(suggestion);
    const editedRange = draft.start !== null && draft.end !== null && draft.end > draft.start &&
      (draft.start !== original.start || draft.end !== original.end)
      ? { start_seconds: draft.start, end_seconds: draft.end }
      : null;
    const editedContent = draft.content.trim() !== suggestion.content ? draft.content.trim() : null;
    if (editedContent === null && editedRange === null) {
      return { suggestion_id: suggestion.id, decision: "accept" };
    }
    return {
      suggestion_id: suggestion.id,
      decision: "edit",
      edited_content: editedContent,
      edited_source_range: editedRange,
    };
  });

  const save = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await api.review(jobId, decisions());
      setReviewSaved(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save AI review.");
    } finally {
      setBusy(false);
    }
  };

  const apply = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await onApplied(await api.apply(jobId, revision));
      setMessage(text.applied);
      setReviewSaved(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not apply reviewed suggestions.");
    } finally {
      setBusy(false);
    }
  };

  const disabled = busy || parentBusy;
  return (
    <section className="content-ai-review content-panel" aria-labelledby="content-ai-title">
      <div className="content-ai-heading">
        <div>
          <p className="eyebrow">{text.eyebrow}</p>
          <h2 id="content-ai-title">{text.title}</h2>
          <p>{text.intro}</p>
        </div>
        <span className="content-ai-local">{providerProfileId ? "BYOK" : "LOCAL"}</span>
      </div>
      <p className="content-ai-disclosure">ⓘ {providerProfileId ? text.remoteDisclosure : text.localDisclosure}</p>
      <details className="content-ai-settings">
        <summary>{text.provider}</summary>
        <ProviderSettings locale={locale} onProfiles={setProviderProfiles} />
      </details>
      <details className="content-ai-settings">
        <summary>{text.model}</summary>
        <div className="content-ai-settings-grid">
          <label>{text.provider}<select value={providerProfileId} onChange={(event) => { setProviderProfileId(event.currentTarget.value); setRemoteConsent(false); }}><option value="">{text.localProvider}</option>{providerProfiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.display_name} · {profile.model_id}</option>)}</select></label>
          <label>{text.model}<input value={model} onChange={(event) => setModel(event.currentTarget.value)} placeholder="qwen2.5:7b" /></label>
          <label>{text.endpoint}<input value={endpoint} onChange={(event) => setEndpoint(event.currentTarget.value)} inputMode="url" /></label>
          <label>{text.asr}<input value={asrModel} onChange={(event) => setAsrModel(event.currentTarget.value)} /></label>
          <label>{text.language}<input value={language} onChange={(event) => setLanguage(event.currentTarget.value)} placeholder="zh / en" /></label>
        </div>
        <label className="content-ai-check"><input type="checkbox" checked={allowDownload} onChange={(event) => setAllowDownload(event.currentTarget.checked)} />{text.download}</label>
        {providerProfileId && <label className="content-ai-check"><input type="checkbox" checked={remoteConsent} onChange={(event) => setRemoteConsent(event.currentTarget.checked)} />{text.remoteConsent}</label>}
      </details>
      <div className="content-ai-actions">
        <button type="button" className="secondary-button" disabled={disabled} onClick={() => void prepare()}>{busy && !batch ? text.preparing : text.prepare}</button>
        {busy && <button type="button" className="danger-button" onClick={() => void api.cancel(jobId)}>{text.cancel}</button>}
      </div>
      {error && <p role="alert" className="content-error">{error}</p>}
      {message && <p role="status" className="content-ai-success">✓ {message}</p>}
      {batch && (
        <div className="content-ai-suggestions">
          <div className="content-ai-batch-meta"><h3>{text.reviewTitle}</h3><span>{batch.provider_id} / {batch.model_id}</span></div>
          {ordered.map((suggestion) => {
            const draft = drafts[suggestion.id] ?? initialDecision(suggestion);
            return (
              <article className="content-ai-suggestion" key={suggestion.id}>
                <header><span>{suggestion.kind}</span>{suggestion.confidence !== null && <small>{text.confidence}: {Math.round(suggestion.confidence * 100)}%</small>}</header>
                <textarea aria-label={`${suggestion.kind} content`} value={draft.content} disabled={draft.decision !== "edit"} onChange={(event) => setDrafts((current) => ({ ...current, [suggestion.id]: { ...draft, content: event.currentTarget.value } }))} />
                <p>{suggestion.rationale}</p>
                {draft.start !== null && draft.end !== null ? (
                  <fieldset className="content-ai-range"><legend>{text.evidence}</legend><label>Start<input type="number" min="0" step="0.1" disabled={draft.decision !== "edit"} value={draft.start} onChange={(event) => setDrafts((current) => ({ ...current, [suggestion.id]: { ...draft, start: Number(event.currentTarget.value) } }))} /></label><label>End<input type="number" min="0" step="0.1" disabled={draft.decision !== "edit"} value={draft.end} onChange={(event) => setDrafts((current) => ({ ...current, [suggestion.id]: { ...draft, end: Number(event.currentTarget.value) } }))} /></label></fieldset>
                ) : <small>{text.noRange}</small>}
                {suggestion.limitations.length > 0 && <details><summary>{text.limitations}</summary><ul>{suggestion.limitations.map((item) => <li key={item}>{item}</li>)}</ul></details>}
                <div className="content-ai-decisions" role="group" aria-label={`${suggestion.kind} decision`}>
                  {(["accept", "reject", "edit"] as const).map((choice) => <button type="button" key={choice} className={draft.decision === choice ? "active" : ""} onClick={() => { setDrafts((current) => ({ ...current, [suggestion.id]: { ...draft, decision: choice } })); setReviewSaved(false); }}>{text[choice]}</button>)}
                </div>
              </article>
            );
          })}
          <div className="content-ai-actions">
            <button type="button" className="secondary-button" disabled={disabled} onClick={() => void save()}>{busy ? text.saving : text.saveReview}</button>
            <button type="button" className="primary-button" disabled={disabled || !reviewSaved} onClick={() => void apply()}>{text.apply}</button>
          </div>
        </div>
      )}
    </section>
  );
}
