/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface TelegramWebApp {
  initData: string;
  ready: () => void;
  expand: () => void;
  themeParams?: Record<string, string | undefined>;
}

interface Window {
  Telegram?: { WebApp: TelegramWebApp };
}
