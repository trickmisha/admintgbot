import { useCallback, useEffect, useState } from "react";
import { apiDelete, apiGet, apiPost, apiPut } from "./api";

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

const TABS = [
  { id: "dash", label: "Dash", ico: "📊" },
  { id: "drip", label: "Drip", ico: "💧" },
  { id: "set", label: "Настр.", ico: "⚙️" },
  { id: "bc", label: "Рассылка", ico: "📢" },
  { id: "posts", label: "Посты", ico: "📅" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function fromDatetimeLocalValue(v: string): string {
  const d = new Date(v);
  return d.toISOString();
}

export default function App() {
  const [tab, setTab] = useState<TabId>("dash");
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const [stats, setStats] = useState<Stats | null>(null);
  const [drip, setDrip] = useState<DripStep[] | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [posts, setPosts] = useState<PostRow[] | null>(null);

  const [bcText, setBcText] = useState("");
  const [bcMedia, setBcMedia] = useState("");
  const [bcTarget, setBcTarget] = useState<"all" | "free" | "vip">("all");

  const [pChannel, setPChannel] = useState<"free" | "paid">("free");
  const [pText, setPText] = useState("");
  const [pMedia, setPMedia] = useState("");
  const [pBtnT, setPBtnT] = useState("");
  const [pBtnU, setPBtnU] = useState("");
  const [pWhen, setPWhen] = useState("");

  const showOk = (m: string) => {
    setOk(m);
    setErr(null);
    setTimeout(() => setOk(null), 2500);
  };

  const loadStats = useCallback(async () => {
    const s = await apiGet<Stats>("/api/stats");
    setStats(s);
  }, []);

  const loadDrip = useCallback(async () => {
    const d = await apiGet<DripStep[]>("/api/drip");
    setDrip(d);
  }, []);

  const loadSettings = useCallback(async () => {
    const s = await apiGet<Settings>("/api/settings");
    setSettings(s);
  }, []);

  const loadPosts = useCallback(async () => {
    const p = await apiGet<PostRow[]>("/api/posts");
    setPosts(p);
  }, []);

  useEffect(() => {
    const tg = window.Telegram?.WebApp;
    tg?.ready();
    tg?.expand();
    const p = tg?.themeParams;
    const root = document.documentElement;
    if (p?.bg_color) root.style.setProperty("--bg", p.bg_color);
    if (p?.secondary_bg_color)
      root.style.setProperty("--bg2", p.secondary_bg_color);
    if (p?.text_color) root.style.setProperty("--text", p.text_color);
    if (p?.hint_color) root.style.setProperty("--muted", p.hint_color);
    if (p?.link_color) root.style.setProperty("--accent", p.link_color);
  }, []);

  useEffect(() => {
    setErr(null);
    (async () => {
      try {
        if (tab === "dash") await loadStats();
        if (tab === "drip") await loadDrip();
        if (tab === "set") await loadSettings();
        if (tab === "posts") await loadPosts();
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [tab, loadStats, loadDrip, loadSettings, loadPosts]);

  const saveDripStep = async (s: DripStep) => {
    try {
      await apiPut<DripStep>(`/api/drip/${s.step}`, {
        text: s.text,
        media_file_id: s.media_file_id,
        media_type: s.media_type,
        button_text: s.button_text,
        button_url: s.button_url,
        delay_hours: s.delay_hours,
        is_active: s.is_active,
      });
      showOk(`Шаг ${s.step} сохранён`);
      await loadDrip();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const saveSettings = async () => {
    if (!settings) return;
    try {
      const r = await apiPut<Settings>("/api/settings", {
        welcome_text: settings.welcome_text,
        button1_text: settings.button1_text,
        button1_url: settings.button1_url,
        button2_text: settings.button2_text,
        button2_url: settings.button2_url,
      });
      setSettings(r);
      showOk("Настройки сохранены");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const sendBroadcast = async () => {
    try {
      const r = await apiPost<{ sent: number; total: number }>("/api/broadcast", {
        text: bcText || null,
        media_file_id: bcMedia || null,
        target: bcTarget,
      });
      showOk(`Отправлено: ${r.sent} / ${r.total}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const schedulePost = async () => {
    if (!pMedia.trim() || !pWhen.trim()) {
      setErr("Нужны media_id и дата/время");
      return;
    }
    try {
      await apiPost<PostRow>("/api/posts", {
        channel: pChannel,
        text: pText || null,
        media_id: pMedia.trim(),
        button_text: pBtnT || null,
        button_url: pBtnU || null,
        scheduled_at: fromDatetimeLocalValue(pWhen),
      });
      setPText("");
      setPMedia("");
      setPBtnT("");
      setPBtnU("");
      setPWhen("");
      showOk("Пост запланирован");
      await loadPosts();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const deletePost = async (id: number) => {
    try {
      await apiDelete(`/api/posts/${id}`);
      showOk("Удалено");
      await loadPosts();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="app">
      <div className="panel">
        {err ? <div className="err">{err}</div> : null}
        {ok ? <div className="ok">{ok}</div> : null}

        {tab === "dash" ? (
          <>
            <h1>Dashboard</h1>
            {stats ? (
              <div className="cards">
                <div className="card">
                  <div className="muted">Всего</div>
                  <div className="v">{stats.total_users}</div>
                </div>
                <div className="card">
                  <div className="muted">VIP</div>
                  <div className="v">{stats.vip_count}</div>
                </div>
                <div className="card">
                  <div className="muted">В drip</div>
                  <div className="v">{stats.in_drip}</div>
                </div>
                <div className="card">
                  <div className="muted">Blocked</div>
                  <div className="v">{stats.blocked_count}</div>
                </div>
                <div className="card" style={{ gridColumn: "span 2" }}>
                  <div className="muted">Конверсия</div>
                  <div className="v">{stats.conversion}%</div>
                </div>
              </div>
            ) : (
              <p>Загрузка…</p>
            )}
          </>
        ) : null}

        {tab === "drip" ? (
          <>
            <h1>Drip (0–4)</h1>
            <div className="stack">
              {drip?.map((s) => (
                <div className="step-card" key={s.step}>
                  <div className="step-title">Шаг {s.step}</div>
                  <label>Текст</label>
                  <textarea
                    value={s.text ?? ""}
                    onChange={(e) =>
                      setDrip((prev) =>
                        prev?.map((x) =>
                          x.step === s.step ? { ...x, text: e.target.value } : x
                        ) ?? null
                      )
                    }
                  />
                  <label>media_file_id</label>
                  <input
                    value={s.media_file_id ?? ""}
                    onChange={(e) =>
                      setDrip((prev) =>
                        prev?.map((x) =>
                          x.step === s.step
                            ? { ...x, media_file_id: e.target.value }
                            : x
                        ) ?? null
                      )
                    }
                  />
                  <label>Тип медиа</label>
                  <select
                    value={s.media_type}
                    onChange={(e) =>
                      setDrip((prev) =>
                        prev?.map((x) =>
                          x.step === s.step
                            ? { ...x, media_type: e.target.value }
                            : x
                        ) ?? null
                      )
                    }
                  >
                    <option value="none">none</option>
                    <option value="photo">photo</option>
                    <option value="video">video</option>
                  </select>
                  <div className="row2">
                    <div>
                      <label>Кнопка (текст)</label>
                      <input
                        value={s.button_text ?? ""}
                        onChange={(e) =>
                          setDrip((prev) =>
                            prev?.map((x) =>
                              x.step === s.step
                                ? { ...x, button_text: e.target.value }
                                : x
                            ) ?? null
                          )
                        }
                      />
                    </div>
                    <div>
                      <label>Кнопка (url)</label>
                      <input
                        value={s.button_url ?? ""}
                        onChange={(e) =>
                          setDrip((prev) =>
                            prev?.map((x) =>
                              x.step === s.step
                                ? { ...x, button_url: e.target.value }
                                : x
                            ) ?? null
                          )
                        }
                      />
                    </div>
                  </div>
                  <label>Задержка (часы)</label>
                  <input
                    type="number"
                    min={0}
                    value={s.delay_hours}
                    onChange={(e) =>
                      setDrip((prev) =>
                        prev?.map((x) =>
                          x.step === s.step
                            ? {
                                ...x,
                                delay_hours: Number(e.target.value) || 0,
                              }
                            : x
                        ) ?? null
                      )
                    }
                  />
                  <div className="chk">
                    <input
                      type="checkbox"
                      checked={s.is_active}
                      onChange={(e) =>
                        setDrip((prev) =>
                          prev?.map((x) =>
                            x.step === s.step
                              ? { ...x, is_active: e.target.checked }
                              : x
                          ) ?? null
                        )
                      }
                    />
                    <span>Активен</span>
                  </div>
                  <button
                    type="button"
                    className="btn btn-primary btn-block"
                    onClick={() => void saveDripStep(s)}
                  >
                    Сохранить
                  </button>
                </div>
              ))}
            </div>
          </>
        ) : null}

        {tab === "set" ? (
          <>
            <h1>Настройки</h1>
            {settings ? (
              <div className="stack">
                <div>
                  <label>Welcome-текст</label>
                  <textarea
                    value={settings.welcome_text}
                    onChange={(e) =>
                      setSettings({ ...settings, welcome_text: e.target.value })
                    }
                  />
                </div>
                <div className="row2">
                  <div>
                    <label>Кнопка 1 — текст</label>
                    <input
                      value={settings.button1_text}
                      onChange={(e) =>
                        setSettings({ ...settings, button1_text: e.target.value })
                      }
                    />
                  </div>
                  <div>
                    <label>Кнопка 1 — URL</label>
                    <input
                      value={settings.button1_url}
                      onChange={(e) =>
                        setSettings({ ...settings, button1_url: e.target.value })
                      }
                    />
                  </div>
                </div>
                <div className="row2">
                  <div>
                    <label>Кнопка 2 — текст</label>
                    <input
                      value={settings.button2_text}
                      onChange={(e) =>
                        setSettings({ ...settings, button2_text: e.target.value })
                      }
                    />
                  </div>
                  <div>
                    <label>Кнопка 2 — URL (хранится в БД)</label>
                    <input
                      value={settings.button2_url}
                      onChange={(e) =>
                        setSettings({ ...settings, button2_url: e.target.value })
                      }
                    />
                  </div>
                </div>
                <div>
                  <label>paid_channel_link (в боте Welcome URL кнопки 2)</label>
                  <input value={settings.paid_channel_link} readOnly />
                </div>
                <button
                  type="button"
                  className="btn btn-primary btn-block"
                  onClick={() => void saveSettings()}
                >
                  Сохранить
                </button>
              </div>
            ) : (
              <p>Загрузка…</p>
            )}
          </>
        ) : null}

        {tab === "bc" ? (
          <>
            <h1>Рассылка</h1>
            <label>Текст</label>
            <textarea value={bcText} onChange={(e) => setBcText(e.target.value)} />
            <label>media_file_id (опционально)</label>
            <input value={bcMedia} onChange={(e) => setBcMedia(e.target.value)} />
            <label>Аудитория</label>
            <select
              value={bcTarget}
              onChange={(e) =>
                setBcTarget(e.target.value as "all" | "free" | "vip")
              }
            >
              <option value="all">Все</option>
              <option value="free">Free</option>
              <option value="vip">VIP</option>
            </select>
            <button
              type="button"
              className="btn btn-primary btn-block"
              onClick={() => void sendBroadcast()}
            >
              Отправить
            </button>
          </>
        ) : null}

        {tab === "posts" ? (
          <>
            <h1>Посты</h1>
            <h2>Запланированные</h2>
            <div className="stack">
              {posts?.length ? (
                posts.map((p) => (
                  <div className="post-row" key={p.id}>
                    <div>
                      <strong>#{p.id}</strong> · {p.channel}
                      <div style={{ fontSize: "0.8rem", color: "var(--muted)" }}>
                        {p.scheduled_at
                          ? new Date(p.scheduled_at).toLocaleString()
                          : "—"}
                      </div>
                      <div style={{ fontSize: "0.8rem" }}>{p.text ?? ""}</div>
                    </div>
                    <button
                      type="button"
                      className="btn btn-danger"
                      onClick={() => void deletePost(p.id)}
                    >
                      Удалить
                    </button>
                  </div>
                ))
              ) : (
                <p style={{ color: "var(--muted)" }}>Пусто</p>
              )}
            </div>

            <h2 style={{ marginTop: 18 }}>Новый пост</h2>
            <label>Канал</label>
            <select
              value={pChannel}
              onChange={(e) => setPChannel(e.target.value as "free" | "paid")}
            >
              <option value="free">free</option>
              <option value="paid">paid</option>
            </select>
            <label>Текст</label>
            <textarea value={pText} onChange={(e) => setPText(e.target.value)} />
            <label>media_id</label>
            <input value={pMedia} onChange={(e) => setPMedia(e.target.value)} />
            <div className="row2">
              <div>
                <label>Кнопка — текст</label>
                <input value={pBtnT} onChange={(e) => setPBtnT(e.target.value)} />
              </div>
              <div>
                <label>Кнопка — URL</label>
                <input value={pBtnU} onChange={(e) => setPBtnU(e.target.value)} />
              </div>
            </div>
            <label>Дата и время</label>
            <input
              type="datetime-local"
              value={pWhen}
              onChange={(e) => setPWhen(e.target.value)}
            />
            <button
              type="button"
              className="btn btn-primary btn-block"
              onClick={() => void schedulePost()}
            >
              Запланировать
            </button>
          </>
        ) : null}
      </div>

      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`tab ${tab === t.id ? "active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            <span className="tab-ico">{t.ico}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
