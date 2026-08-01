export interface SessionVideo {
  reportId: string;
  file: File;
  objectUrl: string;
}

let sessionVideo: SessionVideo | null = null;
let revokeSessionUrl: ((url: string) => void) | null = null;

export function getSessionVideo(): SessionVideo | null {
  return sessionVideo;
}

export function setSessionVideo(
  nextVideo: SessionVideo,
  revokeObjectURL: (url: string) => void = URL.revokeObjectURL,
) {
  if (
    sessionVideo &&
    sessionVideo.objectUrl !== nextVideo.objectUrl &&
    revokeSessionUrl
  ) {
    revokeSessionUrl(sessionVideo.objectUrl);
  }
  sessionVideo = nextVideo;
  revokeSessionUrl = revokeObjectURL;
}

export function clearSessionVideo() {
  if (sessionVideo && revokeSessionUrl) {
    revokeSessionUrl(sessionVideo.objectUrl);
  }
  sessionVideo = null;
  revokeSessionUrl = null;
}
