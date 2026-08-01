import { homepageMedia, type HomepageMediaRole } from "../../data/media-manifest";

export const repositoryUrl = "https://github.com/what912/VideoScope";

export const detectorProtocolExample = `{
  "detector_id": "global_flicker",
  "detector_version": "browser-1",
  "time_range": {
    "start_seconds": 3.2,
    "end_seconds": 4.1
  },
  "severity": "medium",
  "score": 0.72,
  "confidence": 0.79,
  "evidence": ["frame@3.65s"],
  "limitations": ["Intentional lighting can resemble flicker."]
}`;

export function mediaFor(role: HomepageMediaRole) {
  const media = homepageMedia.find((item) => item.role === role);
  if (!media) throw new Error(`Missing homepage media role: ${role}`);
  return media;
}
