export const MAX_LOCAL_VIDEO_BYTES = 500 * 1024 * 1024;

export const SUPPORTED_LOCAL_VIDEO_TYPES = new Set([
  "video/mp4",
  "video/webm",
  "video/quicktime",
  "video/x-matroska",
]);

export const SUPPORTED_LOCAL_VIDEO_EXTENSIONS = new Set([
  ".mp4",
  ".webm",
  ".mov",
  ".mkv",
]);

export type UploadValidationErrorCode =
  | "file_required"
  | "empty_file"
  | "unsupported_type"
  | "file_too_large"
  | "no_detectors_selected";

export function validateLocalVideoSelection(
  file: File | null,
  enabledDetectorIds: readonly string[],
): UploadValidationErrorCode | null {
  if (!file) return "file_required";
  if (file.size <= 0) return "empty_file";
  const mimeType = file.type.trim().toLowerCase();
  const extensionIndex = file.name.lastIndexOf(".");
  const extension =
    extensionIndex >= 0 ? file.name.slice(extensionIndex).toLowerCase() : "";
  if (
    mimeType
      ? !SUPPORTED_LOCAL_VIDEO_TYPES.has(mimeType)
      : !SUPPORTED_LOCAL_VIDEO_EXTENSIONS.has(extension)
  ) {
    return "unsupported_type";
  }
  if (file.size > MAX_LOCAL_VIDEO_BYTES) return "file_too_large";
  if (enabledDetectorIds.length === 0) return "no_detectors_selected";
  return null;
}
