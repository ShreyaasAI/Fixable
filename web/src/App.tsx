// Shell and hash router (m10a §2): #/signin, #/, #/new, #/tasks/<id>, #/admin.

import { useCallback, useEffect, useState } from "react";
import { api, configureApi, type Me } from "./api";
import { currentToken, initAuth, onLinkError, signOut, subscribe } from "./auth";
import { AppError } from "./components";
import { messageFor } from "./messages";
import { go } from "./nav";
import { Admin } from "./screens/Admin";
import { Dashboard } from "./screens/Dashboard";
import { NewTask } from "./screens/NewTask";
import { SignIn } from "./screens/SignIn";
import { TaskDetail } from "./screens/TaskDetail";

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function App() {
  const hash = useHash();
  const [ready, setReady] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    configureApi(currentToken, () => void signOut());
    onLinkError((err) => setError(`Linking GitHub failed: ${messageFor(err)}`));
    const off = subscribe((state) => setToken(state?.token ?? null));
    initAuth()
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => {
        setToken(currentToken());
        setReady(true);
      });
    return () => {
      off();
    };
  }, []);

  const refreshMe = useCallback(async () => {
    try {
      setMe(await api<Me>("GET", "/v1/me"));
      setError(null);
    } catch (err) {
      setMe(null);
      setError(messageFor(err));
    }
  }, []);

  useEffect(() => {
    if (token) void refreshMe();
    else setMe(null);
  }, [token, refreshMe]);

  const route = hash.replace(/^#/, "") || "/";
  useEffect(() => {
    if (ready && !token && route !== "/signin") go("/signin");
    if (ready && token && route === "/signin") go("/");
  }, [ready, token, route]);

  if (!ready) return <main className="page" aria-busy="true" />;
  if (!token || route === "/signin") {
    return (
      <main className="page narrow" id="main">
        <AppError message={error} />
        <SignIn onError={setError} />
      </main>
    );
  }

  let screen;
  const taskMatch = /^\/tasks\/([0-9a-f-]{36})$/.exec(route);
  if (taskMatch) screen = <TaskDetail taskId={taskMatch[1]} onChange={refreshMe} />;
  else if (route === "/new") screen = <NewTask onCreated={refreshMe} />;
  else if (route === "/admin") screen = <Admin me={me} />;
  else screen = <Dashboard me={me} />;

  return (
    <>
      <header className="topbar">
        <a className="brand" href="#/">
          SWF
        </a>
        <nav aria-label="Main" className="nav">
          <a href="#/" data-testid="nav-dashboard" aria-current={route === "/" ? "page" : undefined}>
            Tasks
          </a>
          <a href="#/new" data-testid="nav-new" aria-current={route === "/new" ? "page" : undefined}>
            New task
          </a>
          {me?.is_admin && (
            <a
              href="#/admin"
              data-testid="nav-admin"
              aria-current={route === "/admin" ? "page" : undefined}
            >
              Admin
            </a>
          )}
        </nav>
        <div className="row user">
          <span className="mono" data-testid="user-login">
            {me?.github_login ?? (me ? "unknown" : "…")}
          </span>
          <button type="button" className="btn btn-ghost" data-testid="signout" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>
      <main className="page" id="main">
        <AppError message={error} />
        {screen}
      </main>
    </>
  );
}
