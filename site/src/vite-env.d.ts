/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_WINDOWS_INSTALLER_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
