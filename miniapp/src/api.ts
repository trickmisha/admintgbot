function baseUrl(): string {
  const b = import.meta.env.VITE_API_URL;
  if (!b) throw new Error("VITE_API_URL is not set");
  return b.replace(/\/$/, "");
}

function authHeaders(): HeadersInit {
  const initData = window.Telegram?.WebApp?.initData ?? "";
  return { Authorization: `tma ${initData}` };
}

function authHeadersJson(): HeadersInit {
  return { ...authHeaders(), "Content-Type": "application/json" };
}

async function parseError(res: Response): Promise<string> {
  try {
    const j = await res.json();
    return typeof j?.detail === "string" ? j.detail : JSON.stringify(j);
  } catch {
    return await res.text();
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const r = await fetch(`${baseUrl()}${path}`, { headers: authHeaders() });
  if (!r.ok) throw new Error(await parseError(r));
  return r.json() as Promise<T>;
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${baseUrl()}${path}`, {
    method: "PUT",
    headers: authHeadersJson(),
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await parseError(r));
  return r.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${baseUrl()}${path}`, {
    method: "POST",
    headers: authHeadersJson(),
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await parseError(r));
  return r.json() as Promise<T>;
}

export async function apiDelete(path: string): Promise<void> {
  const r = await fetch(`${baseUrl()}${path}`, {
    method: "DELETE",
    headers: authHeaders(),
  });
  if (!r.ok) throw new Error(await parseError(r));
}
