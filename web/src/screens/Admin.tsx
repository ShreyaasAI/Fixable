import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type AdminUser, type Me, type SystemStatus } from "../api";
import { timeAgo } from "../lib/utils";
import { messageFor } from "../messages";
import { Alert, Button, ConfirmDialog, PageHead, Panel, Stamp } from "../ui";

type Action =
  | { kind: "pause" }
  | { kind: "resume" }
  | { kind: "kill"; user: AdminUser }
  | { kind: "resume-user"; user: AdminUser };

export function Admin({ me }: { me: Me | null }) {
  const [state, setState] = useState<SystemStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [action, setAction] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setState(await api<SystemStatus>("GET", "/v1/admin/status"));
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setForbidden(true);
      setError(messageFor(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (forbidden) {
    return (
      <div className="flex flex-col gap-8">
        <PageHead title="Admin" />
        <Alert testId="admin-error">{error ?? "Admins only."}</Alert>
      </div>
    );
  }

  const run = async (a: Action) => {
    setAction(null);
    setBusy(true);
    try {
      if (a.kind === "pause") await api("POST", "/v1/admin/kill", {});
      else if (a.kind === "resume") await api("POST", "/v1/admin/resume", {});
      else if (a.kind === "kill") await api("POST", "/v1/admin/kill", { user_id: a.user.user_id });
      else await api("POST", "/v1/admin/resume", { user_id: a.user.user_id });
      await load();
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-8 md:gap-9">
      <PageHead
        title="Admin"
        lede="Stop everything, or one person, without touching the database."
      />
      {error && <Alert testId="admin-error">{error}</Alert>}
      {state && (
        <>
          <Panel
            data-testid="admin-state"
            data-paused={String(state.tasks_paused)}
            aria-labelledby="admin-state-title"
            className="flex flex-col gap-4 p-5 md:flex-row md:items-center md:gap-6 md:px-6"
          >
            <Stamp
              label={state.tasks_paused ? "Paused" : "Taking tasks"}
              tone={state.tasks_paused ? "danger" : "ok"}
              animate
              className="w-fit"
            />
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <h2 id="admin-state-title" className="text-[15px]">
                {state.tasks_paused
                  ? "New tasks are refused until you resume."
                  : "Workers are claiming new tasks."}
              </h2>
              <p className="font-mono text-xs text-muted">
                {state.queued} queued · {state.active} running
                {state.updated_at && ` · changed ${timeAgo(state.updated_at)}`}
                {state.updated_by && ` by ${state.updated_by}`}
              </p>
            </div>
            <div className="grid grid-cols-2 gap-3 md:flex">
              <Button
                kind="danger"
                data-testid="pause-all"
                disabled={busy || state.tasks_paused}
                onClick={() => setAction({ kind: "pause" })}
              >
                Pause all
              </Button>
              <Button
                kind="secondary"
                data-testid="resume-all"
                disabled={busy || !state.tasks_paused}
                onClick={() => void run({ kind: "resume" })}
              >
                Resume all
              </Button>
            </div>
          </Panel>
          <section aria-labelledby="users-title" className="flex flex-col gap-3">
            <h2 id="users-title" className="eyebrow">
              People · {state.users.length}
            </h2>
            <Panel className="overflow-hidden">
              <table className="w-full border-collapse text-left">
                <thead className="hidden bg-raised md:table-header-group">
                  <tr className="border-b border-line">
                    {["User", "State", "Open tasks"].map((h) => (
                      <th key={h} scope="col" className="eyebrow px-5 py-3 font-medium">
                        {h}
                      </th>
                    ))}
                    <th scope="col" className="px-5 py-3">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {state.users.map((u) => {
                    const self = u.user_id === me?.user_id;
                    const name = u.github_login ?? u.user_id.slice(0, 8);
                    return (
                      <tr
                        key={u.user_id}
                        data-testid="admin-user-row"
                        data-user-id={u.user_id}
                        data-disabled={String(u.disabled)}
                        className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 px-4 py-3.5 md:table-row md:px-0 md:py-0"
                      >
                        <td className="min-w-0 md:px-5 md:py-3">
                          <span className="font-mono text-sm">{name}</span>
                          {self && <span className="font-mono text-xs text-muted"> (you)</span>}
                          {u.is_admin && (
                            <span className="ml-2 font-mono text-xs uppercase tracking-[0.06em] text-info">
                              admin
                            </span>
                          )}
                        </td>
                        <td className="col-start-1 row-start-2 md:px-5 md:py-3">
                          <Stamp
                            label={u.disabled ? "disabled" : "active"}
                            tone={u.disabled ? "danger" : "ok"}
                          />
                        </td>
                        <td className="col-start-1 row-start-3 font-mono text-[13px] text-muted md:px-5 md:py-3 md:text-sm md:text-fg">
                          <span className="md:hidden">open tasks: </span>
                          {u.open_tasks}
                        </td>
                        <td className="col-start-2 row-span-3 row-start-1 md:px-5 md:py-2.5 md:text-right">
                          {u.disabled ? (
                            <Button
                              kind="secondary"
                              size="sm"
                              data-testid="resume-user"
                              disabled={busy || self}
                              onClick={() => void run({ kind: "resume-user", user: u })}
                            >
                              Resume
                            </Button>
                          ) : (
                            <Button
                              kind="danger-outline"
                              size="sm"
                              data-testid="kill-user"
                              disabled={busy || self}
                              aria-label={`Kill ${u.github_login ?? u.user_id}`}
                              onClick={() => setAction({ kind: "kill", user: u })}
                            >
                              Kill
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </Panel>
          </section>
        </>
      )}
      <ConfirmDialog
        open={action !== null}
        title={action?.kind === "kill" ? "Kill this user?" : "Pause all tasks?"}
        confirmLabel={action?.kind === "kill" ? "Kill user" : "Pause all"}
        danger
        onCancel={() => setAction(null)}
        onConfirm={() => action && void run(action)}
      >
        {action?.kind === "kill" ? (
          <p>
            <span className="mono">{action.user.github_login ?? action.user.user_id}</span> is
            disabled and their {action.user.open_tasks} open task(s) are cancelled.
          </p>
        ) : (
          <p>
            New tasks are refused and the {state ? state.queued + state.active : 0} open task(s)
            are cancelled.
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
