import { AlertTriangle } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, type Preflight, type Repo, type Task } from "../api";
import { FIELD_OF, messageFor } from "../messages";
import { go } from "../nav";
import { Button, Field, Input, PageHead, Panel, RepoCombobox, Stamp, Textarea } from "../ui";

interface Problem {
  message: string;
  field: string | null; // "repo" | "base_branch" | "description" | "failing_test"
}

const FIELD_IDS: Record<string, string> = {
  repo: "repo-select",
  base_branch: "branch-input",
  description: "description-input",
  failing_test: "failing-test-input",
};

const FIELD_LABELS: Record<string, string> = {
  repo: "Repository",
  base_branch: "Base branch",
  description: "What's wrong",
  failing_test: "Failing test",
};

function problemOf(err: unknown): Problem {
  const message = messageFor(err);
  if (err instanceof ApiError) {
    const field = FIELD_OF[err.code] ?? (err.field && FIELD_IDS[err.field] ? err.field : null);
    return { message, field };
  }
  return { message, field: null };
}

export function NewTask({ onCreated }: { onCreated: () => void }) {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [description, setDescription] = useState("");
  const [failingTest, setFailingTest] = useState("");
  const [result, setResult] = useState<{ key: string; value: Preflight } | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState<"check" | "start" | null>(null);
  const summary = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api<Repo[]>("GET", "/v1/repos")
      .then(setRepos)
      .catch((err: unknown) => setProblem(problemOf(err)));
  }, []);

  useEffect(() => {
    if (problem) summary.current?.focus();
  }, [problem]);

  const key = `${repo}\n${branch}`;
  const current = result && result.key === key ? result.value : null;
  const canStart = current !== null && current.decision !== "reject" && busy === null;

  const chooseRepo = (name: string) => {
    setRepo(name);
    const found = repos?.find((r) => r.full_name === name);
    if (found) setBranch(found.default_branch);
  };

  const check = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!repo) {
      setProblem({ message: "Choose a repository.", field: "repo" });
      return;
    }
    setBusy("check");
    setProblem(null);
    try {
      const value = await api<Preflight>("POST", "/v1/preflight", { repo, base_branch: branch });
      setResult({ key, value });
    } catch (err) {
      setResult(null);
      setProblem(problemOf(err));
    } finally {
      setBusy(null);
    }
  };

  const start = async () => {
    if (!description.trim()) {
      setProblem({ message: "Describe what is wrong.", field: "description" });
      return;
    }
    setBusy("start");
    setProblem(null);
    try {
      const created = await api<{ task: Task }>("POST", "/v1/tasks", {
        repo,
        base_branch: branch,
        description,
        failing_test: failingTest.trim() || null,
      });
      onCreated();
      go(`/tasks/${created.task.id}`);
    } catch (err) {
      setProblem(problemOf(err));
    } finally {
      setBusy(null);
    }
  };

  const errorFor = (field: string) => (problem?.field === field ? problem.message : null);
  const invalid = (field: string) =>
    problem?.field === field ? { "aria-invalid": true, "aria-describedby": `${field}-error` } : {};

  return (
    <div className="flex flex-col gap-8">
      <PageHead
        title="New fix"
        lede="Tell it which test fails and what you expected. Check first for the sandbox it gets."
      />
      {problem && (
        <div
          role="alert"
          tabIndex={-1}
          ref={summary}
          data-testid="error-summary"
          aria-labelledby="error-summary-title"
          className="flex flex-col gap-1.5 rounded-sm border border-danger/60 border-l-[3px] border-l-danger bg-raised px-4 py-3"
        >
          <h2 id="error-summary-title" className="eyebrow !text-danger">
            Fix this first
          </h2>
          {problem.field ? (
            <a
              href={`#${FIELD_IDS[problem.field]}`}
              className="text-[15px] underline underline-offset-4"
              onClick={(e) => {
                e.preventDefault();
                document.getElementById(FIELD_IDS[problem.field!])?.focus();
              }}
            >
              {FIELD_LABELS[problem.field]}: {problem.message}
            </a>
          ) : (
            <p className="text-[15px]">{problem.message}</p>
          )}
        </div>
      )}
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)] lg:gap-8">
        <Panel className="p-5 md:p-8">
          <form className="flex flex-col gap-6" onSubmit={(e) => void check(e)} noValidate>
            <Field
              id="repo-select"
              label="Repository"
              field="repo"
              error={errorFor("repo")}
              hint="Repos you can only read are listed but can't be picked."
            >
              <RepoCombobox
                id="repo-select"
                repos={repos}
                value={repo}
                onChange={chooseRepo}
                invalid={invalid("repo")}
              />
            </Field>
            <Field
              id="branch-input"
              label="Base branch"
              field="base_branch"
              error={errorFor("base_branch")}
              hint="Filled in from the repo's default branch."
            >
              <Input
                id="branch-input"
                data-testid="branch-input"
                className="font-mono text-sm"
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                {...invalid("base_branch")}
              />
            </Field>
            <Field
              id="description-input"
              label="What's wrong"
              field="description"
              error={errorFor("description")}
              hint="Describe the failing behaviour, as you would in an issue."
            >
              <Textarea
                id="description-input"
                data-testid="description-input"
                rows={4}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                {...invalid("description")}
              />
            </Field>
            <Field
              id="failing-test-input"
              label="Failing test (optional)"
              field="failing_test"
              error={errorFor("failing_test")}
              hint="A pytest node id. One test per task."
            >
              <Input
                id="failing-test-input"
                data-testid="failing-test-input"
                className="font-mono text-sm"
                placeholder="tests/test_x.py::test_y"
                value={failingTest}
                onChange={(e) => setFailingTest(e.target.value)}
                {...invalid("failing_test")}
              />
            </Field>
            <div className="flex flex-col-reverse gap-3 pt-1 sm:flex-row">
              <Button type="submit" kind="secondary" data-testid="preflight-button" disabled={busy !== null}>
                {busy === "check" ? "Checking…" : "Check first"}
              </Button>
              <Button
                kind="primary"
                data-testid="start-button"
                disabled={!canStart}
                onClick={() => void start()}
              >
                {busy === "start" ? "Starting…" : "Start the fix"}
              </Button>
            </div>
          </form>
        </Panel>
        <AnimatePresence mode="wait">
          {current ? (
            <PreflightResult key={key} value={current} />
          ) : (
            <motion.aside
              key="hint"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="hidden rounded-sm border border-dashed border-line p-6 lg:block"
            >
              <p className="eyebrow">Preflight</p>
              <p className="mt-3 text-[15px] text-muted">
                Check first to see the sandbox image, memory, CPU and time limit before anything
                is created.
              </p>
            </motion.aside>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

function PreflightResult({ value }: { value: Preflight }) {
  const label =
    value.decision === "admit"
      ? "ready"
      : value.decision === "queue"
        ? `queues at #${value.queue_position ?? "?"}`
        : "can't run";
  const tone = value.decision === "admit" ? "ok" : value.decision === "queue" ? "warn" : "danger";
  const facts: [string, string][] = [
    ["Image", value.image],
    ["Memory", `${value.memory_mb} MB`],
    ["CPU", `${value.cpus} vCPU`],
    ["Time limit", `${value.time_limit_minutes} min`],
  ];
  return (
    <motion.section
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ type: "spring", stiffness: 380, damping: 30 }}
      data-testid="preflight-result"
      data-decision={value.decision}
      aria-labelledby="preflight-title"
      className="flex flex-col gap-5 rounded-sm border border-line bg-surface p-5 md:p-6"
    >
      <div className="flex items-center justify-between gap-3">
        <h2 id="preflight-title" className="eyebrow">
          Preflight
        </h2>
        <Stamp label={label} tone={tone} animate />
      </div>
      <dl className="divide-y divide-line border-y border-line">
        {facts.map(([k, v]) => (
          <div key={k} className="flex items-baseline justify-between gap-4 py-2.5">
            <dt className="text-sm text-muted">{k}</dt>
            <dd className="truncate font-mono text-sm">{v}</dd>
          </div>
        ))}
      </dl>
      {value.warnings.length > 0 && (
        <ul className="flex flex-col gap-2">
          {value.warnings.map((w) => (
            <li
              key={w.code}
              className="flex gap-3 border-l-[3px] border-warn bg-raised px-3.5 py-3 text-sm"
            >
              <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warn" />
              <span>
                <span className="font-mono text-xs uppercase tracking-[0.06em] text-warn">
                  {w.code.replace(/_/g, " ")}
                </span>{" "}
                {w.message}
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="font-mono text-xs text-muted">Nothing is created until you press Start.</p>
    </motion.section>
  );
}
