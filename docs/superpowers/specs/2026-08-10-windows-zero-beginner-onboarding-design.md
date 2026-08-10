# Windows zero-beginner onboarding design

## Outcome

A new Windows user should move from the public `/connect` page to a full local
analysis without installing Python, typing a command, exposing a port to the
LAN, or sending a video to a VideoScope server.

## Approved flow

1. Download the official per-user installer from a GitHub Release.
2. Double-click and keep recommended options.
3. Launch the visible connector after install.
4. Check FFmpeg/ffprobe and explain any missing dependency. Winget installation
   requires an explicit confirmation.
5. Bind only to `127.0.0.1:8765`; open the fixed public connect URL only after
   server startup is confirmed.
6. Show the pairing code in a native window, never in a URL, command line,
   file, persistent notification, or access log.
7. Pair the browser with a one-time, ten-minute code and an expiring in-memory
   session.
8. Continue to the first full analysis and retain advanced fallback guidance in
   a collapsed section.

## Security and distribution boundaries

- Current-user install; no elevation, service, startup task, firewall rule or
  LAN bind.
- Exact public-origin CORS/PNA and exact same-origin loopback settings access.
- No bundled FFmpeg, model weights, media, credentials or personal paths.
- No automatic model download or background provider call.
- The installer build is audited, smoke-installed and smoke-uninstalled before
  it becomes a release asset.
- Until paid code signing exists, the UI must disclose the publisher warning
  and require users to verify the GitHub source and SHA-256 checksum.

## UX states

The page distinguishes checking, offline, ready to pair, pairing, paired and
error. The connector status also distinguishes ready from degraded when
FFmpeg or ffprobe is missing. Polling pauses when the page is hidden and no
absolute local path is displayed.
