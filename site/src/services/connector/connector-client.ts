const CONNECTOR_ORIGIN = "http://127.0.0.1:8765";
const CONNECTOR_API = `${CONNECTOR_ORIGIN}/api`;
const SESSION_KEY = "videoscope.connector-session.v1";

export type ConnectorStatus = {
  status: string;
  service: string;
  version: string;
  pairing_required: boolean;
  credentials_persisted: boolean;
  modes: string[];
  ffmpeg_available?: boolean;
  ffprobe_available?: boolean;
};

export type ConnectorProvider = {
  profile_id: string;
  display_name: string;
  provider_id: string;
  protocol: "openai_compatible" | "ollama";
  api_base_url: string;
  model_id: string;
  capabilities: string[];
  request_json_object: boolean;
  credential_state: "memory_only";
};

type StoredSession = {
  token: string;
  expiresAt: string;
};

function storedSession(): StoredSession | null {
  const raw = sessionStorage.getItem(SESSION_KEY);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<StoredSession>;
    if (
      typeof value.token !== "string" ||
      typeof value.expiresAt !== "string" ||
      Date.parse(value.expiresAt) <= Date.now()
    ) {
      throw new Error("expired");
    }
    return value as StoredSession;
  } catch {
    sessionStorage.removeItem(SESSION_KEY);
    return null;
  }
}

async function responseJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new Error(body?.detail ?? `Connector request failed (${response.status}).`);
  }
  return (await response.json()) as T;
}

export class ConnectorClient {
  readonly origin = CONNECTOR_ORIGIN;

  async status(signal?: AbortSignal): Promise<ConnectorStatus> {
    const response = await fetch(`${CONNECTOR_API}/connector/status`, {
      cache: "no-store",
      mode: "cors",
      signal,
    });
    return responseJson<ConnectorStatus>(response);
  }

  isPaired(): boolean {
    return storedSession() !== null;
  }

  async pair(pairingCode: string): Promise<void> {
    const response = await fetch(`${CONNECTOR_API}/connector/sessions`, {
      method: "POST",
      mode: "cors",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ pairing_code: pairingCode }),
    });
    const session = await responseJson<{
      session_token: string;
      expires_at: string;
    }>(response);
    sessionStorage.setItem(
      SESSION_KEY,
      JSON.stringify({
        token: session.session_token,
        expiresAt: session.expires_at,
      } satisfies StoredSession),
    );
  }

  async providers(): Promise<ConnectorProvider[]> {
    const session = storedSession();
    if (!session) throw new Error("Connector pairing is required.");
    const response = await fetch(`${CONNECTOR_API}/connector/providers`, {
      cache: "no-store",
      mode: "cors",
      headers: { "X-VideoScope-Session": session.token },
    });
    if (response.status === 401) sessionStorage.removeItem(SESSION_KEY);
    return responseJson<ConnectorProvider[]>(response);
  }

  async disconnect(): Promise<void> {
    const session = storedSession();
    sessionStorage.removeItem(SESSION_KEY);
    if (!session) return;
    await fetch(`${CONNECTOR_API}/connector/sessions/current`, {
      method: "DELETE",
      mode: "cors",
      headers: { "X-VideoScope-Session": session.token },
    }).catch(() => undefined);
  }

  workbenchUrl(mode: "analyze" | "publish" | "privacy" | "rescue" | "content") {
    return `${CONNECTOR_ORIGIN}/?mode=${encodeURIComponent(mode)}`;
  }
}

export const connectorClient = new ConnectorClient();
