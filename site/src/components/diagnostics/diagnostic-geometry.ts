function finiteOr(value: number, fallback: number) {
  return Number.isFinite(value) ? value : fallback;
}

export function clampTime(value: number, durationSeconds: number) {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) return 0;
  return Math.min(durationSeconds, Math.max(0, finiteOr(value, 0)));
}

export function intervalToPercent(
  startSeconds: number,
  endSeconds: number,
  durationSeconds: number,
): { left: number; width: number } {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    return { left: 0, width: 0 };
  }
  const start = clampTime(startSeconds, durationSeconds);
  const end = Number.isFinite(endSeconds)
    ? clampTime(endSeconds, durationSeconds)
    : start;
  return {
    left: (start / durationSeconds) * 100,
    width: (Math.max(start, end) - start) / durationSeconds * 100,
  };
}

export interface ContainedMediaRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export function containedMediaRect(
  containerWidth: number,
  containerHeight: number,
  mediaWidth: number,
  mediaHeight: number,
): ContainedMediaRect {
  const safeContainerWidth =
    Number.isFinite(containerWidth) && containerWidth > 0 ? containerWidth : 0;
  const safeContainerHeight =
    Number.isFinite(containerHeight) && containerHeight > 0
      ? containerHeight
      : 0;
  if (
    !Number.isFinite(mediaWidth) ||
    !Number.isFinite(mediaHeight) ||
    mediaWidth <= 0 ||
    mediaHeight <= 0
  ) {
    return {
      left: 0,
      top: 0,
      width: safeContainerWidth,
      height: safeContainerHeight,
    };
  }

  const scale = Math.min(
    safeContainerWidth / mediaWidth,
    safeContainerHeight / mediaHeight,
  );
  const width = mediaWidth * scale;
  const height = mediaHeight * scale;
  return {
    left: (safeContainerWidth - width) / 2,
    top: (safeContainerHeight - height) / 2,
    width,
    height,
  };
}

export function formatTimestamp(seconds: number) {
  const safe = Math.max(0, finiteOr(seconds, 0));
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder
    .toFixed(1)
    .padStart(4, "0")}`;
}
