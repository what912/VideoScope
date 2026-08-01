import { useEffect, useMemo, useRef, useState } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import {
  clampTime,
  containedMediaRect,
  formatTimestamp,
} from "./diagnostic-geometry";
import { DiagnosticOverlay } from "./DiagnosticOverlay";
import "./diagnostics.css";

interface VideoPlayerProps {
  src?: string;
  poster?: string;
  currentTime: number;
  duration: number;
  playing: boolean;
  playbackRate: number;
  videoWidth?: number;
  videoHeight?: number;
  selectedFinding?: Finding;
  onSeek(seconds: number): void;
  onPlayingChange(playing: boolean): void;
  onTimeUpdate?(seconds: number): void;
}

export function VideoPlayer({
  src,
  poster,
  currentTime,
  duration,
  playing,
  playbackRate,
  videoWidth,
  videoHeight,
  selectedFinding,
  onSeek,
  onPlayingChange,
  onTimeUpdate,
}: VideoPlayerProps) {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const playerRef = useRef<HTMLDivElement>(null);
  const [containerSize, setContainerSize] = useState({ width: 0, height: 0 });
  const [intrinsicSize, setIntrinsicSize] = useState({ width: 0, height: 0 });
  const hasExplicitSize =
    Number.isFinite(videoWidth) &&
    Number.isFinite(videoHeight) &&
    (videoWidth ?? 0) > 0 &&
    (videoHeight ?? 0) > 0;
  const mediaWidth = hasExplicitSize ? (videoWidth ?? 0) : intrinsicSize.width;
  const mediaHeight = hasExplicitSize
    ? (videoHeight ?? 0)
    : intrinsicSize.height;
  const mediaRect = useMemo(
    () =>
      containedMediaRect(
        containerSize.width,
        containerSize.height,
        mediaWidth,
        mediaHeight,
      ),
    [containerSize, mediaHeight, mediaWidth],
  );

  useEffect(() => {
    const player = playerRef.current;
    if (!player) return;
    const update = (width: number, height: number) => {
      setContainerSize((current) =>
        current.width === width && current.height === height
          ? current
          : { width, height },
      );
    };
    update(player.clientWidth, player.clientHeight);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) update(entry.contentRect.width, entry.contentRect.height);
    });
    observer.observe(player);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !src) return;
    if (Math.abs(video.currentTime - currentTime) > 0.08) {
      video.currentTime = clampTime(currentTime, duration);
    }
    video.playbackRate = playbackRate;
  }, [currentTime, duration, playbackRate, src]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !src) return;
    if (playing) {
      void video.play().catch(() => onPlayingChange(false));
    } else {
      video.pause();
    }
  }, [onPlayingChange, playing, src]);

  const seekByKey = (event: React.KeyboardEvent<HTMLDivElement>) => {
    let next: number | undefined;
    if (event.key === "ArrowLeft") next = currentTime - 1;
    if (event.key === "ArrowRight") next = currentTime + 1;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = duration;
    if (event.key === " " && src) {
      event.preventDefault();
      onPlayingChange(!playing);
      return;
    }
    if (next !== undefined) {
      event.preventDefault();
      onSeek(clampTime(next, duration));
    }
  };

  return (
    <section className="video-player-shell">
      <div
        aria-label={t.diagnostics.videoPlayer}
        className="video-player"
        data-testid="video-player"
        onKeyDown={seekByKey}
        ref={playerRef}
        role="region"
        tabIndex={0}
      >
        {src ? (
          <video
            aria-label={t.diagnostics.video}
            onLoadedMetadata={(event) =>
              setIntrinsicSize({
                width: event.currentTarget.videoWidth,
                height: event.currentTarget.videoHeight,
              })
            }
            onPause={() => onPlayingChange(false)}
            onPlay={() => onPlayingChange(true)}
            onTimeUpdate={(event) =>
              onTimeUpdate?.(event.currentTarget.currentTime)
            }
            playsInline
            poster={poster}
            preload="metadata"
            ref={videoRef}
            src={src}
          />
        ) : (
          <div className="video-player__empty">{t.diagnostics.noVideo}</div>
        )}
        <div
          className="video-player__media-layer"
          data-testid="diagnostic-media-layer"
          style={
            containerSize.width > 0 && containerSize.height > 0
              ? {
                  left: `${mediaRect.left}px`,
                  top: `${mediaRect.top}px`,
                  width: `${mediaRect.width}px`,
                  height: `${mediaRect.height}px`,
                }
              : undefined
          }
        >
          <DiagnosticOverlay finding={selectedFinding} />
        </div>
      </div>
      <div className="video-player__controls">
        <button
          className="button button--quiet"
          disabled={!src}
          onClick={() => onPlayingChange(!playing)}
          type="button"
        >
          {playing ? t.diagnostics.pause : t.diagnostics.play}
        </button>
        <span className="numeric">
          {formatTimestamp(currentTime)} / {formatTimestamp(duration)}
        </span>
        <span className="numeric">{playbackRate.toFixed(2)}×</span>
      </div>
    </section>
  );
}
