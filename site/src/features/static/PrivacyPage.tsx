import { useEffect, useState } from "react";
import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import {
  createReportStore,
  type ReportStore,
  type StorageUsage,
} from "../../services/report-store/report-store";
import {
  clearAllLocalShareRecords,
  getLocalShareRecordUsage,
  type LocalShareRecordUsage,
} from "../../services/share";
import { clearSessionVideo } from "../upload/session-video-store";
import { getStaticCopy } from "./static-copy";
import "./static.css";

const defaultStore = createReportStore().then(({ store }) => store);

interface PrivacyPageProps {
  reportStore?: ReportStore;
  clearSession?(): void;
  shareStorage?: Storage;
}

type UsageState =
  | { status: "loading" }
  | { status: "ready"; usage: StorageUsage }
  | { status: "error" };

type ShareUsageState =
  | { status: "loading" }
  | { status: "ready"; usage: LocalShareRecordUsage }
  | { status: "error" };

export function PrivacyPage({
  reportStore,
  clearSession = clearSessionVideo,
  shareStorage = window.localStorage,
}: PrivacyPageProps = {}) {
  const { locale } = useI18n();
  const copy = getStaticCopy(locale).privacy;
  const [store, setStore] = useState<ReportStore | null>(reportStore ?? null);
  const [usageState, setUsageState] = useState<UsageState>({
    status: "loading",
  });
  const [shareUsageState, setShareUsageState] = useState<ShareUsageState>({
    status: "loading",
  });
  const [confirming, setConfirming] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [result, setResult] = useState<"success" | "partial">();
  const sections = [
    [copy.localTitle, copy.localBody],
    [copy.storageTitle, copy.storageBody],
    [copy.urlTitle, copy.urlBody],
    [copy.accountTitle, copy.accountBody],
    [copy.sharingTitle, copy.sharingBody],
  ] as const;

  useEffect(() => {
    let active = true;
    if (reportStore) {
      setStore(reportStore);
      return () => {
        active = false;
      };
    }
    void defaultStore.then((resolved) => {
      if (active) setStore(resolved);
    });
    return () => {
      active = false;
    };
  }, [reportStore]);

  useEffect(() => {
    if (!store) return;
    let active = true;
    setUsageState({ status: "loading" });
    void store
      .usage()
      .then((usage) => {
        if (active) setUsageState({ status: "ready", usage });
      })
      .catch(() => {
        if (active) setUsageState({ status: "error" });
      });
    return () => {
      active = false;
    };
  }, [store]);

  useEffect(() => {
    let active = true;
    setShareUsageState({ status: "loading" });
    void getLocalShareRecordUsage(shareStorage)
      .then((usage) => {
        if (active) setShareUsageState({ status: "ready", usage });
      })
      .catch(() => {
        if (active) setShareUsageState({ status: "error" });
      });
    return () => {
      active = false;
    };
  }, [shareStorage]);

  const clearLocalData = async () => {
    if (!store || clearing) return;
    setClearing(true);
    setResult(undefined);
    const [reportResult, sessionResult, shareResult] =
      await Promise.allSettled([
        store.clear(),
        Promise.resolve().then(() => clearSession()),
        clearAllLocalShareRecords(shareStorage),
      ]);
    if (reportResult.status === "fulfilled") {
      setUsageState({
        status: "ready",
        usage: { bytes_used: 0, report_count: 0, thumbnail_count: 0 },
      });
    }
    if (shareResult.status === "fulfilled") {
      setShareUsageState({
        status: "ready",
        usage: { bytes_used: 0, key_count: 0, record_count: 0 },
      });
    } else {
      try {
        setShareUsageState({
          status: "ready",
          usage: await getLocalShareRecordUsage(shareStorage),
        });
      } catch {
        setShareUsageState({ status: "error" });
      }
    }
    setClearing(false);
    setConfirming(false);
    setResult(
      [reportResult, sessionResult, shareResult].every(
        (operation) => operation.status === "fulfilled",
      )
        ? "success"
        : "partial",
    );
  };

  const reportCount =
    usageState.status === "ready" ? usageState.usage.report_count : 0;
  const thumbnailCount =
    usageState.status === "ready" ? usageState.usage.thumbnail_count : 0;
  const shareRecordCount =
    shareUsageState.status === "ready"
      ? shareUsageState.usage.record_count
      : 0;

  return (
    <article className="static-page" aria-labelledby="privacy-title">
      <header className="static-page__header">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1 id="privacy-title">{copy.title}</h1>
        <p>{copy.introduction}</p>
      </header>
      <div className="static-page__sections">
        {sections.map(([title, body], index) => (
          <section key={title}>
            <span aria-hidden="true" className="static-page__index numeric">
              {String(index + 1).padStart(2, "0")}
            </span>
            <div>
              <h2>{title}</h2>
              <p>{body}</p>
            </div>
          </section>
        ))}
      </div>
      <section className="static-page__action">
        <div>
          <h2>{copy.controlsTitle}</h2>
          <p>{copy.controlsBody}</p>
          {usageState.status === "loading" ? (
            <p role="status">{copy.usageLoading}</p>
          ) : usageState.status === "error" ? (
            <p role="alert">{copy.usageUnavailable}</p>
          ) : (
            <div className="privacy-usage" aria-live="polite">
              <strong>
                {reportCount}{" "}
                {reportCount === 1
                  ? copy.reportSingular
                  : copy.reportPlural}
              </strong>
              <span>
                {thumbnailCount}{" "}
                {thumbnailCount === 1
                  ? copy.thumbnailSingular
                  : copy.thumbnailPlural}
              </span>
              <span className="numeric">
                {usageState.usage.bytes_used.toLocaleString(locale)} bytes{" "}
                {copy.bytesStored}
              </span>
              {shareUsageState.status === "loading" ? (
                <span>{copy.usageLoading}</span>
              ) : shareUsageState.status === "error" ? (
                <span>{copy.usageUnavailable}</span>
              ) : (
                <>
                  <span>
                    {shareRecordCount}{" "}
                    {shareRecordCount === 1
                      ? copy.shareLinkSingular
                      : copy.shareLinkPlural}
                  </span>
                  <span className="numeric">
                    {shareUsageState.usage.bytes_used.toLocaleString(locale)}{" "}
                    bytes {copy.shareBytesStored}
                  </span>
                </>
              )}
            </div>
          )}
        </div>
        <div className="privacy-actions">
          <button
            className="button button--primary"
            disabled={!store || clearing}
            onClick={() => setConfirming(true)}
            type="button"
          >
            {copy.deleteAction}
          </button>
          <Link className="text-link" to="/workspace">
            {copy.controlsAction}
          </Link>
        </div>
      </section>
      {confirming ? (
        <section
          aria-labelledby="privacy-delete-title"
          className="privacy-confirm"
        >
          <h2 id="privacy-delete-title">{copy.deleteConfirmTitle}</h2>
          <p>{copy.deleteConfirmBody}</p>
          <div>
            <button
              className="button button--quiet"
              disabled={clearing}
              onClick={() => setConfirming(false)}
              type="button"
            >
              {copy.deleteCancel}
            </button>
            <button
              className="button button--primary"
              disabled={clearing}
              onClick={() => void clearLocalData()}
              type="button"
            >
              {copy.deleteConfirmAction}
            </button>
          </div>
        </section>
      ) : null}
      {result ? (
        <p
          aria-label={
            result === "success" ? copy.deleteSuccess : copy.deletePartial
          }
          role={result === "success" ? "status" : "alert"}
        >
          {result === "success" ? copy.deleteSuccess : copy.deletePartial}
        </p>
      ) : null}
    </article>
  );
}
