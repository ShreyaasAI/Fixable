import { ExternalLink } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, TERMINAL, api, type EvidenceItem, type Task } from "../api";
import { NOISE_TYPES } from "../events";
import { cn, shortId } from "../lib/utils";
import { messageFor } from "../messages";
import { go } from "../nav";
import { streamEvents, type StreamEvent } from "../sse";
import {
  Alert,
  Button,
  Checkbox,
  ConfirmDialog,
  ExitStamp,
  Label,
  Panel,
  Stamp,
  StatusStamp,
  Textarea,
} from "../ui";

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
    <div className="flex flex-col gap-7 md:gap-8">
      <div className="flex flex-col gap-3">
        <p className="font-mono text-[13px] text-muted">
          <a href="#/" className="no-underline hover:text-fg">
            Tasks
          </a>
          <span aria-hidden="true"> / </span>
          {shortId(taskId)}
        </p>
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-4">
          <h1 className="line-clamp-3 min-w-0 max-w-3xl flex-1 basis-72 font-display text-[26px] font-extrabold leading-[1.15] tracking-[-0.015em] md:text-4xl">
            {task?.description || `Task ${shortId(taskId)}`}
          </h1>
          {!terminal && status && (
            <Button
              kind="danger-outline"
              data-testid="cancel-button"
              onClick={() => setConfirm("cancel")}
            >
              Cancel task
            </Button>
          )}
          {terminal && (
            <Button
              kind="danger-outline"
              data-testid="delete-button"
              onClick={() => setConfirm("delete")}
            >
              Delete
            </Button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <span
            aria-live="polite"
            data-testid="task-status"
            data-status={status}
            className="inline-flex items-center"
          >
            <span className="sr-only">Status: </span>
            <StatusStamp status={status || "loading"} animate />
          </span>
          {task && (
            <span className="min-w-0 break-all font-mono text-[13px] text-muted">
              {task.repo} @ {task.base_branch}
              {task.failing_test ? ` · ${task.failing_test}` : ""}
            </span>
          )}
          {typeof task?.cost_usd === "number" && (
            <span className="font-mono text-[13px] text-muted">
              ${task.cost_usd.toFixed(2)} spent
            </span>
          )}
        </div>
      </div>

      {error && <Alert testId="page-error">{error}</Alert>}

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_360px] lg:gap-8">
        <aside
          aria-label="Decisions and results"
          className="flex min-w-0 flex-col gap-5 empty:hidden lg:sticky lg:top-6 lg:col-start-2 lg:row-start-1"
        >
          <AnimatePresence initial={false}>
            {pending.approvals.map((a) => (
              <ApprovalCard key={a.id} taskId={taskId} approval={a} />
            ))}
            {pending.questions.map((q) => (
              <QuestionCard key={q.id} taskId={taskId} question={q} />
            ))}
          </AnimatePresence>
          {task?.error && (
            <Panel className="border-danger/60 p-5" aria-label="Error">
              <p className="eyebrow !text-danger">Stopped with an error</p>
              <p className="mt-2 break-words font-mono text-sm" data-testid="task-error">
                {task.error}
              </p>
            </Panel>
          )}
          {prUrl && (
            <a
              href={prUrl}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="pr-link"
              className="flex items-center justify-between gap-3 rounded-sm border border-ok/70 bg-surface px-5 py-4 no-underline transition-colors duration-150 hover:bg-raised"
            >
              <span className="flex min-w-0 flex-col gap-1">
                <span className="eyebrow !text-ok">Pull request</span>
                <span className="truncate font-mono text-sm">{prUrl}</span>
              </span>
              <ExternalLink aria-hidden="true" className="size-4 shrink-0 text-ok" />
            </a>
          )}
          {evidence && evidence.length > 0 && (
            <Panel aria-labelledby="evidence-title" className="px-5 pb-1 pt-5">
              <h2 id="evidence-title" className="eyebrow">
                Evidence
              </h2>
              <ul data-testid="evidence-list" className="mt-2 divide-y divide-line">
                {evidence.map((item) => (
                  <li
                    key={String(item.id)}
                    data-testid="evidence-item"
                    className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3"
                  >
                    <span className="font-mono text-[13px]">{item.kind.replace(/_/g, " ")}</span>
                    <span className="text-[13px] text-muted">{item.source}</span>
                    {item.exit_code !== null && (
                      <span className="ml-auto">
                        <ExitStamp code={item.exit_code} />
                      </span>
                    )}
                    {item.command && (
                      <code className="w-full truncate font-mono text-xs text-muted">
                        {item.command}
                      </code>
                    )}
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </aside>

        <section
          aria-labelledby="timeline-title"
          className="flex min-w-0 flex-col lg:col-start-1 lg:row-start-1"
        >
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line pb-3">
            <h2 id="timeline-title" className="eyebrow">
              Log
            </h2>
            <div className="flex items-center gap-2.5">
              <Checkbox
                id="noise-toggle"
                data-testid="noise-toggle"
                checked={showNoise}
                onCheckedChange={(v) => setShowNoise(v === true)}
              />
              <Label htmlFor="noise-toggle" className="font-normal">
                Show internal events
              </Label>
              <span className="font-mono text-xs text-muted" data-testid="noise-hidden-count">
                ({hidden} hidden)
              </span>
            </div>
          </div>
          <ol data-testid="timeline" className="relative">
            <span
              aria-hidden="true"
              className="absolute bottom-4 left-[2.95rem] top-4 w-px bg-line md:left-[3.2rem]"
            />
            {visible.map((e) => (
              <EventRow key={e.seq} event={e} />
            ))}
          </ol>
        </section>
      </div>

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

/** A card that needs the user: amber-ruled, springs in when it arrives. */
function DecisionCard(props: {
  eyebrow: string;
  title: string;
  titleId: string;
  children: ReactNode;
  [data: `data-${string}`]: string;
}) {
  const { eyebrow, title, titleId, children, ...data } = props;
  return (
    <motion.section
      layout
      initial={{ opacity: 0, y: -10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, height: 0, marginTop: 0, overflow: "hidden" }}
      transition={{ type: "spring", stiffness: 420, damping: 32 }}
      aria-labelledby={titleId}
      className="flex flex-col gap-3.5 rounded-sm border-[1.5px] border-warn bg-surface p-5"
      {...data}
    >
      <p className="eyebrow !text-warn">{eyebrow}</p>
      <h2 id={titleId} className="font-display text-xl font-extrabold leading-snug">
        {title}
      </h2>
      {children}
    </motion.section>
  );
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
    <DecisionCard
      eyebrow="Needs your OK"
      title={approvalTitle(approval.kind)}
      titleId={`approval-${approval.id}`}
      data-testid="approval-card"
      data-approval-id={approval.id}
      data-kind={approval.kind}
    >
      <ApprovalBody kind={approval.kind} payload={approval.payload} />
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={commentId}>Comment (optional)</Label>
        <Textarea
          id={commentId}
          data-testid="approval-comment"
          rows={2}
          className="min-h-16"
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Button
          kind="primary"
          data-testid="approve-button"
          disabled={busy}
          onClick={() => void decide("approve")}
        >
          Approve
        </Button>
        <Button
          kind="secondary"
          data-testid="reject-button"
          disabled={busy}
          onClick={() => void decide("reject")}
        >
          Reject
        </Button>
      </div>
      {note && <p className="font-mono text-xs text-muted">{note}</p>}
    </DecisionCard>
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
      <div className="flex flex-col gap-3 text-[15px]">
        {typeof payload.summary === "string" && <p>{payload.summary}</p>}
        {steps.length > 0 && (
          <ol className="flex flex-col gap-1.5">
            {steps.map((s, i) => (
              <li key={i} className="flex gap-3">
                <span className="font-mono text-[13px] text-accent">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span>{String(s)}</span>
              </li>
            ))}
          </ol>
        )}
        {files.length > 0 && (
          <p className="text-sm text-muted">
            Files: <span className="break-all font-mono text-fg">{files.map(String).join(", ")}</span>
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
    <DecisionCard
      eyebrow="The agent asks"
      title={question.question}
      titleId={`q-${question.id}`}
      data-testid="question-card"
      data-question-id={question.id}
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={inputId}>Your answer</Label>
        <Textarea
          id={inputId}
          data-testid="answer-input"
          rows={2}
          className="min-h-16"
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
        />
      </div>
      <Button
        kind="primary"
        data-testid="answer-button"
        disabled={busy || !answer.trim()}
        onClick={() => void send()}
      >
        Answer
      </Button>
      {note && <p className="font-mono text-xs text-muted">{note}</p>}
    </DecisionCard>
  );
}

// --- timeline rows -------------------------------------------------------------------------

const RAIL: Record<string, string> = {
  status: "bg-muted",
  tool_call: "bg-accent",
  tool_result: "bg-accent/60",
  message: "bg-info",
  pr_opened: "bg-ok",
  completed: "bg-ok",
  approval_requested: "bg-warn",
  question_asked: "bg-warn",
};

function Tag({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "font-mono text-xs font-medium uppercase tracking-[0.06em] text-muted",
        className,
      )}
    >
      {children}
    </span>
  );
}

function EventRow({ event }: { event: StreamEvent }) {
  const d = event.data;
  let body;
  switch (event.type) {
    case "status":
      body = (
        <div className="flex flex-wrap items-center gap-2">
          <Tag>status</Tag>
          <StatusStamp status={String(d.to)} />
          {typeof d.error === "string" && (
            <span className="font-mono text-[13px] text-muted">{d.error}</span>
          )}
        </div>
      );
      break;
    case "message":
      body = (
        <div className="flex flex-col gap-1.5">
          <Tag className="text-info">{String(d.role ?? "message")}</Tag>
          {typeof d.content === "string" && d.content && (
            <Output text={d.content} testId="message-output" prose />
          )}
        </div>
      );
      break;
    case "tool_call":
      body = (
        <div className="flex flex-col gap-1.5">
          <Tag className="text-accent">
            call · <span className="normal-case">{String(d.tool)}</span>
          </Tag>
          <ToolArgs args={d.args} />
        </div>
      );
      break;
    case "tool_result":
      body = (
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <Tag>
              result · <span className="normal-case">{String(d.tool)}</span>
            </Tag>
            {typeof d.returncode === "number" && <ExitStamp code={d.returncode} />}
            {d.truncated === true && <span className="text-xs text-muted">(truncated)</span>}
          </div>
          <Output text={String(d.output ?? "")} testId="tool-output" />
        </div>
      );
      break;
    case "pr_opened":
      body = (
        <div className="flex flex-col gap-1">
          <Tag className="text-ok">pull request opened</Tag>
          <a
            href={String(d.url)}
            target="_blank"
            rel="noopener noreferrer"
            className="break-all font-mono text-sm text-ok underline underline-offset-4"
          >
            {String(d.url)}
          </a>
        </div>
      );
      break;
    case "completed":
      body = (
        <div className="flex flex-col gap-1.5">
          <Stamp label="completed" tone="ok" className="w-fit" />
          {d.summary != null && <KeyValues data={summaryData(d.summary)} skip={[]} />}
        </div>
      );
      break;
    default:
      body = (
        <div className="flex flex-col gap-1">
          <Tag>{event.type.replace(/_/g, " ")}</Tag>
          <KeyValues data={d} skip={["created_at"]} />
        </div>
      );
  }
  return (
    <motion.li
      initial={{ opacity: 0, x: -6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="relative grid grid-cols-[2.2rem_1.5rem_minmax(0,1fr)] gap-x-1.5 py-3 md:grid-cols-[2.4rem_1.6rem_minmax(0,1fr)]"
      data-testid="event-row"
      data-seq={event.seq}
      data-type={event.type}
    >
      <span className="pt-0.5 text-right font-mono text-xs text-muted" aria-hidden="true">
        {String(event.seq).padStart(3, "0")}
      </span>
      <span aria-hidden="true" className="flex justify-center pt-1.5">
        <span
          className={cn(
            "relative z-10 size-2 rotate-45 rounded-[1px] ring-4 ring-bg",
            RAIL[event.type] ?? "bg-line",
          )}
        />
      </span>
      <div className="min-w-0">{body}</div>
    </motion.li>
  );
}

function summaryData(summary: unknown): Record<string, unknown> {
  return summary && typeof summary === "object"
    ? (summary as Record<string, unknown>)
    : { summary };
}

const PRE =
  "overflow-x-auto rounded-sm border border-line bg-raised px-3.5 py-3 font-mono text-[13px] leading-[1.6] text-fg";

function ToolArgs({ args }: { args: unknown }) {
  if (args && typeof args === "object" && "command" in args) {
    return <pre className={PRE}>{String((args as { command: unknown }).command)}</pre>;
  }
  if (args && typeof args === "object" && Object.keys(args).length > 0) {
    return <KeyValues data={args as Record<string, unknown>} skip={[]} />;
  }
  return null;
}

/** Output over 40 lines shows its first 40, with a toggle for the rest (m10a §2). */
function Output({ text, testId, prose = false }: { text: string; testId: string; prose?: boolean }) {
  const [open, setOpen] = useState(false);
  const lines = text.split("\n");
  const short = lines.length <= COLLAPSE_LINES;
  const expanded = short || open;
  const shown = expanded ? text : lines.slice(0, COLLAPSE_LINES).join("\n");
  return (
    <div data-testid={testId} data-expanded={String(expanded)} className="flex flex-col gap-2">
      {prose ? (
        <p className="whitespace-pre-wrap break-words text-[15px] leading-relaxed">{shown}</p>
      ) : (
        <pre className={PRE}>{shown}</pre>
      )}
      {!short && (
        <Button
          kind="link"
          data-testid="output-toggle"
          aria-expanded={open}
          className="w-fit"
          onClick={() => setOpen((o) => !o)}
        >
          {open ? "Show first 40 lines" : `Show all ${lines.length} lines`}
        </Button>
      )}
    </div>
  );
}

function KeyValues({ data, skip }: { data: Record<string, unknown>; skip: string[] }) {
  const entries = Object.entries(data).filter(([k, v]) => !skip.includes(k) && v != null);
  if (entries.length === 0) return null;
  return (
    <dl className="grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-1 text-[13px]">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k.replace(/_/g, " ")}</dt>
          <dd className="break-words font-mono">
            {typeof v === "string" ? v : JSON.stringify(v)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
