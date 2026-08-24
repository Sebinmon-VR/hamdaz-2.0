/**
 * The API client.
 *
 * One place that talks to the backend. Two behaviours matter:
 *
 * 1. **Problem documents are preserved.** The backend returns RFC 7807 with a correlation ID
 *    and, for a 403, the exact permission that was missing. Throwing away that detail in
 *    favour of "Request failed" is how a system becomes unsupportable.
 * 2. **Server and browser both work.** In a server component there is no ambient cookie jar,
 *    so the caller forwards headers explicitly.
 */

import type { ProblemDetail } from "./types";

const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetail | null;

  constructor(status: number, problem: ProblemDetail | null, fallback: string) {
    super(problem?.detail ?? fallback);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  /** True when the user is signed out — the caller should send them to /login. */
  get isUnauthenticated(): boolean {
    return this.status === 401;
  }

  get isForbidden(): boolean {
    return this.status === 403;
  }

  /** The permission the backend wanted, when it told us. */
  get missingPermission(): string | null {
    return this.problem?.permission ?? null;
  }

  get correlationId(): string | null {
    return this.problem?.correlation_id ?? null;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Forwarded from a server component so the session cookie reaches the backend. */
  headers?: Record<string, string>;
  /** Absolute origin, required on the server where relative URLs have no base. */
  baseUrl?: string;
  signal?: AbortSignal;
  cache?: RequestCache;
}

function resolveUrl(path: string, baseUrl?: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  const full = `${API_PREFIX}${suffix}`;

  if (baseUrl) return `${baseUrl.replace(/\/$/, "")}${full}`;
  if (typeof window === "undefined") {
    // Server component with no explicit base: talk to the backend directly.
    const backend = process.env.BACKEND_URL ?? "http://localhost:8000";
    return `${backend}${full}`;
  }
  return full;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, headers = {}, baseUrl, signal, cache } = options;

  const response = await fetch(resolveUrl(path, baseUrl), {
    method,
    // Cookie auth: without this the session never leaves the browser.
    credentials: "include",
    signal,
    // An ERP dashboard showing yesterday's numbers is worse than a slow one.
    cache: cache ?? "no-store",
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload: unknown = text ? safeParse(text) : null;

  if (!response.ok) {
    throw new ApiError(
      response.status,
      isProblem(payload) ? payload : null,
      `${method} ${path} failed with ${response.status}`,
    );
  }

  return payload as T;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function isProblem(value: unknown): value is ProblemDetail {
  return typeof value === "object" && value !== null && "title" in value && "status" in value;
}

export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "GET" }),
  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    request<T>(path, { ...options, method: "POST", body }),
  put: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    request<T>(path, { ...options, method: "PUT", body }),
  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    request<T>(path, { ...options, method: "PATCH", body }),
  delete: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method">) =>
    request<T>(path, { ...options, method: "DELETE", body }),
};

/**
 * Build a query string, dropping empty values.
 *
 * `?status=` reaches FastAPI as an empty string and fails enum validation, which is a
 * confusing 422 for what is really "no filter selected".
 */
export function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const encoded = search.toString();
  return encoded ? `?${encoded}` : "";
}
