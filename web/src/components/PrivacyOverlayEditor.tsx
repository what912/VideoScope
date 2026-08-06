import type { NormalizedBox } from "../types";
import type { WorkbenchLocale } from "./PublishReadyView";

interface PrivacyOverlayEditorProps {
  locale: WorkbenchLocale;
  box: NormalizedBox | null;
  evidenceUrl: string | null;
  sourceUrl: string | null;
  currentTime: number;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  onTimeChange: (seconds: number) => void;
  onBoxChange: (box: NormalizedBox) => void;
}

function clamp(value: number): number {
  return Math.max(0, Math.min(1, Number(value.toFixed(3))));
}

function moveBox(box: NormalizedBox, dx: number, dy: number): NormalizedBox {
  const width = box.x_max - box.x_min;
  const height = box.y_max - box.y_min;
  const xMin = clamp(Math.min(1 - width, Math.max(0, box.x_min + dx)));
  const yMin = clamp(Math.min(1 - height, Math.max(0, box.y_min + dy)));
  return { x_min: xMin, y_min: yMin, x_max: clamp(xMin + width), y_max: clamp(yMin + height) };
}

function resizeBox(box: NormalizedBox, dx: number, dy: number): NormalizedBox {
  return {
    ...box,
    x_max: clamp(Math.max(box.x_min + 0.02, box.x_max + dx)),
    y_max: clamp(Math.max(box.y_min + 0.02, box.y_max + dy)),
  };
}

export function PrivacyOverlayEditor({
  locale,
  box,
  evidenceUrl,
  sourceUrl,
  currentTime,
  videoRef,
  onTimeChange,
  onBoxChange,
}: PrivacyOverlayEditorProps): React.JSX.Element {
  const zh = locale === "zh-CN";
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    if (!box || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const delta = event.shiftKey ? 0.02 : 0.01;
    if (event.shiftKey) {
      onBoxChange(
        resizeBox(
          box,
          event.key === "ArrowRight" ? delta : event.key === "ArrowLeft" ? -delta : 0,
          event.key === "ArrowDown" ? delta : event.key === "ArrowUp" ? -delta : 0,
        ),
      );
      return;
    }
    onBoxChange(
      moveBox(
        box,
        event.key === "ArrowRight" ? delta : event.key === "ArrowLeft" ? -delta : 0,
        event.key === "ArrowDown" ? delta : event.key === "ArrowUp" ? -delta : 0,
      ),
    );
  };

  const beginDrag = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (!box) return;
    const target = event.currentTarget;
    const parent = target.parentElement;
    if (!parent) return;
    const origin = { x: event.clientX, y: event.clientY, box };
    target.setPointerCapture?.(event.pointerId);
    const move = (moveEvent: PointerEvent): void => {
      const bounds = parent.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      onBoxChange(
        moveBox(
          origin.box,
          (moveEvent.clientX - origin.x) / bounds.width,
          (moveEvent.clientY - origin.y) / bounds.height,
        ),
      );
    };
    const stop = (): void => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", stop);
      target.removeEventListener("pointercancel", stop);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", stop);
    target.addEventListener("pointercancel", stop);
  };

  const beginResize = (event: React.PointerEvent<HTMLButtonElement>): void => {
    if (!box) return;
    event.stopPropagation();
    const target = event.currentTarget;
    const parent = target.closest(".privacy-video-stage");
    if (!(parent instanceof HTMLElement)) return;
    const origin = { x: event.clientX, y: event.clientY, box };
    target.setPointerCapture?.(event.pointerId);
    const move = (moveEvent: PointerEvent): void => {
      const bounds = parent.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      onBoxChange(
        resizeBox(
          origin.box,
          (moveEvent.clientX - origin.x) / bounds.width,
          (moveEvent.clientY - origin.y) / bounds.height,
        ),
      );
    };
    const stop = (): void => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", stop);
      target.removeEventListener("pointercancel", stop);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", stop);
    target.addEventListener("pointercancel", stop);
  };

  return (
    <div
      className="privacy-video-stage"
      aria-label={zh ? "本地视频复核区域" : "Local video review area"}
    >
      {sourceUrl ? (
        <video
          ref={videoRef}
          src={sourceUrl}
          controls
          aria-label={zh ? "本地源视频" : "Local source video"}
          onTimeUpdate={(event) => onTimeChange(event.currentTarget.currentTime)}
        />
      ) : evidenceUrl ? (
        <img src={evidenceUrl} alt={zh ? "所选风险的私有证据帧" : "Private evidence frame for the selected risk"} />
      ) : (
        <div className="privacy-frame-placeholder">
          <span aria-hidden="true">⌁</span>
          <strong>{zh ? "本地帧观测区" : "Local frame observatory"}</strong>
        </div>
      )}
      {!sourceUrl && (
        <p className="privacy-source-unavailable">
          {zh
            ? "恢复任务后无法播放源视频。重新选择源视频后才能修改，或使用私有证据完成复核。"
            : "Source playback is unavailable after recovery. Re-select the source before revising or use private evidence for review."}
        </p>
      )}
      <span className="privacy-frame-time">{currentTime.toFixed(2)} s</span>
      {box && (
        <div
          className="privacy-overlay-box"
          role="slider"
          tabIndex={0}
          aria-label={zh ? "所选隐私区域" : "Selected privacy region"}
          aria-valuemin={0}
          aria-valuemax={1}
          aria-valuenow={box.x_min}
          aria-valuetext={`${box.x_min.toFixed(2)}, ${box.y_min.toFixed(2)}, ${box.x_max.toFixed(2)}, ${box.y_max.toFixed(2)}`}
          style={{
            left: `${box.x_min * 100}%`,
            top: `${box.y_min * 100}%`,
            width: `${(box.x_max - box.x_min) * 100}%`,
            height: `${(box.y_max - box.y_min) * 100}%`,
          }}
          onKeyDown={onKeyDown}
          onPointerDown={beginDrag}
        >
          <span>{zh ? "拖动区域 · Shift+方向键调整大小" : "Drag · Shift+arrows resize"}</span>
          <button
            type="button"
            aria-label={zh ? "调整所选隐私区域大小" : "Resize selected privacy region"}
            onPointerDown={beginResize}
          />
        </div>
      )}
    </div>
  );
}
