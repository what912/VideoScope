import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";

import { useAuth } from "../auth/AuthProvider";
import { useOnlineStatus } from "../../hooks/useOnlineStatus";
import { useI18n } from "../../i18n/I18nProvider";
import {
  createShareClient,
  createShareRecordStore,
  evidenceSelectionId,
  isShareEnvironmentEnabled,
  isUnavailableShareClient,
  readShareEnvironment,
  sanitizeReportForShare,
  type ShareRecord,
  type ShareRecordStore,
  type ShareClient,
} from "../../services/share";
import type { BrowserReport } from "../../types/report";
import type { Locale } from "../../i18n/types";

interface ShareDialogCopy {
  title: string;
  description: string;
  notConfigured: string;
  notConfiguredDetail: string;
  offline: string;
  offlineDetail: string;
  reportTitle: string;
  includePrompt: string;
  evidenceHeading: string;
  evidenceAt: string;
  outgoingHeading: string;
  outgoingFields: string[];
  expiry: string;
  noExpiry: string;
  sevenDays: string;
  thirtyDays: string;
  finalConsent: string;
  create: string;
  creating: string;
  close: string;
  failed: string;
  created: string;
  copyLink: string;
  copied: string;
  copyFailed: string;
  revoke: string;
  revoking: string;
  revoked: string;
  savedLinks: string;
  savedLinksBoundary: string;
  revokeNamed: string;
  indexSaveFailed: string;
  indexRemoveFailed: string;
}

const copyByLocale: Record<Locale, ShareDialogCopy> = {
  en: {
    title: "Share a sanitized report",
    description:
      "Sharing is optional. Review exactly what will leave this device before creating a public link.",
    notConfigured: "Not configured",
    notConfiguredDetail:
      "Sanitized sharing requires the feature flag, configured authentication, and a signed-in session. Local reports remain available.",
    offline: "Sharing is offline",
    offlineDetail:
      "Reconnect before creating or revoking public links. Local reports remain available.",
    reportTitle: "Public report title (optional)",
    includePrompt: "Include prompt",
    evidenceHeading: "Evidence records to include",
    evidenceAt: "Evidence at",
    outgoingHeading: "Data leaving this device",
    outgoingFields: [
      "Report schema and tool version",
      "Report creation time",
      "Public title, when provided",
      "Prompt, only when separately selected",
      "Video dimensions and duration",
      "Video MIME type, file size, frame rate, and audio flag",
      "Detector results and limitations",
      "Detector configuration, execution timing, and sanitized errors",
      "Detector-local metrics, summary counts, and warnings",
      "Sanitized runtime and selected evidence metadata",
      "Selected evidence timestamps and descriptions",
      "No original video or evidence image files",
    ],
    expiry: "Link expiry",
    noExpiry: "No automatic expiry",
    sevenDays: "7 days",
    thirtyDays: "30 days",
    finalConsent:
      "I understand that the listed data will leave this device and be readable through the public link.",
    create: "Create share link",
    creating: "Creating link…",
    close: "Close",
    failed: "The share link could not be created. No video was uploaded.",
    created: "Sanitized share link created",
    copyLink: "Copy link",
    copied: "Copied",
    copyFailed: "Copy failed. Select the link manually.",
    revoke: "Revoke link",
    revoking: "Revoking…",
    revoked: "Share link revoked",
    savedLinks: "Saved share links",
    savedLinksBoundary:
      "Links saved in this browser for this signed-in account. This list is not synchronized across devices.",
    revokeNamed: "Revoke",
    indexSaveFailed:
      "The public link works, but its local revoke shortcut could not be saved. Copy or revoke it before closing.",
    indexRemoveFailed:
      "The public link was revoked, but its stale local shortcut could not be removed from this browser.",
  },
  "zh-CN": {
    title: "分享脱敏报告",
    description:
      "分享为可选操作。创建公开链接前，请逐项确认哪些数据将离开当前设备。",
    notConfigured: "未配置",
    notConfiguredDetail:
      "脱敏分享必须同时启用功能开关、配置身份验证并登录。未配置时，本地报告仍可正常使用。",
    offline: "分享服务当前离线",
    offlineDetail:
      "重新联网后可创建或撤销公开链接；本地报告仍然可用。",
    reportTitle: "公开报告标题（可选）",
    includePrompt: "包含提示词",
    evidenceHeading: "选择要包含的证据记录",
    evidenceAt: "证据时间",
    outgoingHeading: "将离开当前设备的数据",
    outgoingFields: [
      "报告模式与工具版本",
      "报告创建时间",
      "用户填写的公开标题（如有）",
      "仅在单独勾选后包含提示词",
      "视频尺寸与时长",
      "视频 MIME 类型、文件大小、帧率和音频标记",
      "检测器结果与局限说明",
      "检测器配置、执行耗时和脱敏错误",
      "检测器内指标、汇总计数和警告",
      "脱敏运行时和所选证据元数据",
      "所选证据的时间戳与描述",
      "不包含原始视频或证据图片文件",
    ],
    expiry: "链接有效期",
    noExpiry: "不自动过期",
    sevenDays: "7 天",
    thirtyDays: "30 天",
    finalConsent:
      "我理解以上列出的数据将离开当前设备，并可通过公开链接读取。",
    create: "创建分享链接",
    creating: "正在创建…",
    close: "关闭",
    failed: "无法创建分享链接。原始视频未被上传。",
    created: "脱敏分享链接已创建",
    copyLink: "复制链接",
    copied: "已复制",
    copyFailed: "复制失败，请手动选择链接。",
    revoke: "撤销链接",
    revoking: "正在撤销…",
    revoked: "分享链接已撤销",
    savedLinks: "已保存的分享链接",
    savedLinksBoundary:
      "这些链接仅保存在当前浏览器并按登录账户隔离，不会在不同设备之间同步。",
    revokeNamed: "撤销",
    indexSaveFailed:
      "公开链接已经生效，但本地撤销入口未能保存。关闭前请复制或撤销此链接。",
    indexRemoveFailed:
      "公开链接已撤销，但当前浏览器中的旧撤销入口未能删除。",
  },
};

const defaultEnvironment = readShareEnvironment();
const defaultShareClient = createShareClient(defaultEnvironment);
const defaultShareEnabled = isShareEnvironmentEnabled(defaultEnvironment);
const defaultShareRecordStore = createShareRecordStore();

function expiryFromChoice(choice: string) {
  const days = Number(choice);
  if (!Number.isFinite(days) || days <= 0) return undefined;
  return new Date(Date.now() + days * 86_400_000).toISOString();
}

function shareUrl(publicId: string) {
  const basePath = import.meta.env.BASE_URL.endsWith("/")
    ? import.meta.env.BASE_URL
    : `${import.meta.env.BASE_URL}/`;
  return new URL(
    `${basePath}report/${encodeURIComponent(publicId)}?shared=1`,
    window.location.origin,
  ).toString();
}

export interface ShareDialogProps {
  onClose(): void;
  report: BrowserReport;
  shareClient?: ShareClient;
  shareEnabled?: boolean;
  shareRecordStore?: ShareRecordStore;
}

export function ShareDialog({
  onClose,
  report,
  shareClient = defaultShareClient,
  shareEnabled = defaultShareEnabled,
  shareRecordStore = defaultShareRecordStore,
}: ShareDialogProps) {
  const { locale } = useI18n();
  const auth = useAuth();
  const online = useOnlineStatus();
  const copy = copyByLocale[locale];
  const [reportTitle, setReportTitle] = useState("");
  const [includePrompt, setIncludePrompt] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<Set<string>>(
    () => new Set(),
  );
  const [expiry, setExpiry] = useState("");
  const [consent, setConsent] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState(false);
  const [link, setLink] = useState<string>();
  const [publicId, setPublicId] = useState<string>();
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const [savedRecords, setSavedRecords] = useState<ShareRecord[]>([]);
  const [revokingSavedId, setRevokingSavedId] = useState<string>();
  const [indexSaveFailed, setIndexSaveFailed] = useState(false);
  const [indexRemoveFailed, setIndexRemoveFailed] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const copyRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    closeRef.current?.focus();
    return () => {
      if (
        previouslyFocused instanceof HTMLElement &&
        previouslyFocused.isConnected
      ) {
        previouslyFocused.focus();
      }
    };
  }, []);

  const serviceConfigured =
    shareEnabled &&
    !isUnavailableShareClient(shareClient) &&
    auth.status === "authenticated" &&
    Boolean(auth.session);
  const configured = serviceConfigured && online;

  useEffect(() => {
    let active = true;
    if (!configured || !auth.session) {
      setSavedRecords([]);
      return () => {
        active = false;
      };
    }
    void shareRecordStore
      .list(auth.session.user.id, report.id)
      .then((records) => {
        if (active) setSavedRecords(records);
      })
      .catch(() => {
        if (active) setSavedRecords([]);
      });
    return () => {
      active = false;
    };
  }, [auth.session, configured, report.id, shareRecordStore]);

  useEffect(() => {
    if (link && !revoked) {
      copyRef.current?.focus();
    } else if (revoked) {
      closeRef.current?.focus();
    }
  }, [link, revoked]);

  const evidenceOptions = useMemo(
    () =>
      report.findings.flatMap((finding) =>
        finding.evidence.map((evidence, index) => ({
          description: evidence.description,
          id: evidenceSelectionId(finding.id, index),
          timestamp: evidence.timestamp_seconds,
        })),
      ),
    [report],
  );

  async function createLink() {
    if (!configured || !consent || !auth.session || working) return;
    setWorking(true);
    setError(false);
    setIndexSaveFailed(false);
    setIndexRemoveFailed(false);
    try {
      const sanitized = sanitizeReportForShare(report, {
        includePrompt,
        reportTitle,
        selectedEvidence,
      });
      const result = await shareClient.createShare({
        expiresAt: expiryFromChoice(expiry),
        ownerId: auth.session.user.id,
        report: sanitized,
      });
      const record: ShareRecord = {
        publicId: result.publicId,
        createdAt: result.createdAt,
        ...(result.expiresAt ? { expiresAt: result.expiresAt } : {}),
        reportId: report.id,
        title: reportTitle.trim() || report.title,
      };
      try {
        await shareRecordStore.put(auth.session.user.id, record);
        setSavedRecords((current) => [
          record,
          ...current.filter(
            (candidate) => candidate.publicId !== record.publicId,
          ),
        ]);
      } catch {
        setIndexSaveFailed(true);
      }
      setPublicId(result.publicId);
      setLink(shareUrl(result.publicId));
    } catch {
      setError(true);
    } finally {
      setWorking(false);
    }
  }

  async function copyShareLink() {
    if (!link) return;
    setCopyFailed(false);
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
      setCopyFailed(true);
    }
  }

  async function revokeShareLink() {
    if (!publicId || revoking) return;
    setRevoking(true);
    setError(false);
    setIndexRemoveFailed(false);
    try {
      await shareClient.revokeShare(publicId);
      if (auth.session) {
        try {
          await shareRecordStore.remove(auth.session.user.id, publicId);
        } catch {
          setIndexRemoveFailed(true);
        }
      }
      setSavedRecords((current) =>
        current.filter((record) => record.publicId !== publicId),
      );
      setRevoked(true);
    } catch {
      setError(true);
    } finally {
      setRevoking(false);
    }
  }

  async function revokeSavedLink(record: ShareRecord) {
    if (!auth.session || revokingSavedId) return;
    setRevokingSavedId(record.publicId);
    setError(false);
    setIndexRemoveFailed(false);
    try {
      await shareClient.revokeShare(record.publicId);
      try {
        await shareRecordStore.remove(auth.session.user.id, record.publicId);
      } catch {
        setIndexRemoveFailed(true);
      }
      setSavedRecords((current) =>
        current.filter(
          (candidate) => candidate.publicId !== record.publicId,
        ),
      );
      closeRef.current?.focus();
    } catch {
      setError(true);
    } finally {
      setRevokingSavedId(undefined);
    }
  }

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    ).filter((element) => {
      const style = window.getComputedStyle(element);
      return (
        !element.closest('[hidden], [inert], [aria-hidden="true"]') &&
        style.display !== "none" &&
        style.visibility !== "hidden"
      );
    });
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleBackdropMouseDown(event: MouseEvent<HTMLDivElement>) {
    if (event.target === event.currentTarget) onClose();
  }

  function toggleEvidence(id: string) {
    setSelectedEvidence((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div
      aria-labelledby="share-dialog-title"
      aria-modal="true"
      className="share-dialog"
      onKeyDown={handleDialogKeyDown}
      onMouseDown={handleBackdropMouseDown}
      ref={dialogRef}
      role="dialog"
    >
      <div className="share-dialog__panel">
        <header>
          <div>
            <p className="eyebrow">VideoScope · local-first</p>
            <h2 id="share-dialog-title">{copy.title}</h2>
          </div>
          <button
            aria-label={copy.close}
            className="button button--quiet"
            onClick={onClose}
            ref={closeRef}
            type="button"
          >
            ×
          </button>
        </header>

        {!configured ? (
          <section
            aria-label={serviceConfigured ? copy.offline : copy.notConfigured}
            className="share-dialog__unavailable"
            role="status"
          >
            <strong>
              {serviceConfigured ? copy.offline : copy.notConfigured}
            </strong>
            <p>
              {serviceConfigured
                ? copy.offlineDetail
                : copy.notConfiguredDetail}
            </p>
          </section>
        ) : link ? (
          <section className="share-dialog__created" role="status">
            <strong>{revoked ? copy.revoked : copy.created}</strong>
            <output>{link}</output>
            {!revoked ? (
              <div className="share-dialog__created-actions">
                <button
                  className="button button--primary"
                  onClick={() => void copyShareLink()}
                  ref={copyRef}
                  type="button"
                >
                  {copied ? copy.copied : copy.copyLink}
                </button>
                <button
                  className="button button--quiet"
                  disabled={revoking}
                  onClick={() => void revokeShareLink()}
                  type="button"
                >
                  {revoking ? copy.revoking : copy.revoke}
                </button>
              </div>
            ) : null}
            {copyFailed ? <p role="alert">{copy.copyFailed}</p> : null}
            {indexSaveFailed ? (
              <p role="alert">{copy.indexSaveFailed}</p>
            ) : null}
            {indexRemoveFailed ? (
              <p role="alert">{copy.indexRemoveFailed}</p>
            ) : null}
            {error ? <p role="alert">{copy.failed}</p> : null}
          </section>
        ) : (
          <>
            <p>{copy.description}</p>
            {savedRecords.length > 0 ? (
              <section
                aria-labelledby="saved-share-links-title"
                className="share-dialog__saved"
              >
                <h3 id="saved-share-links-title">{copy.savedLinks}</h3>
                <p>{copy.savedLinksBoundary}</p>
                <ul>
                  {savedRecords.map((record) => (
                    <li key={record.publicId}>
                      <div>
                        <strong>{record.title}</strong>
                        <output>{shareUrl(record.publicId)}</output>
                      </div>
                      <button
                        aria-label={`${copy.revokeNamed} ${record.title}`}
                        className="button button--quiet"
                        disabled={revokingSavedId === record.publicId}
                        onClick={() => void revokeSavedLink(record)}
                        type="button"
                      >
                        {revokingSavedId === record.publicId
                          ? copy.revoking
                          : copy.revokeNamed}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ) : null}
            {indexRemoveFailed ? (
              <p role="alert">{copy.indexRemoveFailed}</p>
            ) : null}
            <label>
              <span>{copy.reportTitle}</span>
              <input
                onChange={(event) => setReportTitle(event.target.value)}
                type="text"
                value={reportTitle}
              />
            </label>
            {report.prompt ? (
              <label className="share-dialog__check">
                <input
                  checked={includePrompt}
                  onChange={(event) => setIncludePrompt(event.target.checked)}
                  type="checkbox"
                />
                <span>{copy.includePrompt}</span>
              </label>
            ) : null}
            <label>
              <span>{copy.expiry}</span>
              <select
                onChange={(event) => setExpiry(event.target.value)}
                value={expiry}
              >
                <option value="">{copy.noExpiry}</option>
                <option value="7">{copy.sevenDays}</option>
                <option value="30">{copy.thirtyDays}</option>
              </select>
            </label>

            <fieldset>
              <legend>{copy.evidenceHeading}</legend>
              <div className="share-dialog__evidence">
                {evidenceOptions.map((evidence) => (
                  <label className="share-dialog__check" key={evidence.id}>
                    <input
                      checked={selectedEvidence.has(evidence.id)}
                      onChange={() => toggleEvidence(evidence.id)}
                      type="checkbox"
                    />
                    <span>
                      {copy.evidenceAt} {evidence.timestamp.toFixed(3)} s ·{" "}
                      {evidence.description}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <section aria-labelledby="outgoing-fields-title">
              <h3 id="outgoing-fields-title">{copy.outgoingHeading}</h3>
              <ul>
                {copy.outgoingFields.map((field) => (
                  <li key={field}>{field}</li>
                ))}
              </ul>
            </section>
            <label className="share-dialog__check share-dialog__consent">
              <input
                checked={consent}
                onChange={(event) => setConsent(event.target.checked)}
                type="checkbox"
              />
              <span>{copy.finalConsent}</span>
            </label>
            {error ? <p role="alert">{copy.failed}</p> : null}
            <button
              className="button button--primary"
              disabled={!consent || working}
              onClick={() => void createLink()}
              type="button"
            >
              {working ? copy.creating : copy.create}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
