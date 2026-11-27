import { ArrowRight, Plus } from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { api, type Me, type Task } from "../api";
import { shortId, timeAgo } from "../lib/utils";
import { messageFor } from "../messages";
import { Alert, Button, Ledger, PageHead, Panel, StatusStamp } from "../ui";

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
    <div className="flex flex-col gap-8 md:gap-9">
      <PageHead
        title="Tasks"
        lede="Each run gets its own sandbox, a budget, and a written record."
        action={
          <Button kind="primary" asChild>
            <a href="#/new" className="no-underline">
              <Plus aria-hidden="true" /> New fix
            </a>
          </Button>
        }
      />
      {me && <Quota me={me} />}
      {error && <Alert testId="page-error">{error}</Alert>}
      {tasks && tasks.length === 0 && (
        <Panel data-testid="empty-tasks" className="flex flex-col items-start gap-3 px-6 py-10">
          <p className="font-display text-2xl font-extrabold">Nothing on the bench yet.</p>
          <p className="text-muted">Pick a repo and a failing test, and the first fix starts here.</p>
          <Button kind="link" asChild>
            <a href="#/new">
              Start a fix <ArrowRight aria-hidden="true" className="!size-4" />
            </a>
          </Button>
        </Panel>
      )}
      {tasks && tasks.length > 0 && (
        <section aria-labelledby="recent-title" className="flex flex-col gap-3">
          <h2 id="recent-title" className="eyebrow">
            Recent · {tasks.length}
          </h2>
          <Panel className="overflow-hidden">
            <ul data-testid="task-list" className="divide-y divide-line">
              {tasks.map((t, i) => (
                <motion.li
                  key={t.id}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.2, delay: Math.min(i, 8) * 0.025 }}
                  data-testid="task-row"
                  data-task-id={t.id}
                  data-status={t.status}
                >
                  <a
                    href={`#/tasks/${t.id}`}
                    className="grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1.5 px-4 py-3.5 no-underline transition-colors duration-150 hover:bg-raised md:grid-cols-[5.5rem_1fr_auto_6.5rem] md:px-5"
                  >
                    <span className="font-mono text-xs text-muted md:text-[13px]">
                      {shortId(t.id)}
                      <span className="md:hidden"> · {t.repo}</span>
                    </span>
                    <span className="col-span-2 row-start-2 flex min-w-0 flex-col gap-0.5 md:col-span-1 md:row-start-auto">
                      <span className="hidden truncate font-mono text-sm md:block">{t.repo}</span>
                      <span className="line-clamp-2 text-[15px] md:truncate md:text-sm md:text-muted">
                        {t.description}
                      </span>
                    </span>
                    <span className="col-start-2 row-start-1 justify-self-end md:col-start-auto md:row-start-auto">
                      <StatusStamp status={t.status} />
                    </span>
                    <span className="col-span-2 font-mono text-xs text-muted md:col-span-1 md:text-right">
                      {timeAgo(t.created_at)}
                    </span>
                  </a>
                </motion.li>
              ))}
            </ul>
          </Panel>
        </section>
      )}
    </div>
  );
}

function Quota({ me }: { me: Me }) {
  const { limits, remaining } = me;
  if (!limits || !remaining) return null; // an older API without quota fields
  return (
    <Ledger
      label="Quota"
      cells={[
        {
          testId: "quota-tasks",
          label: "Tasks today",
          value: String(remaining.tasks_today),
          of: `of ${limits.tasks_per_day}`,
        },
        {
          testId: "quota-cost",
          label: "Spend today",
          value: `$${remaining.cost_today_usd.toFixed(2)}`,
          of: `of $${limits.cost_per_day_usd.toFixed(2)}`,
        },
        {
          testId: "quota-concurrent",
          label: "Running now",
          value: String(remaining.concurrent_tasks),
          of: `of ${limits.concurrent_tasks} at once`,
        },
      ]}
    />
  );
}
