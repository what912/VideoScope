import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiError,
  confirmContentJob,
  contentArtifactUrl,
  contentPreviewUrl,
  createContentJob,
  createContentPreviews,
  deleteContentJob,
  getContentJob,
  getContentMap,
  getContentPlan,
  getContentPreviews,
  reviseContentStoryboard,
  subscribeToContentEvents,
} from "../api";
import { contentStatusText, contentText, type ContentLocale } from "../contentI18n";
import { CONTENT_TERMINAL_STATUSES, useContentLifecycle, type ContentLifecycleApi } from "../hooks/useContentLifecycle";
import type {
  ContentCreateOptions,
  ContentGoal,
  ContentJobResponse,
  ContentPlan,
  ContentRangeInput,
  ContentRevisionPayload,
} from "../types";
import { ContentGoalSelector } from "./ContentGoalSelector";
import { ContentAIReview } from "./ContentAIReview";
import { ContentJoinPreview } from "./ContentJoinPreview";
import { ContentMapTimeline } from "./ContentMapTimeline";
import { ContentPlanReview } from "./ContentPlanReview";
import { ContentResult } from "./ContentResult";
import { ContentStoryboard } from "./ContentStoryboard";

export interface ContentApi extends ContentLifecycleApi {
  createJob(video: File, options: ContentCreateOptions): Promise<ContentJobResponse>;
  revise(jobId: string, payload: ContentRevisionPayload): Promise<ContentJobResponse>;
  createPreviews(jobId: string): Promise<ContentJobResponse>;
  confirm(jobId: string, plan: ContentPlan, revision: number): Promise<ContentJobResponse>;
  deleteJob(jobId: string): Promise<ContentJobResponse | null>;
  artifactUrl(jobId: string, path: string): string;
  previewUrl(jobId: string, path: string): string;
}

const DEFAULT_API: ContentApi = {
  createJob: createContentJob,
  getJob: getContentJob,
  getMap: getContentMap,
  getPlan: getContentPlan,
  getPreviews: getContentPreviews,
  revise: reviseContentStoryboard,
  createPreviews: createContentPreviews,
  confirm: confirmContentJob,
  deleteJob: deleteContentJob,
  subscribeToEvents: subscribeToContentEvents,
  artifactUrl: contentArtifactUrl,
  previewUrl: contentPreviewUrl,
};

export function ContentView({
  locale,
  initialJobId = null,
  onJobChange,
  api = DEFAULT_API,
}: {
  locale: ContentLocale;
  initialJobId?: string | null;
  onJobChange(jobId: string | null): void;
  api?: ContentApi;
}): React.JSX.Element {
  const fileRef = useRef<HTMLInputElement>(null);
  const playerRef = useRef<HTMLVideoElement>(null);
  const [jobId, setJobId] = useState<string | null>(initialJobId);
  const [file, setFile] = useState<File | null>(null);
  const [transcript, setTranscript] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [goal, setGoal] = useState<ContentGoal>("faithful_clean");
  const [ranges, setRanges] = useState<ContentRangeInput[]>([]);
  const [selectedOrder, setSelectedOrder] = useState<string[]>([]);
  const [reorderAcknowledged, setReorderAcknowledged] = useState(false);
  const [chapterTitles, setChapterTitles] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lifecycleError = useCallback((caught: Error) => setError(caught.message), []);
  const { job, contentMap, plan, previews, applySnapshot, refresh } = useContentLifecycle({
    api,
    jobId,
    onError: lifecycleError,
  });

  useEffect(
    () => () => {
      if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
    },
    [sourceUrl],
  );

  useEffect(() => {
    if (!contentMap) return;
    setRanges(
      contentMap.user_ranges.map((range) => ({
        kind: range.kind,
        start_seconds: range.source_range.start_seconds,
        end_seconds: range.source_range.end_seconds,
        label: range.label,
      })),
    );
    setSelectedOrder([]);
    setReorderAcknowledged(false);
  }, [contentMap?.map_digest]);

  const seek = (seconds: number): void => {
    if (!playerRef.current) return;
    playerRef.current.currentTime = seconds;
    playerRef.current.focus();
  };

  const start = async (): Promise<void> => {
    if (!file) {
      fileRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.createJob(file, {
        goal,
        transcript,
        config: {
          allow_reorder: goal === "selected_clips",
          export_clips: goal === "selected_clips",
          export_subtitles: transcript !== null,
        },
      });
      setJobId(created.job_id);
      onJobChange(created.job_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start useful-content job.");
    } finally {
      setBusy(false);
    }
  };

  const revise = async (): Promise<void> => {
    if (!jobId || !job) return;
    setBusy(true);
    setError(null);
    try {
      const snapshot = await api.revise(jobId, {
        expected_revision: job.revision,
        ranges,
        selected_range_order: selectedOrder,
        reorder_acknowledged: reorderAcknowledged,
        chapter_titles: chapterTitles,
      });
      await applySnapshot(snapshot);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        await refresh();
        setError(contentText("stale", locale));
      } else {
        setError(caught instanceof Error ? caught.message : "Could not revise storyboard.");
      }
    } finally {
      setBusy(false);
    }
  };

  const preview = async (): Promise<void> => {
    if (!jobId) return;
    setBusy(true);
    setError(null);
    try {
      await applySnapshot(await api.createPreviews(jobId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not create previews.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (): Promise<void> => {
    if (!jobId || !job || !plan) return;
    setBusy(true);
    setError(null);
    try {
      await applySnapshot(await api.confirm(jobId, plan, job.revision));
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) await refresh();
      setError(caught instanceof Error ? caught.message : "Could not confirm exact plan.");
    } finally {
      setBusy(false);
    }
  };

  const reset = (): void => {
    if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
    setJobId(null);
    setFile(null);
    setTranscript(null);
    setSourceUrl(null);
    setRanges([]);
    setSelectedOrder([]);
    setReorderAcknowledged(false);
    setChapterTitles({});
    setError(null);
    onJobChange(null);
  };

  const remove = async (): Promise<void> => {
    if (!jobId) return;
    setBusy(true);
    try {
      const snapshot = await api.deleteJob(jobId);
      if (snapshot) await applySnapshot(snapshot);
      else reset();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not remove local task data.");
    } finally {
      setBusy(false);
    }
  };

  const terminal = job ? CONTENT_TERMINAL_STATUSES.has(job.status) : false;
  const selectedEmpty =
    goal === "selected_clips" &&
    !contentMap?.user_ranges.some((range) => range.kind === "keep" || range.kind === "locked_keep");
  const statusLabel = useMemo(
    () => (job ? contentStatusText(job.status, locale) : ""),
    [job, locale],
  );

  if (job && terminal) {
    return (
      <ContentResult
        locale={locale}
        job={job}
        plan={plan}
        artifactUrl={(path) => api.artifactUrl(job.job_id, path)}
        onNewTask={reset}
        onDelete={() => void remove()}
      />
    );
  }

  return (
    <main className="content-view">
      <header className="content-hero">
        <div>
          <p className="creator-mark">what912</p>
          <p className="eyebrow">MODE C / USEFUL CONTENT</p>
          <h1>{contentText("title", locale)}</h1>
          <p>{contentText("subtitle", locale)}</p>
          <small>{contentText("local", locale)}</small>
        </div>
        <div className="content-scope-mark" aria-hidden="true"><span /><span /></div>
      </header>
      {error && <p role="alert" className="content-error">{error}</p>}

      {!job && (
        <section className="content-start content-panel">
          <label className="content-file-field">
            {contentText("chooseVideo", locale)}
            <input
              ref={fileRef}
              type="file"
              accept="video/*"
              onChange={(event) => {
                const next = event.currentTarget.files?.[0] ?? null;
                setFile(next);
                if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
                setSourceUrl(next ? URL.createObjectURL(next) : null);
              }}
            />
          </label>
          <label className="content-file-field">
            {contentText("chooseTranscript", locale)}
            <input type="file" accept=".srt,.vtt,text/vtt" onChange={(event) => setTranscript(event.currentTarget.files?.[0] ?? null)} />
          </label>
          <ContentGoalSelector locale={locale} value={goal} onChange={setGoal} />
          <button type="button" className="primary-button" disabled={!file || busy} onClick={() => void start()}>
            {contentText("start", locale)}
          </button>
        </section>
      )}

      {job && (
        <section className="content-progress content-panel" aria-live="polite">
          <div><span>{statusLabel}</span><strong>{job.progress_percent}%</strong></div>
          <progress value={job.progress_percent} max="100">{job.progress_percent}%</progress>
          <p>{job.message}</p>
          {!terminal && <button type="button" className="danger-button" disabled={busy} onClick={() => void remove()}>{contentText("cancel", locale)}</button>}
        </section>
      )}

      {sourceUrl && (
        <section className="content-source-player content-panel">
          <video ref={playerRef} src={sourceUrl} controls preload="metadata"><track kind="captions" /></video>
        </section>
      )}
      {contentMap && <ContentMapTimeline locale={locale} contentMap={contentMap} onSeek={seek} />}
      {contentMap && job?.status === "awaiting_review" && (
        <>
          <ContentAIReview
            jobId={job.job_id}
            revision={job.revision}
            locale={locale}
            busy={busy}
            onApplied={applySnapshot}
          />
          <ContentStoryboard
            locale={locale}
            contentMap={contentMap}
            plan={null}
            ranges={ranges}
            selectedOrder={selectedOrder}
            reorderAcknowledged={reorderAcknowledged}
            chapterTitles={chapterTitles}
            busy={busy}
            onRangesChange={setRanges}
            onSelectedOrderChange={setSelectedOrder}
            onReorderAcknowledgedChange={setReorderAcknowledged}
            onChapterTitlesChange={setChapterTitles}
            onRevise={() => void revise()}
            onSeek={seek}
          />
          <button type="button" className="content-preview-button primary-button" disabled={busy || selectedEmpty} onClick={() => void preview()}>
            {contentText("createPreviews", locale)}
          </button>
        </>
      )}
      {job?.status === "ready_to_confirm" && plan && (
        <>
          <ContentJoinPreview locale={locale} previews={previews} urlFor={(path) => api.previewUrl(job.job_id, path)} onSeek={seek} />
          <ContentPlanReview locale={locale} plan={plan} busy={busy} onConfirm={() => void confirm()} />
        </>
      )}
    </main>
  );
}
