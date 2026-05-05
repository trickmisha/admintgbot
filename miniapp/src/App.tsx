import { useCallback, useEffect, useState, useRef } from "react";
import { apiDelete, apiGet, apiPost, apiPut } from "./api";

// ─── Types ────────────────────────────────────────────────────────────────────

type Stats = {
  total_users: number;
  vip_count: number;
  in_drip: number;
  blocked_count: number;
  conversion: number;
};

type DripStep = {
  step: number;
  text: string | null;
  media_file_id: string | null;
  media_type: string;
  button_text: string | null;
  button_url: string | null;
  delay_hours: number;
  is_active: boolean;
};

type Settings = {
  welcome_text: string;
  button1_text: string;
  button1_url: string;
  button2_text: string;
  button2_url: string;
  paid_channel_link: string;
};

type PostRow = {
  id: number;
  channel: string;
  text: string | null;
  media_id: string | null;
  button_text: string | null;
  button_url: string | null;
  scheduled_at: string | null;
  status: string;
};

type TabId = "dash" | "drip" | "set" | "bc" | "posts";

// ─── Styles ───────────────────────────────────────────────────────────────────

const css = `
  @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700;800&family=DM+Sans:wght@300;400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0a0a0f;
    --bg2: #111118;
    --bg3: #1a1a24;
    --border: rgba(255,255,255,0.07);
    --text: #f0f0f8;
    --muted: #6b6b80;
    --accent: #a78bfa;
    --accent2: #7c3aed;
    --green: #34d399;
    --red: #f87171;
    --amber: #fbbf24;
    --card-shadow: 0 4px 24px rgba(0,0,0,0.4);
  }

  html, body, #root { height: 100%; overflow: hidden; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }

  .app {
    display: flex;
    flex-direction: column;
    height: 100%;
    max-width: 480px;
    margin: 0 auto;
  }

  /* ── Header ── */
  .header {
    padding: 16px 20px 0;
    flex-shrink: 0;
  }
  .header-inner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 2px;
  }
  .header-title {
    font-family: 'Syne', sans-serif;
    font-size: 22px;
    font-weight: 800;
    letter-spacing: -0.5px;
    background: linear-gradient(135deg, #f0f0f8 0%, #a78bfa 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .header-badge {
    background: linear-gradient(135deg, #a78bfa20, #7c3aed20);
    border: 1px solid #a78bfa40;
    color: #a78bfa;
    font-size: 11px;
    font-weight: 500;
    padding: 3px 10px;
    border-radius: 20px;
    letter-spacing: 0.5px;
  }
  .header-sub {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 16px;
  }

  /* ── Tabs ── */
  .tabs {
    display: flex;
    gap: 4px;
    padding: 0 20px 12px;
    flex-shrink: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .tabs::-webkit-scrollbar { display: none; }
  .tab {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 7px 13px;
    border-radius: 10px;
    border: 1px solid var(--border);
    background: var(--bg2);
    color: var(--muted);
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    font-weight: 400;
    cursor: pointer;
    transition: all 0.15s;
    white-space: nowrap;
  }
  .tab:hover { border-color: rgba(255,255,255,0.12); color: var(--text); }
  .tab.active {
    background: linear-gradient(135deg, #a78bfa15, #7c3aed15);
    border-color: #a78bfa50;
    color: #a78bfa;
    font-weight: 500;
  }
  .tab-ico { font-size: 14px; }

  /* ── Content ── */
  .content {
    flex: 1;
    overflow-y: auto;
    padding: 0 20px 20px;
    scrollbar-width: thin;
    scrollbar-color: var(--bg3) transparent;
  }
  .content::-webkit-scrollbar { width: 4px; }
  .content::-webkit-scrollbar-thumb { background: var(--bg3); border-radius: 2px; }

  /* ── Toast ── */
  .toast-wrap {
    position: fixed;
    top: 16px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 100;
    pointer-events: none;
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: center;
  }
  .toast {
    padding: 8px 16px;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 500;
    animation: toastIn 0.2s ease;
    white-space: nowrap;
  }
  .toast.ok { background: #34d39920; border: 1px solid #34d39940; color: #34d399; }
  .toast.err { background: #f8717120; border: 1px solid #f8717140; color: #f87171; }
  @keyframes toastIn { from { opacity: 0; transform: translateY(-8px); } to { opacity: 1; transform: translateY(0); } }

  /* ── Cards grid ── */
  .stat-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 16px;
  }
  .stat-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 16px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.15s;
  }
  .stat-card:hover { border-color: rgba(255,255,255,0.12); }
  .stat-card.wide { grid-column: span 2; }
  .stat-card-glow {
    position: absolute;
    top: -20px; right: -20px;
    width: 60px; height: 60px;
    border-radius: 50%;
    opacity: 0.15;
    filter: blur(20px);
  }
  .stat-label {
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
  }
  .stat-val {
    font-family: 'Syne', sans-serif;
    font-size: 28px;
    font-weight: 700;
    color: var(--text);
    line-height: 1;
  }
  .stat-val.accent { color: var(--accent); }
  .stat-val.green { color: var(--green); }
  .stat-val.amber { color: var(--amber); }
  .stat-val.red { color: var(--red); }

  /* ── Conv bar ── */
  .conv-wrap { margin-top: 8px; }
  .conv-bar-track {
    height: 4px;
    background: var(--bg3);
    border-radius: 2px;
    overflow: hidden;
    margin-top: 6px;
  }
  .conv-bar-fill {
    height: 100%;
    border-radius: 2px;
    background: linear-gradient(90deg, #a78bfa, #7c3aed);
    transition: width 0.8s cubic-bezier(0.4,0,0.2,1);
  }

  /* ── Section header ── */
  .section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 20px 0 12px;
  }
  .section-title {
    font-family: 'Syne', sans-serif;
    font-size: 15px;
    font-weight: 700;
    color: var(--text);
  }
  .section-count {
    font-size: 11px;
    color: var(--muted);
    background: var(--bg3);
    padding: 2px 8px;
    border-radius: 20px;
  }

  /* ── Drip steps ── */
  .drip-step {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 14px;
    margin-bottom: 10px;
    overflow: hidden;
    transition: border-color 0.15s;
  }
  .drip-step:hover { border-color: rgba(255,255,255,0.1); }
  .drip-step-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    cursor: pointer;
    user-select: none;
  }
  .drip-step-left { display: flex; align-items: center; gap: 10px; }
  .step-num {
    width: 26px; height: 26px;
    border-radius: 8px;
    background: linear-gradient(135deg, #a78bfa20, #7c3aed20);
    border: 1px solid #a78bfa30;
    color: #a78bfa;
    font-family: 'Syne', sans-serif;
    font-size: 12px;
    font-weight: 700;
    display: flex; align-items: center; justify-content: center;
  }
  .step-num.inactive {
    background: var(--bg3);
    border-color: var(--border);
    color: var(--muted);
  }
  .drip-step-info { display: flex; flex-direction: column; gap: 2px; }
  .drip-step-name { font-size: 13px; font-weight: 500; color: var(--text); }
  .drip-step-meta { font-size: 11px; color: var(--muted); }
  .drip-toggle {
    width: 34px; height: 20px;
    border-radius: 10px;
    background: var(--bg3);
    border: 1px solid var(--border);
    cursor: pointer;
    position: relative;
    transition: background 0.2s;
    flex-shrink: 0;
  }
  .drip-toggle.on { background: linear-gradient(135deg, #a78bfa, #7c3aed); border-color: transparent; }
  .drip-toggle::after {
    content: '';
    position: absolute;
    top: 2px; left: 2px;
    width: 14px; height: 14px;
    border-radius: 50%;
    background: white;
    transition: transform 0.2s;
  }
  .drip-toggle.on::after { transform: translateX(14px); }
  .drip-step-body { padding: 0 14px 14px; border-top: 1px solid var(--border); display: none; }
  .drip-step-body.open { display: block; padding-top: 14px; }
  .chevron { color: var(--muted); font-size: 10px; transition: transform 0.2s; }
  .chevron.open { transform: rotate(180deg); }

  /* ── Form elements ── */
  .field { margin-bottom: 12px; }
  .field label {
    display: block;
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 5px;
    font-weight: 500;
  }
  .field input, .field textarea, .field select {
    width: 100%;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text);
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    padding: 9px 12px;
    outline: none;
    transition: border-color 0.15s;
    appearance: none;
    -webkit-appearance: none;
  }
  .field input:focus, .field textarea:focus, .field select:focus {
    border-color: #a78bfa50;
  }
  .field textarea { resize: none; height: 80px; line-height: 1.5; }
  .field select {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%236b6b80' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    padding-right: 28px;
  }
  .row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }

  /* ── Segment control ── */
  .seg { display: flex; gap: 4px; background: var(--bg3); border-radius: 10px; padding: 3px; margin-bottom: 14px; }
  .seg-btn {
    flex: 1;
    padding: 7px 4px;
    border-radius: 8px;
    border: none;
    background: transparent;
    color: var(--muted);
    font-family: 'DM Sans', sans-serif;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    text-align: center;
  }
  .seg-btn.active {
    background: var(--bg2);
    color: var(--text);
    box-shadow: 0 1px 4px rgba(0,0,0,0.3);
  }

  /* ── Buttons ── */
  .btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 10px 18px;
    border-radius: 10px;
    border: none;
    font-family: 'DM Sans', sans-serif;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
  }
  .btn-primary {
    background: linear-gradient(135deg, #a78bfa, #7c3aed);
    color: white;
  }
  .btn-primary:hover { opacity: 0.9; transform: translateY(-1px); }
  .btn-primary:active { transform: translateY(0); opacity: 1; }
  .btn-ghost {
    background: var(--bg3);
    color: var(--text);
    border: 1px solid var(--border);
  }
  .btn-ghost:hover { border-color: rgba(255,255,255,0.15); }
  .btn-danger { background: #f8717120; color: #f87171; border: 1px solid #f8717130; }
  .btn-danger:hover { background: #f8717130; }
  .btn-block { width: 100%; }
  .btn-sm { padding: 6px 12px; font-size: 12px; }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none !important; }

  /* ── Post rows ── */
  .post-item {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 8px;
    transition: border-color 0.15s;
  }
  .post-item:hover { border-color: rgba(255,255,255,0.1); }
  .post-meta { font-size: 11px; color: var(--muted); margin-bottom: 3px; }
  .post-text { font-size: 13px; color: var(--text); }
  .channel-badge {
    display: inline-block;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 2px 7px;
    border-radius: 5px;
    margin-right: 5px;
  }
  .channel-badge.free { background: #34d39915; color: #34d399; }
  .channel-badge.paid { background: #fbbf2415; color: #fbbf24; }

  /* ── Settings card ── */
  .settings-card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 12px;
  }
  .settings-card-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 12px;
  }

  /* ── Divider ── */
  .divider { height: 1px; background: var(--border); margin: 16px 0; }

  /* ── Loading ── */
  .loading {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 40px 0;
    flex-direction: column;
    gap: 12px;
    color: var(--muted);
    font-size: 13px;
  }
  .spinner {
    width: 24px; height: 24px;
    border: 2px solid var(--bg3);
    border-top-color: #a78bfa;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Empty ── */
  .empty { text-align: center; padding: 32px 0; color: var(--muted); font-size: 13px; }

  /* ── Publish now toggle ── */
  .publish-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--bg3);
    border-radius: 10px;
    padding: 10px 12px;
    margin-bottom: 12px;
  }
  .publish-row-label { font-size: 13px; color: var(--text); }
`;

// ─── Helpers ─────────────────────────────────────────────────────────────────

function inject(style: string) {
  const el = document.createElement("style");
  el.textContent = style;
  document.head.appendChild(el);
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function Spinner() {
  return <div className="loading"><div className="spinner" /><span>Загрузка…</span></div>;
}

function Toast({ msg, type }: { msg: string; type: "ok" | "err" }) {
  return (
    <div className="toast-wrap">
      <div className={`toast ${type}`}>{type === "ok" ? "✓ " : "✕ "}{msg}</div>
    </div>
  );
}

function StatCard({
  label, value, color = "", wide = false, pct,
}: {
  label: string; value: string | number; color?: string; wide?: boolean; pct?: number;
}) {
  const glowColors: Record<string, string> = {
    accent: "#a78bfa", green: "#34d399", amber: "#fbbf24", red: "#f87171",
  };
  return (
    <div className={`stat-card${wide ? " wide" : ""}`}>
      {color && <div className="stat-card-glow" style={{ background: glowColors[color] }} />}
      <div className="stat-label">{label}</div>
      <div className={`stat-val${color ? " " + color : ""}`}>{value}</div>
      {pct !== undefined && (
        <div className="conv-wrap">
          <div className="conv-bar-track">
            <div className="conv-bar-fill" style={{ width: `${Math.min(pct, 100)}%` }} />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── App ─────────────────────────────────────────────────────────────────────

export default function App() {
  const [tab, setTab] = useState<TabId>("dash");
  const [toast, setToast] = useState<{ msg: string; type: "ok" | "err" } | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [stats, setStats] = useState<Stats | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);

  const [drip, setDrip] = useState<DripStep[] | null>(null);
  const [dripLoading, setDripLoading] = useState(false);
  const [openStep, setOpenStep] = useState<number | null>(null);

  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsLoading, setSettingsLoading] = useState(false);

  const [posts, setPosts] = useState<PostRow[] | null>(null);
  const [postsLoading, setPostsLoading] = useState(false);

  // Broadcast
  const [bcText, setBcText] = useState("");
  const [bcMedia, setBcMedia] = useState("");
  const [bcTarget, setBcTarget] = useState<"all" | "free" | "vip">("all");
  const [bcSending, setBcSending] = useState(false);

  // New post
  const [pChannel, setPChannel] = useState<"free" | "paid">("free");
  const [pText, setPText] = useState("");
  const [pMedia, setPMedia] = useState("");
  const [pMediaType, setPMediaType] = useState("photo");
  const [pBtnT, setPBtnT] = useState("");
  const [pBtnU, setPBtnU] = useState("");
  const [pWhen, setPWhen] = useState("");
  const [pNow, setPNow] = useState(false);
  const [pSaving, setPSaving] = useState(false);

  const showToast = (msg: string, type: "ok" | "err" = "ok") => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast({ msg, type });
    toastTimer.current = setTimeout(() => setToast(null), 2800);
  };

  // Telegram theme
  useEffect(() => {
    inject(css);
    const tg = window.Telegram?.WebApp;
    tg?.ready();
    tg?.expand();
    const p = tg?.themeParams;
    if (p?.bg_color) document.documentElement.style.setProperty("--bg", p.bg_color);
  }, []);

  const loadStats = useCallback(async () => {
    setStatsLoading(true);
    try { setStats(await apiGet<Stats>("/api/stats")); }
    catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
    finally { setStatsLoading(false); }
  }, []);

  const loadDrip = useCallback(async () => {
    setDripLoading(true);
    try { setDrip(await apiGet<DripStep[]>("/api/drip")); }
    catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
    finally { setDripLoading(false); }
  }, []);

  const loadSettings = useCallback(async () => {
    setSettingsLoading(true);
    try { setSettings(await apiGet<Settings>("/api/settings")); }
    catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
    finally { setSettingsLoading(false); }
  }, []);

  const loadPosts = useCallback(async () => {
    setPostsLoading(true);
    try { setPosts(await apiGet<PostRow[]>("/api/posts")); }
    catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
    finally { setPostsLoading(false); }
  }, []);

  useEffect(() => {
    if (tab === "dash") loadStats();
    if (tab === "drip") loadDrip();
    if (tab === "set") loadSettings();
    if (tab === "posts") loadPosts();
  }, [tab]);

  const saveDripStep = async (s: DripStep) => {
    try {
      await apiPut<DripStep>(`/api/drip/${s.step}`, {
        text: s.text, media_file_id: s.media_file_id,
        media_type: s.media_type, button_text: s.button_text,
        button_url: s.button_url, delay_hours: s.delay_hours, is_active: s.is_active,
      });
      showToast(`Шаг ${s.step} сохранён`);
      await loadDrip();
    } catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
  };

  const saveSettings = async () => {
    if (!settings) return;
    try {
      const r = await apiPut<Settings>("/api/settings", {
        welcome_text: settings.welcome_text,
        button1_text: settings.button1_text, button1_url: settings.button1_url,
        button2_text: settings.button2_text, button2_url: settings.button2_url,
      });
      setSettings(r);
      showToast("Настройки сохранены");
    } catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
  };

  const sendBroadcast = async () => {
    if (!bcText.trim() && !bcMedia.trim()) { showToast("Нужен текст или медиа", "err"); return; }
    setBcSending(true);
    try {
      const r = await apiPost<{ sent: number; total: number }>("/api/broadcast", {
        text: bcText || null, media_file_id: bcMedia || null, target: bcTarget,
      });
      showToast(`Отправлено: ${r.sent} / ${r.total}`);
      setBcText(""); setBcMedia("");
    } catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
    finally { setBcSending(false); }
  };

  const schedulePost = async () => {
    if (!pNow && !pWhen.trim()) { showToast("Укажите дату или выберите «Сейчас»", "err"); return; }
    if (!pText.trim() && !pMedia.trim()) { showToast("Нужен текст или media_id", "err"); return; }
    setPSaving(true);
    try {
      await apiPost<PostRow>("/api/posts", {
        channel: pChannel,
        text: pText || null,
        media_id: pMedia.trim() || null,
        media_type: pMediaType,
        button_text: pBtnT || null,
        button_url: pBtnU || null,
        scheduled_at: pNow ? null : new Date(pWhen).toISOString(),
        publish_now: pNow,
      });
      setPText(""); setPMedia(""); setPBtnT(""); setPBtnU(""); setPWhen(""); setPNow(false);
      showToast(pNow ? "Пост опубликован!" : "Пост запланирован");
      await loadPosts();
    } catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
    finally { setPSaving(false); }
  };

  const deletePost = async (id: number) => {
    try {
      await apiDelete(`/api/posts/${id}`);
      showToast("Удалено");
      await loadPosts();
    } catch (e) { showToast(e instanceof Error ? e.message : String(e), "err"); }
  };

  const TABS = [
    { id: "dash" as TabId, label: "Обзор", ico: "◈" },
    { id: "drip" as TabId, label: "Drip", ico: "◉" },
    { id: "set" as TabId, label: "Настройки", ico: "◎" },
    { id: "bc" as TabId, label: "Рассылка", ico: "◐" },
    { id: "posts" as TabId, label: "Посты", ico: "◑" },
  ];

  return (
    <div className="app">
      {toast && <Toast msg={toast.msg} type={toast.type} />}

      {/* Header */}
      <div className="header">
        <div className="header-inner">
          <div className="header-title">Aura</div>
          <div className="header-badge">ADMIN</div>
        </div>
        <div className="header-sub">{
          tab === "dash" ? "Статистика канала" :
          tab === "drip" ? "Цепочка сообщений" :
          tab === "set" ? "Параметры бота" :
          tab === "bc" ? "Массовая рассылка" :
          "Контент-план"
        }</div>
      </div>

      {/* Tabs */}
      <div className="tabs">
        {TABS.map(t => (
          <button key={t.id} className={`tab${tab === t.id ? " active" : ""}`} onClick={() => setTab(t.id)}>
            <span className="tab-ico">{t.ico}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="content">

        {/* ── Dashboard ── */}
        {tab === "dash" && (
          <>
            {statsLoading ? <Spinner /> : stats ? (
              <>
                <div className="stat-grid">
                  <StatCard label="Подписчики" value={stats.total_users.toLocaleString()} />
                  <StatCard label="VIP" value={stats.vip_count.toLocaleString()} color="accent" />
                  <StatCard label="В drip" value={stats.in_drip.toLocaleString()} color="green" />
                  <StatCard label="Отписались" value={stats.blocked_count.toLocaleString()} color="red" />
                  <StatCard label="Конверсия в VIP" value={`${stats.conversion.toFixed(1)}%`}
                    color="amber" wide pct={stats.conversion} />
                </div>
                <button className="btn btn-ghost btn-block btn-sm" onClick={loadStats}>
                  ↻ Обновить
                </button>
              </>
            ) : null}
          </>
        )}

        {/* ── Drip ── */}
        {tab === "drip" && (
          <>
            {dripLoading ? <Spinner /> : drip ? (
              <>
                <div className="section-head">
                  <div className="section-title">Цепочка</div>
                  <div className="section-count">{drip.filter(s => s.is_active).length} активных</div>
                </div>
                {drip.map(s => (
                  <div className="drip-step" key={s.step}>
                    <div className="drip-step-head" onClick={() => setOpenStep(openStep === s.step ? null : s.step)}>
                      <div className="drip-step-left">
                        <div className={`step-num${s.is_active ? "" : " inactive"}`}>{s.step}</div>
                        <div className="drip-step-info">
                          <div className="drip-step-name">
                            {s.text ? s.text.slice(0, 28) + (s.text.length > 28 ? "…" : "") : "Не настроен"}
                          </div>
                          <div className="drip-step-meta">
                            {s.delay_hours}ч · {s.media_type !== "none" ? s.media_type : "текст"}
                          </div>
                        </div>
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <div
                          className={`drip-toggle${s.is_active ? " on" : ""}`}
                          onClick={e => {
                            e.stopPropagation();
                            const updated = { ...s, is_active: !s.is_active };
                            setDrip(prev => prev?.map(x => x.step === s.step ? updated : x) ?? null);
                            saveDripStep(updated);
                          }}
                        />
                        <div className={`chevron${openStep === s.step ? " open" : ""}`}>▼</div>
                      </div>
                    </div>

                    <div className={`drip-step-body${openStep === s.step ? " open" : ""}`}>
                      <div className="field">
                        <label>Текст</label>
                        <textarea value={s.text ?? ""} onChange={e =>
                          setDrip(prev => prev?.map(x => x.step === s.step ? { ...x, text: e.target.value } : x) ?? null)
                        } />
                      </div>
                      <div className="field">
                        <label>media_file_id</label>
                        <input value={s.media_file_id ?? ""} onChange={e =>
                          setDrip(prev => prev?.map(x => x.step === s.step ? { ...x, media_file_id: e.target.value } : x) ?? null)
                        } />
                      </div>
                      <div className="row2">
                        <div className="field">
                          <label>Тип медиа</label>
                          <select value={s.media_type} onChange={e =>
                            setDrip(prev => prev?.map(x => x.step === s.step ? { ...x, media_type: e.target.value } : x) ?? null)
                          }>
                            <option value="none">Нет</option>
                            <option value="photo">Фото</option>
                            <option value="video">Видео</option>
                            <option value="voice">Голосовое</option>
                          </select>
                        </div>
                        <div className="field">
                          <label>Задержка (ч)</label>
                          <input type="number" min={0} value={s.delay_hours} onChange={e =>
                            setDrip(prev => prev?.map(x => x.step === s.step ? { ...x, delay_hours: Number(e.target.value) || 0 } : x) ?? null)
                          } />
                        </div>
                      </div>
                      <div className="row2">
                        <div className="field">
                          <label>Кнопка — текст</label>
                          <input value={s.button_text ?? ""} onChange={e =>
                            setDrip(prev => prev?.map(x => x.step === s.step ? { ...x, button_text: e.target.value } : x) ?? null)
                          } />
                        </div>
                        <div className="field">
                          <label>Кнопка — URL</label>
                          <input value={s.button_url ?? ""} onChange={e =>
                            setDrip(prev => prev?.map(x => x.step === s.step ? { ...x, button_url: e.target.value } : x) ?? null)
                          } />
                        </div>
                      </div>
                      <button className="btn btn-primary btn-block" onClick={() => saveDripStep(s)}>
                        Сохранить шаг {s.step}
                      </button>
                    </div>
                  </div>
                ))}
              </>
            ) : null}
          </>
        )}

        {/* ── Settings ── */}
        {tab === "set" && (
          <>
            {settingsLoading ? <Spinner /> : settings ? (
              <>
                <div className="settings-card">
                  <div className="settings-card-title">Welcome сообщение</div>
                  <div className="field">
                    <label>Текст</label>
                    <textarea value={settings.welcome_text} onChange={e =>
                      setSettings({ ...settings, welcome_text: e.target.value })
                    } style={{ height: 100 }} />
                  </div>
                </div>
                <div className="settings-card">
                  <div className="settings-card-title">Кнопка 1</div>
                  <div className="row2">
                    <div className="field">
                      <label>Текст</label>
                      <input value={settings.button1_text} onChange={e =>
                        setSettings({ ...settings, button1_text: e.target.value })
                      } />
                    </div>
                    <div className="field">
                      <label>URL</label>
                      <input value={settings.button1_url} onChange={e =>
                        setSettings({ ...settings, button1_url: e.target.value })
                      } />
                    </div>
                  </div>
                </div>
                <div className="settings-card">
                  <div className="settings-card-title">Кнопка 2 (VIP)</div>
                  <div className="row2">
                    <div className="field">
                      <label>Текст</label>
                      <input value={settings.button2_text} onChange={e =>
                        setSettings({ ...settings, button2_text: e.target.value })
                      } />
                    </div>
                    <div className="field">
                      <label>URL</label>
                      <input value={settings.button2_url} onChange={e =>
                        setSettings({ ...settings, button2_url: e.target.value })
                      } />
                    </div>
                  </div>
                </div>
                <div className="settings-card">
                  <div className="settings-card-title">Paid канал</div>
                  <div className="field">
                    <label>Ссылка (только чтение)</label>
                    <input value={settings.paid_channel_link} readOnly style={{ opacity: 0.5 }} />
                  </div>
                </div>
                <button className="btn btn-primary btn-block" onClick={saveSettings}>
                  Сохранить настройки
                </button>
              </>
            ) : null}
          </>
        )}

        {/* ── Broadcast ── */}
        {tab === "bc" && (
          <>
            <div className="section-head" style={{ marginTop: 0 }}>
              <div className="section-title">Аудитория</div>
            </div>
            <div className="seg" style={{ marginBottom: 16 }}>
              {(["all", "free", "vip"] as const).map(t => (
                <button key={t} className={`seg-btn${bcTarget === t ? " active" : ""}`} onClick={() => setBcTarget(t)}>
                  {t === "all" ? "Все" : t === "free" ? "Free" : "VIP"}
                </button>
              ))}
            </div>
            <div className="settings-card">
              <div className="settings-card-title">Сообщение</div>
              <div className="field">
                <label>Текст</label>
                <textarea value={bcText} onChange={e => setBcText(e.target.value)}
                  style={{ height: 100 }} placeholder="Введите текст рассылки…" />
              </div>
              <div className="field">
                <label>media_file_id (опционально)</label>
                <input value={bcMedia} onChange={e => setBcMedia(e.target.value)}
                  placeholder="AgACAgI…" />
              </div>
            </div>
            <button className="btn btn-primary btn-block" onClick={sendBroadcast} disabled={bcSending}>
              {bcSending ? "Отправка…" : "Отправить рассылку"}
            </button>
          </>
        )}

        {/* ── Posts ── */}
        {tab === "posts" && (
          <>
            {postsLoading ? <Spinner /> : (
              <>
                <div className="section-head" style={{ marginTop: 0 }}>
                  <div className="section-title">Запланированные</div>
                  {posts && <div className="section-count">{posts.length}</div>}
                </div>
                {posts?.length ? posts.map(p => (
                  <div className="post-item" key={p.id}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="post-meta">
                        <span className={`channel-badge ${p.channel}`}>{p.channel}</span>
                        {fmtDate(p.scheduled_at)}
                      </div>
                      <div className="post-text" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {p.text ?? (p.media_id ? "📎 Медиа" : "—")}
                      </div>
                    </div>
                    <button className="btn btn-danger btn-sm" onClick={() => deletePost(p.id)}>✕</button>
                  </div>
                )) : <div className="empty">Нет запланированных постов</div>}

                <div className="divider" />

                <div className="section-title" style={{ marginBottom: 12 }}>Новый пост</div>
                <div className="settings-card">
                  <div className="settings-card-title">Канал</div>
                  <div className="seg">
                    {(["free", "paid"] as const).map(c => (
                      <button key={c} className={`seg-btn${pChannel === c ? " active" : ""}`} onClick={() => setPChannel(c)}>
                        {c === "free" ? "🆓 Free" : "👑 Paid"}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="settings-card">
                  <div className="settings-card-title">Контент</div>
                  <div className="field">
                    <label>Текст поста</label>
                    <textarea value={pText} onChange={e => setPText(e.target.value)}
                      placeholder="Текст поста…" />
                  </div>
                  <div className="row2">
                    <div className="field">
                      <label>media_id</label>
                      <input value={pMedia} onChange={e => setPMedia(e.target.value)} placeholder="AgACAgI…" />
                    </div>
                    <div className="field">
                      <label>Тип медиа</label>
                      <select value={pMediaType} onChange={e => setPMediaType(e.target.value)}>
                        <option value="photo">Фото</option>
                        <option value="video">Видео</option>
                        <option value="voice">Голосовое</option>
                      </select>
                    </div>
                  </div>
                  <div className="row2">
                    <div className="field">
                      <label>Кнопка — текст</label>
                      <input value={pBtnT} onChange={e => setPBtnT(e.target.value)} />
                    </div>
                    <div className="field">
                      <label>Кнопка — URL</label>
                      <input value={pBtnU} onChange={e => setPBtnU(e.target.value)} />
                    </div>
                  </div>
                </div>
                <div className="settings-card">
                  <div className="settings-card-title">Время публикации</div>
                  <div className="publish-row">
                    <div className="publish-row-label">Опубликовать сейчас</div>
                    <div className={`drip-toggle${pNow ? " on" : ""}`} onClick={() => setPNow(!pNow)} />
                  </div>
                  {!pNow && (
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Дата и время</label>
                      <input type="datetime-local" value={pWhen} onChange={e => setPWhen(e.target.value)} />
                    </div>
                  )}
                </div>
                <button className="btn btn-primary btn-block" onClick={schedulePost} disabled={pSaving}>
                  {pSaving ? "Сохранение…" : pNow ? "⚡ Опубликовать сейчас" : "📅 Запланировать пост"}
                </button>
              </>
            )}
          </>
        )}

      </div>
    </div>
  );
}
