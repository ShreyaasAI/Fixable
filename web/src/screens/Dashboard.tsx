import { useEffect, useState } from "react";
import { api, type Me, type Task } from "../api";
import { StatusBadge, shortId, timeAgo } from "../components";
import { messageFor } from "../messages";

export function Dashboard({ me }: { me: Me | null }) {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api<Task[]>("GET", "/v1/tasks")
        .then((t) => alive && (setTasks(t), setError(null)))
        .catch((err: unknown) => alive && setError(messageFor(err)));
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  return (
    <div className="stack">
      <h1>Tasks</h1>
      {me && <Quota me={me} />}
      {error && (
        <div className="alert" role="alert" data-testid="page-error">
          {error}
        </div>
      )}
      {tasks && tasks.length === 0 && (
        <p className="card muted" data-testid="empty-tasks">
          No tasks yet. <a href="#/new">Create one</a>.
        </p>
      )}
      {tasks && tasks.length > 0 && (
        <ul className="task-list" data-testid="task-list">
          {tasks.map((t) => (
            <li key={t.id} data-testid="task-row" data-task-id={t.id} data-status={t.status}>
              <a href={`#/tasks/${t.id}`} className="task-link">
                <span className="mono">{shortId(t.id)}</span>
                <span className="mono repo">{t.repo}</span>
                <span className="desc">{t.description}</span>
                <StatusBadge status={t.status} />
                <span className="muted when">{timeAgo(t.created_at)}</span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Quota({ me }: { me: Me }) {
  const { limits, remaining } = me;
  return (
    <section className="quota" aria-label="Quota">
      <div className="quota-item" data-testid="quota-tasks">
        <span className="quota-num">{remaining.tasks_today}</span> left
        <span className="muted"> of {limits.tasks_per_day} tasks today</span>
      </div>
      <div className="quota-item" data-testid="quota-cost">
        <span className="quota-num">${remaining.cost_today_usd.toFixed(2)}</span> left
        <span className="muted"> of ${limits.cost_per_day_usd.toFixed(2)} today</span>
      </div>
      <div className="quota-item" data-testid="quota-concurrent">
        <span className="quota-num">{remaining.concurrent_tasks}</span> left
        <span className="muted"> of {limits.concurrent_tasks} running at once</span>
      </div>
    </section>
  );
}
