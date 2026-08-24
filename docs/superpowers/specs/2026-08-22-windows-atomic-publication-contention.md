# Windows Atomic Publication Contention Design

## Status

Approved by the user on 2026-08-22.

## Problem

The unrestricted unified validation produced two Windows-only
`PermissionError: [WinError 5]` failures after freshly written handles had
already been closed:

- clarity provenance replaced an exclusively created empty final placeholder
  with the completed partial file;
- the V15 demo verifier renamed a completed staging directory to an absent
  no-clobber output.

The same bundle test passed in focused sandboxed runs with both the old raw
`MoveFileExW` boundary and the current `os.rename` boundary. Replacing one
Windows rename API with another therefore does not address the observed
execution-context-sensitive contention.

## Root-cause hypothesis

Under the long unrestricted Windows test load, a filesystem filter or other
external reader can briefly retain a handle without delete sharing. Windows
rename operations require delete access, so either file replacement or
directory rename can return `WinError 5` during that bounded contention
window. The exact external holder was not retained after pytest reused the
fresh temporary root, so the holder identity remains unproven; the hypothesis
is intentionally tested through deterministic injected failures.

## Approved behavior

1. Keep atomic no-clobber publication. Never delete, replace, or overwrite a
   concurrent destination.
2. On Windows only, retry a rename only when all of these facts hold:
   - the error is exactly `WinError 5`;
   - the source still exists without following links;
   - the destination still does not exist without following links;
   - a configured retry delay remains.
3. Use delays of `0.01`, `0.02`, `0.04`, `0.08`, and `0.16` seconds: at most
   six total rename attempts and at most `0.31` seconds of waiting.
4. Surface the original final error when the bounded attempts are exhausted.
5. Never retry `FileExistsError`, any non-`WinError 5`, a missing source, or
   an appearing destination.
6. For clarity provenance on Windows, remove the self-created empty final
   placeholder and rename the fully flushed partial file directly to the
   absent final name. Keep the existing non-Windows placeholder-and-replace
   implementation unchanged.
7. Keep all algorithm and qualification thresholds unchanged. Do not add
   skips, xfails, network access, dependencies, FFmpeg/ffprobe launches, Git
   operations, release operations, PREPARE, or execute.

## Validation sequence

Use deterministic fault injection to establish RED before implementation,
then run the focused non-native tests and static checks. Format only the two
files identified by the retained unified Ruff failure. After an independent
review, request separate authorization before another unrestricted unified
validation.
