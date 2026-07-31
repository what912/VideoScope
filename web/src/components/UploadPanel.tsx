import { useRef, useState, type DragEvent, type FormEvent } from "react";
import type { AnalysisOptions, DetectorManifest } from "../types";

interface Props {
  detectors: DetectorManifest[];
  loadingDetectors: boolean;
  initialError: string | null;
  onSubmit: (file: File, prompt: string, options: AnalysisOptions) => void;
}

const MAX_UPLOAD_HINT = "Default local limit: 1,024 MiB";

export function UploadPanel({
  detectors,
  loadingDetectors,
  initialError,
  onSubmit,
}: Props): React.JSX.Element {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [prompt, setPrompt] = useState("");
  const [dragging, setDragging] = useState(false);
  const [sampleFps, setSampleFps] = useState(2);
  const [thumbnailSize, setThumbnailSize] = useState(640);
  const [locale, setLocale] = useState("en");
  const [selected, setSelected] = useState<Set<string>>(
    () =>
      new Set(
        detectors.filter((item) => item.default_enabled).map((item) => item.id),
      ),
  );

  const effectiveSelected =
    selected.size === 0 && detectors.length > 0
      ? new Set(
          detectors
            .filter((item) => item.default_enabled)
            .map((item) => item.id),
        )
      : selected;

  const updateFile = (candidate?: File): void => {
    if (candidate) setFile(candidate);
  };

  const drop = (event: DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    setDragging(false);
    updateFile(event.dataTransfer.files[0]);
  };

  const toggle = (id: string): void => {
    setSelected((current) => {
      const next = new Set(effectiveSelected);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const submit = (event: FormEvent): void => {
    event.preventDefault();
    if (!file) {
      inputRef.current?.focus();
      return;
    }
    onSubmit(file, prompt, {
      sampleFps,
      thumbnailMaxSize: thumbnailSize,
      locale,
      detectorIds: [...effectiveSelected].sort(),
    });
  };

  const cpu = detectors.filter((item) => item.category === "cpu");
  const optional = detectors.filter((item) => item.category !== "cpu");

  return (
    <main className="home-shell">
      <section className="hero">
        <p className="eyebrow">Local-first video diagnostics</p>
        <h1>Find the moment a generated video stops holding together.</h1>
        <p className="hero-copy">
          Drop in a local video. VideoScope maps observable quality signals to
          precise time ranges, evidence frames, and a report you can audit.
        </p>
        <div className="trust-row" aria-label="Privacy guarantees">
          <span>● Runs on this machine</span>
          <span>● No default upload</span>
          <span>● CPU baseline</span>
        </div>
      </section>

      <form className="analysis-card" onSubmit={submit}>
        <div className="card-heading">
          <div>
            <p className="step-label">01 / Input</p>
            <h2>New analysis</h2>
          </div>
          <span className="format-note">MP4 · MOV · MKV · WEBM</span>
        </div>

        <div
          className={`drop-zone ${dragging ? "is-dragging" : ""} ${
            file ? "has-file" : ""
          }`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={drop}
          onClick={() => inputRef.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              inputRef.current?.click();
            }
          }}
          aria-label="Choose a local video"
        >
          <input
            ref={inputRef}
            className="visually-hidden"
            type="file"
            accept="video/*,.mkv"
            onChange={(event) => updateFile(event.target.files?.[0])}
          />
          <span className="upload-icon" aria-hidden="true">
            ↑
          </span>
          {file ? (
            <>
              <strong>{file.name}</strong>
              <span>{(file.size / 1024 / 1024).toFixed(1)} MiB · ready locally</span>
            </>
          ) : (
            <>
              <strong>Drop a video here</strong>
              <span>or click to choose a local file</span>
            </>
          )}
          <small>{MAX_UPLOAD_HINT}</small>
        </div>

        <label className="field">
          <span>
            Prompt <em>optional</em>
          </span>
          <textarea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={3}
            placeholder="What was the video intended to show?"
          />
        </label>

        <fieldset className="detector-fieldset">
          <legend>
            <span className="step-label">02 / Checks</span>
            <strong>CPU detectors</strong>
          </legend>
          {loadingDetectors ? (
            <p className="muted">Reading local detector manifest…</p>
          ) : (
            <div className="detector-grid">
              {cpu.map((detector) => (
                <DetectorOption
                  key={detector.id}
                  detector={detector}
                  checked={effectiveSelected.has(detector.id)}
                  onChange={() => toggle(detector.id)}
                />
              ))}
            </div>
          )}
        </fieldset>

        <fieldset className="detector-fieldset optional-detectors">
          <legend>
            <strong>Optional AI &amp; OCR</strong>
          </legend>
          <div className="detector-grid">
            {optional.map((detector) => (
              <DetectorOption
                key={detector.id}
                detector={detector}
                checked={effectiveSelected.has(detector.id)}
                onChange={() => toggle(detector.id)}
              />
            ))}
          </div>
        </fieldset>

        <details className="advanced-settings">
          <summary>Advanced settings</summary>
          <div className="advanced-grid">
            <label className="field">
              <span>Sample FPS</span>
              <input
                type="number"
                min="0.1"
                max="24"
                step="0.1"
                value={sampleFps}
                onChange={(event) => setSampleFps(event.target.valueAsNumber)}
              />
            </label>
            <label className="field">
              <span>Thumbnail max edge</span>
              <input
                type="number"
                min="160"
                max="1920"
                step="16"
                value={thumbnailSize}
                onChange={(event) => setThumbnailSize(event.target.valueAsNumber)}
              />
            </label>
            <label className="field">
              <span>Report locale</span>
              <select value={locale} onChange={(event) => setLocale(event.target.value)}>
                <option value="en">English</option>
                <option value="zh-CN">简体中文</option>
              </select>
            </label>
          </div>
        </details>

        {initialError && <p className="form-error">{initialError}</p>}
        <button className="primary-button" type="submit" disabled={!file}>
          <span>Start local analysis</span>
          <span aria-hidden="true">→</span>
        </button>
      </form>
    </main>
  );
}

function DetectorOption({
  detector,
  checked,
  onChange,
}: {
  detector: DetectorManifest;
  checked: boolean;
  onChange: () => void;
}): React.JSX.Element {
  return (
    <label
      className={`detector-option ${!detector.available ? "is-unavailable" : ""}`}
    >
      <input
        type="checkbox"
        checked={checked && detector.available}
        disabled={!detector.available}
        onChange={onChange}
      />
      <span className="custom-check" aria-hidden="true" />
      <span className="detector-copy">
        <strong>{detector.display_name}</strong>
        <small>{detector.description}</small>
        {!detector.available && (
          <span className="unavailable-reason">
            Not installed — {detector.unavailable_reason}
          </span>
        )}
      </span>
      <span className="cost-chip">{detector.estimated_cost}</span>
    </label>
  );
}
