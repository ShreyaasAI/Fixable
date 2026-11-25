import { useCallback, useEffect, useState } from "react";
import { ApiError, api, type AdminUser, type Me, type SystemStatus } from "../api";
import { ConfirmDialog, timeAgo } from "../components";
import { messageFor } from "../messages";

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
      <div className="stack">
        <h1>Admin</h1>
        <p className="alert" role="alert" data-testid="admin-error">
          {error ?? "Admins only."}
        </p>
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
    <div className="stack">
      <h1>Admin</h1>
      {error && (
        <p className="alert" role="alert" data-testid="admin-error">
          {error}
        </p>
      )}
      {state && (
        <>
          <section
            className="card stack"
            data-testid="admin-state"
            data-paused={String(state.tasks_paused)}
            aria-labelledby="admin-state-title"
          >
            <h2 id="admin-state-title">
              Service: {state.tasks_paused ? "paused" : "running"}
            </h2>
            <p className="muted">
              {state.queued} queued, {state.active} running
              {state.updated_at && ` · changed ${timeAgo(state.updated_at)}`}
            </p>
            <div className="row">
              <button
                type="button"
                className="btn btn-danger"
                data-testid="pause-all"
                disabled={busy || state.tasks_paused}
                onClick={() => setAction({ kind: "pause" })}
              >
                Pause all
              </button>
              <button
                type="button"
                className="btn"
                data-testid="resume-all"
                disabled={busy || !state.tasks_paused}
                onClick={() => void run({ kind: "resume" })}
              >
                Resume all
              </button>
            </div>
          </section>
          <section className="card stack" aria-labelledby="users-title">
            <h2 id="users-title">Users</h2>
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">GitHub login</th>
                    <th scope="col">State</th>
                    <th scope="col">Open tasks</th>
                    <th scope="col">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {state.users.map((u) => {
                    const self = u.user_id === me?.user_id;
                    return (
                      <tr
                        key={u.user_id}
                        data-testid="admin-user-row"
                        data-user-id={u.user_id}
                        data-disabled={String(u.disabled)}
                      >
                        <td className="mono">
                          {u.github_login ?? u.user_id.slice(0, 8)}
                          {u.is_admin && <span className="badge badge-info"> admin</span>}
                        </td>
                        <td>
                          <span className={`badge ${u.disabled ? "badge-bad" : "badge-ok"}`}>
                            {u.disabled ? "disabled" : "active"}
                          </span>
                        </td>
                        <td>{u.open_tasks}</td>
                        <td>
                          {u.disabled ? (
                            <button
                              type="button"
                              className="btn"
                              data-testid="resume-user"
                              disabled={busy || self}
                              onClick={() => void run({ kind: "resume-user", user: u })}
                            >
                              Resume
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="btn btn-danger-outline"
                              data-testid="kill-user"
                              disabled={busy || self}
                              aria-label={`Kill ${u.github_login ?? u.user_id}`}
                              onClick={() => setAction({ kind: "kill", user: u })}
                            >
                              Kill
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
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
