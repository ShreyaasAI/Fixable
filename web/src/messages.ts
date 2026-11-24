// API error code -> user text (m10a §2). Unknown codes fall back to the code itself.

import { ApiError } from "./api";

const TEXT: Record<string, string> = {
  invalid_token: "Your session has expired. Sign in again.",
  account_disabled: "Your account is disabled. Contact an admin.",
  admin_only: "Admins only.",
  rate_limited: "Too many requests.",
  service_paused: "The service is paused by an admin. Try again later.",
  quota_tasks_per_day: "You have used today's task quota.",
  quota_cost_per_day: "You have used today's cost quota.",
  quota_concurrent_tasks: "You have too many tasks running. Wait for one to finish.",
  github_token_missing: "GitHub is not linked. Sign in again with GitHub.",
  github_token_invalid: "GitHub rejected the token. Sign in again with GitHub.",
  github_account_mismatch: "The GitHub account does not match the signed-in account.",
  github_unavailable: "GitHub is not reachable right now. Try again.",
  repo_not_found: "Repository not found, or you have no access to it.",
  repo_not_writable: "You have no push access to this repository.",
  branch_not_found: "Branch not found in this repository.",
  task_finished: "The task has already finished.",
  task_not_finished: "Only a finished task can be deleted.",
  task_busy: "Finishing up; try again in a moment.",
  already_decided: "Already decided.",
  already_answered: "Already answered.",
  cannot_kill_self: "You cannot kill your own account.",
  not_found: "Not found.",
  invalid_input: "Check the highlighted field.",
  invalid_last_event_id: "The event stream could not resume.",
  network_error: "The API is not reachable.",
  unknown_error: "Something went wrong.",
};

/** Field a pre-flight/create error belongs to, for the inline message. */
export const FIELD_OF: Record<string, string> = {
  repo_not_found: "repo",
  repo_not_writable: "repo",
  branch_not_found: "base_branch",
};

export function messageFor(err: unknown): string {
  if (err instanceof ApiError) {
    const base = TEXT[err.code] ?? err.code;
    if (err.status === 429 && err.retryAfter) {
      return `${base} Retry in ${formatWait(err.retryAfter)}.`;
    }
    return base;
  }
  return TEXT.unknown_error;
}

function formatWait(seconds: number): string {
  if (seconds < 90) return `${seconds} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}
