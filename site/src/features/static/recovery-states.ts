export const PUBLIC_RECOVERY_STATE_IDS = [
  "missing_file",
  "unsupported_media",
  "decode_failure",
  "duration_unavailable",
  "file_too_large",
  "canvas_unavailable",
  "memory_or_sample_cap",
  "cors_failure",
  "cancelled",
  "detector_failure",
  "no_findings",
  "local_report_missing",
  "shared_report_unavailable",
  "auth_unavailable",
  "optional_service_offline",
] as const;

export type PublicRecoveryStateId =
  (typeof PUBLIC_RECOVERY_STATE_IDS)[number];
