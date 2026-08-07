import { type FormEvent, useEffect, useState } from "react";

import {
  deleteConnectorProvider,
  listConnectorProviders,
  putConnectorProvider,
} from "../api";
import type {
  ConnectorProviderProfile,
  ConnectorProviderProfileInput,
} from "../types";
import type { ContentLocale } from "../contentI18n";

const PRESETS = {
  custom: { label: "Custom compatible endpoint", endpoint: "", json: false },
  openai: { label: "OpenAI compatible API", endpoint: "https://api.openai.com/v1", json: true },
  deepseek: { label: "DeepSeek compatible API", endpoint: "https://api.deepseek.com", json: true },
  qwen: { label: "Qwen DashScope compatible API", endpoint: "https://dashscope.aliyuncs.com/compatible-mode/v1", json: true },
  glm: { label: "Zhipu GLM compatible API", endpoint: "https://open.bigmodel.cn/api/paas/v4", json: true },
  kimi: { label: "Kimi / Moonshot compatible API", endpoint: "https://api.moonshot.cn/v1", json: true },
} as const;

const COPY = {
  en: {
    title: "BYOK provider vault",
    description: "Keys are sent only to this loopback connector, kept in process memory, and cleared when VideoScope exits. Provider pricing and compatibility remain your responsibility.",
    preset: "Compatibility preset",
    profile: "Profile ID",
    name: "Display name",
    endpoint: "HTTPS API base URL",
    model: "Exact model ID",
    key: "API key (never stored on disk)",
    json: "Request JSON object mode when supported",
    save: "Keep in memory for this session",
    saved: "Provider is ready in connector memory.",
    remove: "Remove",
    none: "No in-memory provider profile yet.",
    failure: "The provider profile could not be updated.",
  },
  "zh-CN": {
    title: "BYOK 供应商密钥保险库",
    description: "密钥只会发送到当前回环连接器，仅保存在进程内存，并在 VideoScope 退出时清除。供应商价格和兼容性由您自行确认。",
    preset: "兼容协议预设",
    profile: "配置 ID",
    name: "显示名称",
    endpoint: "HTTPS API 基础地址",
    model: "准确模型 ID",
    key: "API Key（绝不写入磁盘）",
    json: "供应商支持时请求 JSON 对象模式",
    save: "仅在本次会话保存在内存",
    saved: "供应商已在连接器内存中就绪。",
    remove: "移除",
    none: "当前尚无内存供应商配置。",
    failure: "无法更新供应商配置。",
  },
} as const;

export function ProviderSettings({
  locale,
  onProfiles,
}: {
  locale: ContentLocale;
  onProfiles(profiles: ConnectorProviderProfile[]): void;
}): React.JSX.Element {
  const text = COPY[locale];
  const [profiles, setProfiles] = useState<ConnectorProviderProfile[]>([]);
  const [preset, setPreset] = useState<keyof typeof PRESETS>("custom");
  const [profileId, setProfileId] = useState("my-ai");
  const [displayName, setDisplayName] = useState("My AI provider");
  const [endpoint, setEndpoint] = useState("");
  const [modelId, setModelId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [requestJson, setRequestJson] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async (): Promise<void> => {
    const next = await listConnectorProviders();
    setProfiles(next);
    onProfiles(next);
  };

  useEffect(() => {
    void refresh().catch(() => setError(text.failure));
  }, []);

  const selectPreset = (value: keyof typeof PRESETS): void => {
    setPreset(value);
    setEndpoint(PRESETS[value].endpoint);
    setRequestJson(PRESETS[value].json);
  };

  const save = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    setError(null);
    setMessage(null);
    const payload: ConnectorProviderProfileInput = {
      profile_id: profileId.trim(),
      display_name: displayName.trim(),
      provider_id: preset === "custom" ? "custom-openai" : preset,
      protocol: "openai_compatible",
      api_base_url: endpoint.trim(),
      model_id: modelId.trim(),
      api_key: apiKey,
      capabilities: ["structured_text"],
      request_json_object: requestJson,
    };
    try {
      await putConnectorProvider(payload);
      setApiKey("");
      await refresh();
      setMessage(text.saved);
    } catch {
      setError(text.failure);
    }
  };

  return (
    <section className="provider-settings" aria-labelledby="provider-settings-title">
      <div><h3 id="provider-settings-title">{text.title}</h3><p>{text.description}</p></div>
      <form onSubmit={(event) => void save(event)}>
        <div className="content-ai-settings-grid">
          <label>{text.preset}<select value={preset} onChange={(event) => selectPreset(event.currentTarget.value as keyof typeof PRESETS)}>{Object.entries(PRESETS).map(([id, item]) => <option key={id} value={id}>{item.label}</option>)}</select></label>
          <label>{text.profile}<input pattern="[a-z][a-z0-9_-]{0,63}" required value={profileId} onChange={(event) => setProfileId(event.currentTarget.value)} /></label>
          <label>{text.name}<input required value={displayName} onChange={(event) => setDisplayName(event.currentTarget.value)} /></label>
          <label>{text.endpoint}<input inputMode="url" required value={endpoint} onChange={(event) => setEndpoint(event.currentTarget.value)} placeholder="https://provider.example/v1" /></label>
          <label>{text.model}<input required value={modelId} onChange={(event) => setModelId(event.currentTarget.value)} /></label>
          <label>{text.key}<input autoComplete="off" required type="password" value={apiKey} onChange={(event) => setApiKey(event.currentTarget.value)} /></label>
        </div>
        <label className="content-ai-check"><input checked={requestJson} onChange={(event) => setRequestJson(event.currentTarget.checked)} type="checkbox" />{text.json}</label>
        <button className="secondary-button" type="submit">{text.save}</button>
      </form>
      {profiles.length ? <ul>{profiles.map((profile) => <li key={profile.profile_id}><span><strong>{profile.display_name}</strong><small>{profile.model_id} · {profile.credential_state}</small></span><button className="danger-button" onClick={() => void deleteConnectorProvider(profile.profile_id).then(refresh).catch(() => setError(text.failure))} type="button">{text.remove}</button></li>)}</ul> : <p>{text.none}</p>}
      {message && <p className="content-ai-success" role="status">✓ {message}</p>}
      {error && <p className="content-error" role="alert">{error}</p>}
    </section>
  );
}
