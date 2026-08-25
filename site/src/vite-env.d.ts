/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_WINDOWS_INSTALLER_URL?: string;
  readonly VITE_WINDOWS_INSTALLER_SHA256?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
