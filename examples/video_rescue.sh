#!/usr/bin/env sh
set -eu

input_video=${1:?usage: video_rescue.sh INPUT [OUTPUT_DIRECTORY]}
output_directory=${2:-runs/video-rescue}

# Interactive confirmation shows the plan digest after private previews are built.
videoscope rescue "$input_video" \
  --output "$output_directory" \
  --strategy balanced \
  --symptom dark \
  --symptom video_noise \
  --preview-seconds 8
