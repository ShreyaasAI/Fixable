// Shell and hash router (m10a §2): #/signin, #/, #/new, #/tasks/<id>, #/admin.
// m11: workbench shell; the nav wraps to its own row under 768 px.

import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, configureApi, type Me } from "./api";
import { currentToken, initAuth, onLinkError, signOut, subscribe } from "./auth";
import { messageFor } from "./messages";
import { go } from "./nav";
import { Admin } from "./screens/Admin";
import { Dashboard } from "./screens/Dashboard";
import { NewTask } from "./screens/NewTask";
import { SignIn } from "./screens/SignIn";
import { TaskDetail } from "./screens/TaskDetail";
import { AppError, Button, ErrorBoundary, Footer, TooltipProvider, Wordmark } from "./ui";
import { cn } from "./lib/utils";

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
  return (
    <MotionConfig reducedMotion="user">
      <TooltipProvider delayDuration={300}>
        <Shell />
      </TooltipProvider>
    </MotionConfig>
  );
}

function Shell() {
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

  if (!ready) return <main className="min-h-dvh" aria-busy="true" />;
  if (!token || route === "/signin") return <SignIn error={error} onError={setError} />;

  let screen;
  const taskMatch = /^\/tasks\/([0-9a-f-]{36})$/.exec(route);
  if (taskMatch) screen = <TaskDetail taskId={taskMatch[1]} onChange={refreshMe} />;
  else if (route === "/new") screen = <NewTask onCreated={refreshMe} />;
  else if (route === "/admin") screen = <Admin me={me} />;
  else screen = <Dashboard me={me} />;
  const section = taskMatch ? "/" : route;

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b border-line">
        <div className="mx-auto flex w-full max-w-[1100px] flex-wrap items-center gap-x-10 px-5 pt-2.5 md:px-8 md:py-3">
          <a href="#/" aria-label="Fixable, tasks" className="rounded-xs py-1.5">
            <Wordmark />
          </a>
          <nav
            aria-label="Main"
            className="order-last -mx-5 flex w-[calc(100%+2.5rem)] gap-6 border-t border-line px-5 md:order-none md:mx-0 md:w-auto md:border-0 md:px-0"
          >
            <NavLink href="#/" testId="nav-dashboard" current={section === "/"}>
              Tasks
            </NavLink>
            <NavLink href="#/new" testId="nav-new" current={section === "/new"}>
              New fix
            </NavLink>
            {me?.is_admin && (
              <NavLink href="#/admin" testId="nav-admin" current={section === "/admin"}>
                Admin
              </NavLink>
            )}
          </nav>
          <div className="ml-auto flex items-center gap-3 py-1.5">
            <span className="max-w-[9rem] truncate font-mono text-[13px] text-muted" data-testid="user-login">
              {me?.github_login ?? (me ? "unknown" : "…")}
            </span>
            <Button kind="quiet" size="sm" data-testid="signout" onClick={() => void signOut()}>
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main id="main" className="mx-auto w-full max-w-[1100px] flex-1 px-5 py-7 md:px-8 md:py-12">
        <div className="mb-6 empty:hidden">
          <AppError message={error} />
        </div>
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={route}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
          >
            <ErrorBoundary key={route}>{screen}</ErrorBoundary>
          </motion.div>
        </AnimatePresence>
      </main>
      <Footer />
    </div>
  );
}

function NavLink(props: { href: string; testId: string; current: boolean; children: ReactNode }) {
  return (
    <a
      href={props.href}
      data-testid={props.testId}
      aria-current={props.current ? "page" : undefined}
      className={cn(
        "-mb-px border-b-2 py-3 font-mono text-[13px] font-medium uppercase tracking-[0.06em] no-underline transition-colors duration-150",
        props.current ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
      )}
    >
      {props.children}
    </a>
  );
}
