/** Thin fetch wrapper: same-origin cookies, CSRF header, idempotency keys, typed errors. */
import { withBase } from "./base";

export class ApiError extends Error {
  code: string;
  status: number;
  data: Record<string, any>;
  constructor(status: number, code: string, message: string, data: Record<string, any> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

let csrfToken = "";
export function setCsrf(token: string) {
  csrfToken = token;
}

function idempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID().replace(/-/g, "");
  return Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

type Method = "GET" | "POST" | "PUT" | "DELETE";

export interface RequestOptions {
  idempotent?: boolean;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | string[] | undefined | null>;
  /** Milliseconds before the request is abandoned. 0 disables the deadline. */
  timeout?: number;
}

/**
 * Every request gets a deadline.
 *
 * `fetch` has none of its own: a proxy that accepts the connection and then
 * says nothing leaves the promise pending for as long as the tab is open. The
 * app boots by awaiting two of these, so one stalled response used to leave
 * players on the loading spinner with no way forward — the login screen never
 * appeared at all.
 */
const DEFAULT_TIMEOUT = 20000;

function deadline(ms: number, outer?: AbortSignal): { signal: AbortSignal; done: () => void; timedOut: () => boolean } {
  const ctl = new AbortController();
  let expired = false;
  const timer = ms > 0 ? window.setTimeout(() => { expired = true; ctl.abort(); }, ms) : 0;
  const relay = () => ctl.abort();
  outer?.addEventListener("abort", relay);
  return {
    signal: ctl.signal,
    done: () => { if (timer) window.clearTimeout(timer); outer?.removeEventListener("abort", relay); },
    timedOut: () => expired,
  };
}

function buildUrl(path: string, query?: RequestOptions["query"]) {
  path = withBase(path);
  if (!query) return path;
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
    else p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `${path}?${s}` : path;
}

export async function api<T = any>(method: Method, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (opts.idempotent) headers["Idempotency-Key"] = idempotencyKey();
  let res: Response;
  let text: string;
  // The deadline has to cover reading the body too: a proxy can send headers
  // and then stall, which leaves res.text() pending just as long as fetch would.
  const limit = deadline(opts.timeout ?? DEFAULT_TIMEOUT, opts.signal);
  try {
    res = await fetch(buildUrl(path, opts.query), {
      method,
      headers,
      credentials: "same-origin",
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: limit.signal,
    });
    text = await res.text();
  } catch (e) {
    if (limit.timedOut()) throw new ApiError(0, "timeout", "サーバーの応答がありません。しばらくしてからお試しください。");
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "network", "サーバーに接続できません。通信状況を確認してください。");
  } finally {
    limit.done();
  }
  let data: any = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const err = data?.error ?? {};
    const message = err.message || `エラーが発生しました (${res.status})`;
    throw new ApiError(res.status, err.code || "http_error", message, { ...(err.data || {}), fields: err.fields, error_id: err.error_id });
  }
  return data as T;
}

export const get = <T = any>(path: string, query?: RequestOptions["query"], signal?: AbortSignal, timeout?: number) =>
  api<T>("GET", path, undefined, { query, signal, timeout });
export const post = <T = any>(path: string, body?: unknown, idempotent = false) => api<T>("POST", path, body ?? {}, { idempotent });
export const put = <T = any>(path: string, body?: unknown) => api<T>("PUT", path, body ?? {});
export const del = <T = any>(path: string, query?: RequestOptions["query"]) => api<T>("DELETE", path, undefined, { query });
