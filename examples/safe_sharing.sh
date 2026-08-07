#!/usr/bin/env sh
set -eu

if [ "$#" -lt 1 ]; then
  printf '%s\n' "usage: safe_sharing.sh INPUT [OUTPUT] [REVIEW_FILE] [CONFIRM_DIGEST]" >&2
  exit 2
fi

input_video=$1
output_directory=${2:-runs/safe-sharing}
review_file=${3:-}
confirm_digest=${4:-}

if [ -n "$confirm_digest" ]; then
  exec videoscope privacy "$input_video" \
    --output "$output_directory" \
    --confirm-digest "$confirm_digest"
fi

if [ -n "$review_file" ]; then
  if [ ! -f "$review_file" ]; then
    printf '%s\n' "review file not found: $review_file" >&2
    exit 2
  fi
  exec videoscope privacy "$input_video" \
    --output "$output_directory" \
    --review-file "$review_file" \
    --preview-only
fi

exec videoscope privacy "$input_video" \
  --output "$output_directory" \
  --audience public \
  --scan-only
