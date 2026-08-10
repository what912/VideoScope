# Windows zero-beginner onboarding implementation plan

1. Add failing launcher, protocol, bundle-audit and connector security tests.
2. Add the per-user PyInstaller/Inno Setup packaging contract and build audit.
3. Add a visible launcher with single-instance shutdown, FFmpeg diagnosis,
   explicit Winget consent and server-ready browser gating.
4. Register `videoscope://start` under the current user and remove it on
   uninstall.
5. Harden exact-origin access, one-time pairing expiry and failed-attempt
   limiting.
6. Replace the public connect page with a bilingual, ordered beginner flow and
   an advanced fallback.
7. Document install, checksum, pairing, troubleshooting and uninstall.
8. Run Python unit/static/type checks, frontend tests/typecheck/build, unified
   validation and—when build tools are available—the real installer smoke.

No step authorizes a commit, push, release or deployment.
