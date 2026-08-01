#!/usr/bin/env sh
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $0 INPUT_VIDEO [OUTPUT_DIRECTORY]" >&2
  exit 2
fi

input_video=$1
output_directory=${2:-runs/basic-cli}

videoscope --version
videoscope doctor
videoscope analyze "$input_video" --output "$output_directory"

printf 'JSON report: %s/report.json\n' "$output_directory"
printf 'HTML report: %s/report.html\n' "$output_directory"
