param(
    [Parameter(Mandatory = $true)]
    [string]$InputVideo,
    [string]$OutputDirectory = "runs\video-rescue"
)

# Interactive confirmation shows the plan digest after private previews are built.
videoscope rescue $InputVideo `
  --output $OutputDirectory `
  --strategy balanced `
  --symptom dark `
  --symptom video_noise `
  --preview-seconds 8
