import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, type Preflight, type Repo, type Task } from "../api";
import { FIELD_OF, messageFor } from "../messages";
import { go } from "../nav";

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
  description: "What is wrong",
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
      const value = await api<Preflight>("POST", "/v1/preflight", {
        repo,
        base_branch: branch,
      });
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

  const fieldError = (field: string) =>
    problem?.field === field ? (
      <p className="field-error" id={`${field}-error`} data-testid="field-error" data-field={field}>
        {problem.message}
      </p>
    ) : null;
  const invalid = (field: string) =>
    problem?.field === field ? { "aria-invalid": true, "aria-describedby": `${field}-error` } : {};

  return (
    <div className="stack">
      <h1>New task</h1>
      {problem && (
        <div
          className="alert"
          role="alert"
          tabIndex={-1}
          ref={summary}
          data-testid="error-summary"
          aria-labelledby="error-summary-title"
        >
          <h2 id="error-summary-title">There is a problem</h2>
          {problem.field ? (
            <a href={`#${FIELD_IDS[problem.field]}`} onClick={(e) => {
              e.preventDefault();
              document.getElementById(FIELD_IDS[problem.field!])?.focus();
            }}>
              {FIELD_LABELS[problem.field]}: {problem.message}
            </a>
          ) : (
            <p>{problem.message}</p>
          )}
        </div>
      )}
      <form className="card stack" onSubmit={(e) => void check(e)} noValidate>
        <div className="field">
          <label htmlFor="repo-select">Repository</label>
          <select
            id="repo-select"
            data-testid="repo-select"
            value={repo}
            onChange={(e) => chooseRepo(e.target.value)}
            {...invalid("repo")}
          >
            <option value="" disabled>
              {repos === null ? "Loading…" : "Choose a repository"}
            </option>
            {repos?.map((r) => (
              <option key={r.full_name} value={r.full_name} disabled={!r.can_push}>
                {r.can_push ? r.full_name : `${r.full_name} (no push access)`}
              </option>
            ))}
          </select>
          <p className="hint">Only repositories you can push to can be picked.</p>
          {fieldError("repo")}
        </div>
        <div className="field">
          <label htmlFor="branch-input">Base branch</label>
          <input
            id="branch-input"
            data-testid="branch-input"
            className="mono"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            {...invalid("base_branch")}
          />
          {fieldError("base_branch")}
        </div>
        <div className="field">
          <label htmlFor="description-input">What is wrong</label>
          <textarea
            id="description-input"
            data-testid="description-input"
            rows={4}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            {...invalid("description")}
          />
          <p className="hint">Describe the failing behaviour, as you would in an issue.</p>
          {fieldError("description")}
        </div>
        <div className="field">
          <label htmlFor="failing-test-input">Failing test (optional)</label>
          <input
            id="failing-test-input"
            data-testid="failing-test-input"
            className="mono"
            placeholder="tests/test_x.py::test_y"
            value={failingTest}
            onChange={(e) => setFailingTest(e.target.value)}
            {...invalid("failing_test")}
          />
          {fieldError("failing_test")}
        </div>
        <div className="row">
          <button type="submit" className="btn" data-testid="preflight-button" disabled={busy !== null}>
            {busy === "check" ? "Checking…" : "Check"}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="start-button"
            disabled={!canStart}
            onClick={() => void start()}
          >
            {busy === "start" ? "Starting…" : "Start"}
          </button>
        </div>
      </form>
      {current && <PreflightResult value={current} />}
    </div>
  );
}

function PreflightResult({ value }: { value: Preflight }) {
  const label =
    value.decision === "admit"
      ? "Ready to start"
      : value.decision === "queue"
        ? `Will queue at position ${value.queue_position ?? "?"}`
        : "Cannot run";
  return (
    <section
      className={`card stack preflight preflight-${value.decision}`}
      data-testid="preflight-result"
      data-decision={value.decision}
      aria-labelledby="preflight-title"
    >
      <h2 id="preflight-title">Pre-flight: {label}</h2>
      <dl className="facts">
        <dt>Image</dt>
        <dd className="mono">{value.image}</dd>
        <dt>Memory</dt>
        <dd>{value.memory_mb} MB</dd>
        <dt>CPU</dt>
        <dd>{value.cpus} vCPU</dd>
        <dt>Time limit</dt>
        <dd>{value.time_limit_minutes} min</dd>
      </dl>
      {value.warnings.length > 0 && (
        <ul className="warnings">
          {value.warnings.map((w) => (
            <li key={w.code}>
              <span className="badge badge-warn">{w.code.replace(/_/g, " ")}</span> {w.message}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
