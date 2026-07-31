import {
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { useI18n } from "../../i18n/I18nProvider";
import type {
  BrowserAnalysisService,
} from "../../services/browser-analysis";
import {
  DirectMediaImportError,
  importDirectMediaUrl,
  type DirectMediaImportDependencies,
  type DirectMediaImportErrorCode,
} from "../../services/browser-analysis/url-import";
import type { ReportStore } from "../../services/report-store/report-store";
import {
  AnalysisModeSelector,
} from "./AnalysisModeSelector";
import {
  CPU_DETECTOR_IDS,
  createModeOptions,
  type BrowserAnalysisModeId,
  type CpuDetectorId,
} from "./analysis-modes";
import "./upload-lab.css";
import { UploadDropzone } from "./UploadDropzone";
import {
  type PublicAnalysisErrorCode,
  useAnalysisJob,
} from "./useAnalysisJob";
import {
  type UploadValidationErrorCode,
  validateLocalVideoSelection,
} from "./validation";

type UploadErrorCode =
  | UploadValidationErrorCode
  | DirectMediaImportErrorCode
  | PublicAnalysisErrorCode;

export interface UploadLabProps {
  analysisService: BrowserAnalysisService;
  reportStore: ReportStore;
  navigate(path: string): void;
  createObjectURL(file: File): string;
  revokeObjectURL(url: string): void;
  importUrl(
    input: string,
    dependencies: DirectMediaImportDependencies,
  ): Promise<File>;
  loadSample(): Promise<File>;
}

export function UploadLab({
  analysisService,
  reportStore,
  navigate,
  createObjectURL,
  revokeObjectURL,
  importUrl = importDirectMediaUrl,
  loadSample,
}: UploadLabProps) {
  const { locale, t } = useI18n();
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [mode, setMode] = useState<BrowserAnalysisModeId>("quick");
  const [detectors, setDetectors] = useState<CpuDetectorId[]>([
    ...CPU_DETECTOR_IDS,
  ]);
  const [errorCode, setErrorCode] = useState<UploadErrorCode | null>(null);
  const [directUrl, setDirectUrl] = useState("");
  const [urlConsent, setUrlConsent] = useState(false);
  const [importing, setImporting] = useState(false);
  const [loadingSample, setLoadingSample] = useState(false);
  const importController = useRef<AbortController | null>(null);
  const selectionOperationId = useRef(0);
  const [reducedMotion, setReducedMotion] = useState(() =>
    typeof window.matchMedia === "function"
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false,
  );
  const dependencies = useMemo(
    () => ({
      analysisService,
      reportStore,
      navigate,
      createObjectURL,
      revokeObjectURL,
    }),
    [
      analysisService,
      reportStore,
      navigate,
      createObjectURL,
      revokeObjectURL,
    ],
  );
  const job = useAnalysisJob(dependencies);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const preference = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    );
    const updatePreference = (event: MediaQueryListEvent) => {
      setReducedMotion(event.matches);
    };
    preference.addEventListener("change", updatePreference);
    setReducedMotion(preference.matches);
    return () => preference.removeEventListener("change", updatePreference);
  }, []);

  useEffect(
    () => () => {
      selectionOperationId.current += 1;
      importController.current?.abort();
      importController.current = null;
    },
    [],
  );

  function cancelSelectionOperations() {
    selectionOperationId.current += 1;
    importController.current?.abort();
    importController.current = null;
    setImporting(false);
    setLoadingSample(false);
  }

  function acceptFile(nextFile: File) {
    job.reset();
    const validation = validateLocalVideoSelection(nextFile, detectors);
    if (validation && validation !== "no_detectors_selected") {
      setFile(null);
      setErrorCode(validation);
      return;
    }
    setFile(nextFile);
    setErrorCode(null);
  }

  function selectFile(nextFile: File) {
    cancelSelectionOperations();
    acceptFile(nextFile);
  }

  function toggleDetector(detectorId: CpuDetectorId) {
    setDetectors((current) => {
      const next = current.includes(detectorId)
        ? current.filter((id) => id !== detectorId)
        : [...current, detectorId];
      if (next.length > 0 && errorCode === "no_detectors_selected") {
        setErrorCode(null);
      }
      return next;
    });
  }

  async function startAnalysis() {
    const validation = validateLocalVideoSelection(file, detectors);
    if (validation) {
      setErrorCode(validation);
      return;
    }
    if (!file) return;
    setErrorCode(null);
    await job.start(
      file,
      createModeOptions(mode, detectors, locale, reducedMotion),
    );
  }

  async function importRemote() {
    if (!urlConsent) {
      setErrorCode("consent_required");
      return;
    }
    job.reset();
    cancelSelectionOperations();
    const operationId = selectionOperationId.current;
    const controller = new AbortController();
    importController.current = controller;
    setImporting(true);
    setErrorCode(null);
    try {
      const imported = await importUrl(directUrl, {
        consent: true,
        signal: controller.signal,
      });
      if (
        selectionOperationId.current !== operationId ||
        controller.signal.aborted
      ) {
        return;
      }
      acceptFile(imported);
    } catch (error) {
      if (selectionOperationId.current !== operationId) return;
      if (
        error instanceof DOMException &&
        error.name === "AbortError"
      ) {
        return;
      }
      setErrorCode(
        error instanceof DirectMediaImportError
          ? error.code
          : "cors_or_network",
      );
    } finally {
      if (
        selectionOperationId.current === operationId &&
        importController.current === controller
      ) {
        importController.current = null;
        setImporting(false);
      }
    }
  }

  async function selectSample() {
    job.reset();
    cancelSelectionOperations();
    const operationId = selectionOperationId.current;
    setLoadingSample(true);
    setErrorCode(null);
    try {
      const sample = await loadSample();
      if (selectionOperationId.current !== operationId) return;
      acceptFile(sample);
    } catch {
      if (selectionOperationId.current !== operationId) return;
      setErrorCode("analysis_failed");
    } finally {
      if (selectionOperationId.current === operationId) {
        setLoadingSample(false);
      }
    }
  }

  const activeError =
    job.state.status === "failed" ? job.state.error.code : errorCode;
  const progress =
    job.state.status === "running" ? job.state.progress : null;

  return (
    <section aria-labelledby="upload-lab-title" className="upload-lab">
      <header className="upload-lab__header" role="presentation">
        <p className="upload-lab__eyebrow">{t.upload.eyebrow}</p>
        <h2 id="upload-lab-title">{t.upload.title}</h2>
        <p>{t.upload.description}</p>
        <p className="upload-lab__privacy">
          <span aria-hidden="true">●</span> {t.upload.privacy}
        </p>
      </header>

      <UploadDropzone
        copy={t.upload}
        dragging={dragging}
        file={file}
        onDragStateChange={setDragging}
        onFile={selectFile}
      />

      <div className="upload-lab__sample">
        <button
          className="button button--quiet"
          disabled={loadingSample}
          onClick={() => void selectSample()}
          type="button"
        >
          {loadingSample ? t.upload.loadingSample : t.upload.sample}
        </button>
      </div>

      <AnalysisModeSelector
        copy={t.upload}
        onSelect={setMode}
        selectedMode={mode}
      />

      <fieldset className="detector-selector">
        <legend>{t.upload.detectorsLegend}</legend>
        {CPU_DETECTOR_IDS.map((detectorId) => (
          <label key={detectorId}>
            <input
              checked={detectors.includes(detectorId)}
              onChange={() => toggleDetector(detectorId)}
              type="checkbox"
            />
            <span>{t.upload.detectors[detectorId]}</span>
          </label>
        ))}
      </fieldset>

      <div className="direct-url">
        <h3>{t.upload.directUrlTitle}</h3>
        <label htmlFor="direct-video-url">{t.upload.directUrlLabel}</label>
        <div className="direct-url__row">
          <input
            id="direct-video-url"
            inputMode="url"
            onChange={(event) => setDirectUrl(event.target.value)}
            placeholder={t.upload.directUrlPlaceholder}
            type="url"
            value={directUrl}
          />
          <button
            className="button button--quiet"
            disabled={importing}
            onClick={() => void importRemote()}
            type="button"
          >
            {importing ? t.upload.importingUrl : t.upload.importUrl}
          </button>
        </div>
        <p className="direct-url__notice">{t.upload.directUrlNotice}</p>
        <label className="direct-url__consent">
          <input
            checked={urlConsent}
            onChange={(event) => setUrlConsent(event.target.checked)}
            type="checkbox"
          />
          <span>{t.upload.directUrlConsent}</span>
        </label>
      </div>

      <details className="upload-lab__advanced">
        <summary>{t.upload.advanced}</summary>
        <p>{t.upload.samplingSummary}</p>
      </details>

      {activeError ? (
        <p aria-live="polite" className="upload-lab__error" role="alert">
          <strong aria-hidden="true">!</strong>
          {t.upload.errors[activeError]}
        </p>
      ) : null}

      {progress ? (
        <div
          aria-label={t.upload.progressLabel}
          aria-live="polite"
          className="analysis-progress"
        >
          <div>
            <strong>{t.upload.stages[progress.stage]}</strong>
            <span className="numeric">
              {Math.round(progress.progress * 100)}%
            </span>
          </div>
          <progress max={1} value={progress.progress} />
          {progress.detector_id ? (
            <small className="numeric">{progress.detector_id}</small>
          ) : null}
        </div>
      ) : null}

      {job.state.status === "cancelled" ? (
        <p aria-live="polite" className="upload-lab__status">
          {t.upload.cancelled}
        </p>
      ) : null}
      {job.state.status === "completed" ? (
        <p aria-live="polite" className="upload-lab__status">
          {t.upload.complete}
        </p>
      ) : null}

      <div className="upload-lab__actions">
        {job.state.status === "running" ? (
          <button
            className="button button--quiet"
            onClick={job.cancel}
            type="button"
          >
            {t.upload.cancel}
          </button>
        ) : (
          <button
            className="button button--primary"
            onClick={() => void startAnalysis()}
            type="button"
          >
            {t.upload.start}
          </button>
        )}
      </div>
    </section>
  );
}
