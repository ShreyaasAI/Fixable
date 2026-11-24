import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, TERMINAL, api, type EvidenceItem, type Task } from "../api";
import { ConfirmDialog, StatusBadge, shortId } from "../components";
import { NOISE_TYPES } from "../events";
import { messageFor } from "../messages";
import { go } from "../nav";
import { streamEvents, type StreamEvent } from "../sse";

const NOISE = new Set<string>(NOISE_TYPES);
const COLLAPSE_LINES = 40;

type Confirm = "cancel" | "delete" | null;

export function TaskDetail({ taskId, onChange }: { taskId: string; onChange: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<Map<number, StreamEvent>>(new Map());
  const [ended, setEnded] = useState(false);
  const [evidence, setEvidence] = useState<EvidenceItem[] | null>(null);
  const [showNoise, setShowNoise] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm>(null);

  const loadTask = useCallback(async () => {
    try {
      setTask(await api<Task>("GET", `/v1/tasks/${taskId}`));
    } catch (err) {
      setError(messageFor(err));
    }
  }, [taskId]);

  const loadEvidence = useCallback(async () => {
    try {
      const out = await api<{ items: EvidenceItem[] }>("GET", `/v1/tasks/${taskId}/evidence`);
      setEvidence(out.items);
    } catch {
      /* evidence is optional on this page */
    }
  }, [taskId]);

  useEffect(() => {
    setEvents(new Map());
    setEnded(false);
    void loadTask();
    const stop = streamEvents(taskId, {
      onEvent: (event) => {
        setEvents((prev) => {
          if (prev.has(event.seq)) return prev;
          const next = new Map(prev);
          next.set(event.seq, event);
          return next;
        });
      },
      onEnd: () => setEnded(true),
      onError: (status) => {
        if (status === 403) setError(messageFor(new ApiError(403, "account_disabled", null)));
      },
    });
    return stop;
  }, [taskId, loadTask]);

  const ordered = useMemo(() => [...events.values()].sort((a, b) => a.seq - b.seq), [events]);

  const status = useMemo(() => {
    for (let i = ordered.length - 1; i >= 0; i--) {
      const e = ordered[i];
      if (e.type === "status" && typeof e.data.to === "string") return e.data.to;
    }
    return task?.status ?? "";
  }, [ordered, task]);
  const terminal = TERMINAL.has(status);

  // Refresh the row (PR url, worker release) and the evidence when the task ends.
  useEffect(() => {
    if (terminal || ended) {
      void loadTask();
      void loadEvidence();
      onChange();
    }
  }, [terminal, ended, loadTask, loadEvidence, onChange]);

  // A finished task is deletable once the worker has let go of it; poll until then.
  useEffect(() => {
    if (!terminal || !task || task.worker_id == null) return;
    const timer = setInterval(() => void loadTask(), 2000);
    return () => clearInterval(timer);
  }, [terminal, task, loadTask]);

  const pending = useMemo(() => pendingCards(ordered), [ordered]);
  const prUrl =
    task?.pr_url ??
    (ordered.find((e) => e.type === "pr_opened")?.data.url as string | undefined) ??
    null;
  const visible = showNoise ? ordered : ordered.filter((e) => !NOISE.has(e.type));
  const hidden = ordered.length - visible.length;

  const doCancel = async () => {
    setConfirm(null);
    try {
      setTask(await api<Task>("POST", `/v1/tasks/${taskId}/cancel`));
    } catch (err) {
      setError(messageFor(err));
    }
  };
  const doDelete = async () => {
    setConfirm(null);
    try {
      await api<void>("DELETE", `/v1/tasks/${taskId}`);
      onChange();
      go("/");
    } catch (err) {
      setError(messageFor(err));
    }
  };

  return (
    <div className="stack">
      <div className="row between wrap">
        <h1 className="mono">Task {shortId(taskId)}</h1>
        <div className="row">
          {!terminal && status && (
            <button
              type="button"
              className="btn btn-danger-outline"
              data-testid="cancel-button"
              onClick={() => setConfirm("cancel")}
            >
              Cancel task
            </button>
          )}
          {terminal && (
            <button
              type="button"
              className="btn btn-danger-outline"
              data-testid="delete-button"
              onClick={() => setConfirm("delete")}
            >
              Delete
            </button>
          )}
        </div>
      </div>

      <section className="card stack" aria-label="Summary">
        <div className="row wrap">
          <span aria-live="polite" data-testid="task-status" data-status={status}>
            Status: <StatusBadge status={status || "loading"} />
          </span>
          {task && (
            <span className="mono muted">
              {task.repo} @ {task.base_branch}
            </span>
          )}
        </div>
        {task && <p className="desc-full">{task.description}</p>}
        {task?.error && (
          <p className="field-error" data-testid="task-error">
            Error: <span className="mono">{task.error}</span>
          </p>
        )}
        {prUrl && (
          <p>
            Pull request:{" "}
            <a href={prUrl} target="_blank" rel="noopener noreferrer" data-testid="pr-link">
              {prUrl}
            </a>
          </p>
        )}
      </section>

      {error && (
        <div className="alert" role="alert" data-testid="page-error">
          {error}
        </div>
      )}

      {pending.approvals.map((a) => (
        <ApprovalCard key={a.id} taskId={taskId} approval={a} />
      ))}
      {pending.questions.map((q) => (
        <QuestionCard key={q.id} taskId={taskId} question={q} />
      ))}

      {evidence && evidence.length > 0 && (
        <section className="card stack" aria-labelledby="evidence-title">
          <h2 id="evidence-title">Evidence</h2>
          <ul className="evidence" data-testid="evidence-list">
            {evidence.map((item) => (
              <li key={String(item.id)} data-testid="evidence-item">
                <span className="badge badge-info">{item.kind.replace(/_/g, " ")}</span>{" "}
                <span className="muted">{item.source}</span>
                {item.command && <code className="mono"> {item.command}</code>}
                {item.exit_code !== null && <ExitBadge code={item.exit_code} />}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="stack" aria-labelledby="timeline-title">
        <div className="row between wrap">
          <h2 id="timeline-title">Timeline</h2>
          <label className="row toggle">
            <input
              type="checkbox"
              data-testid="noise-toggle"
              checked={showNoise}
              onChange={(e) => setShowNoise(e.target.checked)}
            />
            Show internal events
            <span className="muted" data-testid="noise-hidden-count">
              ({hidden} hidden)
            </span>
          </label>
        </div>
        <ol className="timeline" data-testid="timeline">
          {visible.map((e) => (
            <EventRow key={e.seq} event={e} />
          ))}
        </ol>
      </section>

      <ConfirmDialog
        open={confirm !== null}
        title={confirm === "delete" ? "Delete this task?" : "Cancel this task?"}
        confirmLabel={confirm === "delete" ? "Delete" : "Cancel task"}
        danger
        onCancel={() => setConfirm(null)}
        onConfirm={() => void (confirm === "delete" ? doDelete() : doCancel())}
      >
        <p>
          Task <span className="mono">{shortId(taskId)}</span>
          {task ? ` on ${task.repo}` : ""}.{" "}
          {confirm === "delete"
            ? "Its timeline, approvals and evidence are removed. This cannot be undone."
            : "The agent stops and the sandbox is removed."}
        </p>
      </ConfirmDialog>
    </div>
  );
}

// --- approvals and questions, rebuilt from the replay -----------------------------------------

interface PendingApproval {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
}
interface PendingQuestion {
  id: string;
  question: string;
}

function pendingCards(events: StreamEvent[]): {
  approvals: PendingApproval[];
  questions: PendingQuestion[];
} {
  const approvals = new Map<string, PendingApproval>();
  const questions = new Map<string, PendingQuestion>();
  for (const e of events) {
    const d = e.data;
    if (e.type === "approval_requested") {
      approvals.set(String(d.approval_id), {
        id: String(d.approval_id),
        kind: String(d.kind),
        payload: (d.payload as Record<string, unknown>) ?? {},
      });
    } else if (e.type === "approval_decided" || e.type === "approval_expired") {
      approvals.delete(String(d.approval_id));
    } else if (e.type === "question_asked") {
      questions.set(String(d.question_id), {
        id: String(d.question_id),
        question: String(d.question),
      });
    } else if (e.type === "question_answered" || e.type === "question_expired") {
      questions.delete(String(d.question_id));
    }
  }
  return { approvals: [...approvals.values()], questions: [...questions.values()] };
}

function ApprovalCard({ taskId, approval }: { taskId: string; approval: PendingApproval }) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const decide = async (decision: "approve" | "reject") => {
    setBusy(true);
    setNote(null);
    try {
      await api("POST", `/v1/tasks/${taskId}/approvals/${approval.id}`, {
        decision,
        comment: comment.trim() || null,
      });
      setNote("Sent. Waiting for the agent…");
    } catch (err) {
      setNote(err instanceof ApiError && err.status === 409 ? "Already decided." : messageFor(err));
      setBusy(false);
    }
  };
  const commentId = `comment-${approval.id}`;
  return (
    <section
      className="card stack attention"
      data-testid="approval-card"
      data-approval-id={approval.id}
      data-kind={approval.kind}
      aria-labelledby={`approval-${approval.id}`}
    >
      <h2 id={`approval-${approval.id}`}>{approvalTitle(approval.kind)}</h2>
      <ApprovalBody kind={approval.kind} payload={approval.payload} />
      <label htmlFor={commentId}>Comment (optional)</label>
      <textarea
        id={commentId}
        data-testid="approval-comment"
        rows={2}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
      />
      <div className="row">
        <button
          type="button"
          className="btn btn-primary"
          data-testid="approve-button"
          disabled={busy}
          onClick={() => void decide("approve")}
        >
          Approve
        </button>
        <button
          type="button"
          className="btn btn-danger-outline"
          data-testid="reject-button"
          disabled={busy}
          onClick={() => void decide("reject")}
        >
          Reject
        </button>
        {note && <span className="muted">{note}</span>}
      </div>
    </section>
  );
}

function approvalTitle(kind: string): string {
  if (kind === "plan") return "Approve the plan";
  if (kind === "guard_flag") return "A guardrail flagged this step";
  if (kind === "command") return "Approve a command";
  return `Approval needed: ${kind.replace(/_/g, " ")}`;
}

function ApprovalBody({ kind, payload }: { kind: string; payload: Record<string, unknown> }) {
  if (kind === "plan") {
    const steps = Array.isArray(payload.steps) ? (payload.steps as unknown[]) : [];
    const files = Array.isArray(payload.files) ? (payload.files as unknown[]) : [];
    return (
      <div className="stack">
        {typeof payload.summary === "string" && <p>{payload.summary}</p>}
        {steps.length > 0 && (
          <ol className="plan">
            {steps.map((s, i) => (
              <li key={i}>{String(s)}</li>
            ))}
          </ol>
        )}
        {files.length > 0 && (
          <p className="muted">
            Files: <span className="mono">{files.map(String).join(", ")}</span>
          </p>
        )}
        <KeyValues data={payload} skip={["summary", "steps", "files"]} />
      </div>
    );
  }
  return <KeyValues data={payload} skip={[]} />;
}

function QuestionCard({ taskId, question }: { taskId: string; question: PendingQuestion }) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const send = async () => {
    if (!answer.trim()) return;
    setBusy(true);
    setNote(null);
    try {
      await api("POST", `/v1/tasks/${taskId}/answers/${question.id}`, { answer });
      setNote("Sent. Waiting for the agent…");
    } catch (err) {
      setNote(err instanceof ApiError && err.status === 409 ? "Already answered." : messageFor(err));
      setBusy(false);
    }
  };
  const inputId = `answer-${question.id}`;
  return (
    <section
      className="card stack attention"
      data-testid="question-card"
      data-question-id={question.id}
      aria-labelledby={`q-${question.id}`}
    >
      <h2 id={`q-${question.id}`}>The agent asks</h2>
      <p className="question">{question.question}</p>
      <label htmlFor={inputId}>Your answer</label>
      <textarea
        id={inputId}
        data-testid="answer-input"
        rows={2}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
      />
      <div className="row">
        <button
          type="button"
          className="btn btn-primary"
          data-testid="answer-button"
          disabled={busy || !answer.trim()}
          onClick={() => void send()}
        >
          Answer
        </button>
        {note && <span className="muted">{note}</span>}
      </div>
    </section>
  );
}

// --- timeline rows -------------------------------------------------------------------------

function EventRow({ event }: { event: StreamEvent }) {
  const d = event.data;
  let body;
  switch (event.type) {
    case "status":
      body = (
        <span>
          Status → <StatusBadge status={String(d.to)} />
          {typeof d.error === "string" && <span className="mono muted"> {d.error}</span>}
        </span>
      );
      break;
    case "message":
      body = (
        <div>
          <span className="tag">{String(d.role ?? "message")}</span>
          {typeof d.content === "string" && d.content && (
            <Output text={d.content} testId="message-output" />
          )}
        </div>
      );
      break;
    case "tool_call":
      body = (
        <div>
          <span className="tag">call</span> <span className="mono">{String(d.tool)}</span>
          <ToolArgs args={d.args} />
        </div>
      );
      break;
    case "tool_result":
      body = (
        <div>
          <span className="tag">result</span> <span className="mono">{String(d.tool)}</span>
          {typeof d.returncode === "number" && <ExitBadge code={d.returncode} />}
          {d.truncated === true && <span className="muted"> (truncated)</span>}
          <Output text={String(d.output ?? "")} testId="tool-output" />
        </div>
      );
      break;
    case "pr_opened":
      body = (
        <span>
          Pull request opened:{" "}
          <a href={String(d.url)} target="_blank" rel="noopener noreferrer">
            {String(d.url)}
          </a>
        </span>
      );
      break;
    case "completed":
      body = (
        <div>
          <span className="tag tag-ok">completed</span>
          {d.summary != null && <KeyValues data={summaryData(d.summary)} skip={[]} />}
        </div>
      );
      break;
    default:
      body = (
        <div>
          <span className="tag">{event.type.replace(/_/g, " ")}</span>
          <KeyValues data={d} skip={["created_at"]} />
        </div>
      );
  }
  return (
    <li className={`event event-${event.type}`} data-testid="event-row" data-seq={event.seq} data-type={event.type}>
      <span className="seq mono muted" aria-hidden="true">
        {event.seq}
      </span>
      <div className="event-body">{body}</div>
    </li>
  );
}

function summaryData(summary: unknown): Record<string, unknown> {
  return summary && typeof summary === "object"
    ? (summary as Record<string, unknown>)
    : { summary };
}

function ToolArgs({ args }: { args: unknown }) {
  if (args && typeof args === "object" && "command" in args) {
    return <pre className="code">{String((args as { command: unknown }).command)}</pre>;
  }
  if (args && typeof args === "object" && Object.keys(args).length > 0) {
    return <KeyValues data={args as Record<string, unknown>} skip={[]} />;
  }
  return null;
}

/** Output over 40 lines shows its first 40, with a toggle for the rest (m10a §2). */
function Output({ text, testId }: { text: string; testId: string }) {
  const [open, setOpen] = useState(false);
  const lines = text.split("\n");
  const short = lines.length <= COLLAPSE_LINES;
  const expanded = short || open;
  return (
    <div className="output" data-testid={testId} data-expanded={String(expanded)}>
      <pre className="code">{expanded ? text : lines.slice(0, COLLAPSE_LINES).join("\n")}</pre>
      {!short && (
        <button
          type="button"
          className="output-toggle"
          data-testid="output-toggle"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Show first 40 lines" : `Show all ${lines.length} lines`}
        </button>
      )}
    </div>
  );
}

function ExitBadge({ code }: { code: number }) {
  return (
    <span className={`badge ${code === 0 ? "badge-ok" : "badge-bad"}`}>exit {code}</span>
  );
}

function KeyValues({ data, skip }: { data: Record<string, unknown>; skip: string[] }) {
  const entries = Object.entries(data).filter(([k, v]) => !skip.includes(k) && v != null);
  if (entries.length === 0) return null;
  return (
    <dl className="kv">
      {entries.map(([k, v]) => (
        <div key={k} className="kv-row">
          <dt>{k.replace(/_/g, " ")}</dt>
          <dd className="mono">{typeof v === "string" ? v : JSON.stringify(v)}</dd>
        </div>
      ))}
    </dl>
  );
}
