// Sign-in (m10a §0 decisions 3, 14): Supabase GitHub OAuth (scope `repo`), or, in dev builds
// only, a pasted JWT + provider token. After each sign-in the provider token is posted to
// /v1/me/github-token so the worker can clone and open the PR.

import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { api } from "./api";

export const DEV_AUTH: boolean = import.meta.env.VITE_AUTH_MODE === "dev";

export interface AuthState {
  token: string;
}

type Listener = (state: AuthState | null) => void;

const DEV_KEY = "swf.dev-session";
let current: AuthState | null = null;
const listeners = new Set<Listener>();
let client: SupabaseClient | null = null;

function emit(state: AuthState | null): void {
  current = state;
  for (const fn of listeners) fn(state);
}

export function currentToken(): string | null {
  return current?.token ?? null;
}

export function subscribe(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

async function linkGitHub(providerToken: string): Promise<void> {
  await api<void>("POST", "/v1/me/github-token", { provider_token: providerToken });
}

async function supabase(): Promise<SupabaseClient> {
  if (client) return client;
  const url = import.meta.env.VITE_SUPABASE_URL as string | undefined;
  const key = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;
  if (!url || !key) throw new Error("VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY are not set");
  const { createClient } = await import("@supabase/supabase-js");
  // PKCE: the OAuth result comes back as `?code=`, which leaves the hash router alone.
  client = createClient(url, key, { auth: { flowType: "pkce", detectSessionInUrl: true } });
  return client;
}

/** Restore a session on page load; returns once the initial state is known. */
export async function initAuth(): Promise<void> {
  if (DEV_AUTH) {
    const saved = sessionStorage.getItem(DEV_KEY);
    emit(saved ? (JSON.parse(saved) as AuthState) : null);
    return;
  }
  const sb = await supabase();
  sb.auth.onAuthStateChange((event, session: Session | null) => {
    if (!session) {
      emit(null);
      return;
    }
    emit({ token: session.access_token });
    if (event === "SIGNED_IN" && session.provider_token) {
      void linkGitHub(session.provider_token).catch(() => {});
    }
  });
  const { data } = await sb.auth.getSession();
  emit(data.session ? { token: data.session.access_token } : null);
  if (window.location.search.includes("code=")) {
    window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  }
}

export async function signInWithGitHub(): Promise<void> {
  const sb = await supabase();
  await sb.auth.signInWithOAuth({
    provider: "github",
    options: { scopes: "repo", redirectTo: window.location.origin + window.location.pathname },
  });
}

/** Dev builds only: the token is kept in sessionStorage for this tab. */
export async function devSignIn(token: string, providerToken: string): Promise<void> {
  const state = { token: token.trim() };
  emit(state);
  try {
    if (providerToken.trim()) await linkGitHub(providerToken.trim());
  } catch (err) {
    emit(null);
    throw err;
  }
  sessionStorage.setItem(DEV_KEY, JSON.stringify(state));
}

export async function signOut(): Promise<void> {
  if (DEV_AUTH) {
    sessionStorage.removeItem(DEV_KEY);
  } else if (client) {
    await client.auth.signOut();
  }
  emit(null);
}
