// HTTP client for the SWF API (m10a §2). Same origin on localhost (Vite proxies /v1).

export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    readonly retryAfter: number | null,
    readonly field: string | null = null,
  ) {
    super(code);
  }
}

let tokenSource: () => string | null = () => null;
let onUnauthorized: () => void = () => {};

export function configureApi(getToken: () => string | null, unauthorized: () => void): void {
  tokenSource = getToken;
  onUnauthorized = unauthorized;
}

export function notifyUnauthorized(): void {
  onUnauthorized();
}

export function authHeaders(): Record<string, string> {
  const token = tokenSource();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function codeOf(body: unknown): { code: string; field: string | null } {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return { code: detail, field: null };
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown[] };
      const loc = Array.isArray(first.loc) ? first.loc : [];
      const field = loc.length > 0 ? String(loc[loc.length - 1]) : null;
      return { code: "invalid_input", field };
    }
  }
  return { code: "unknown_error", field: null };
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { ...authHeaders() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp: Response;
  try {
    resp = await fetch(API_BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network_error", null);
  }
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const { code, field } = codeOf(data);
    const retry = resp.headers.get("Retry-After");
    const err = new ApiError(resp.status, code, retry ? Number(retry) : null, field);
    if (resp.status === 401) onUnauthorized();
    throw err;
  }
  return data as T;
}

// --- API shapes ---------------------------------------------------------------------------

export interface Me {
  user_id: string;
  github_login: string | null;
  is_admin: boolean;
  github_linked: boolean;
  usage_today: { day: string; tasks: number; cost_usd: number };
  limits: {
    tasks_per_day: number;
    cost_per_day_usd: number;
    cost_per_task_usd: number;
    concurrent_tasks: number;
  };
  remaining: { tasks_today: number; cost_today_usd: number; concurrent_tasks: number };
}

export interface Repo {
  full_name: string;
  default_branch: string;
  private: boolean;
  can_push: boolean;
}

export interface PreflightWarning {
  code: string;
  message: string;
}

export interface Preflight {
  decision: "admit" | "queue" | "reject";
  image: string;
  memory_mb: number;
  cpus: number;
  time_limit_minutes: number;
  queue_position: number | null;
  warnings: PreflightWarning[];
}

export interface Task {
  id: string;
  repo: string;
  base_branch: string;
  description: string;
  failing_test: string | null;
  status: string;
  phase?: string | null;
  error?: string | null;
  pr_url?: string | null;
  worker_id?: string | null;
  cost_usd?: number;
  created_at: string;
}

export interface EvidenceItem {
  id: number | string;
  kind: string;
  source: string;
  exit_code: number | null;
  command?: string | null;
  created_at?: string;
}

export interface AdminUser {
  user_id: string;
  github_login: string | null;
  is_admin: boolean;
  disabled: boolean;
  open_tasks: number;
}

export interface SystemStatus {
  tasks_paused: boolean;
  updated_at: string | null;
  updated_by: string | null;
  queued: number;
  active: number;
  users: AdminUser[];
}

export const TERMINAL = new Set(["completed", "rejected", "failed", "interrupted", "cancelled"]);
