# Web Coding Agent — Problem Specification (v2)

- **Purpose:** Define *what* the MVP must do and *why*, precisely enough that a new session can plan and build it without the original transcript.
- **Last updated:** 2026-11-19
- **Status:** Approved (v2, 2026-11-19 — supersedes v1). Zero-cost constraint applied 2026-11-19
- **Related artifacts:** `docs/software-factory/research-plan.md` (Approved) · `docs/software-factory/implementation-plan.md` (Under review) · `PROMPT.md` (process rules) · `system_prompts/Cursor_*` (agent prompt baseline)

Labels: **[D]** confirmed decision · **[Q]** open question · **[A]** assumption to validate · **[R]** risk/constraint · **[N]** non-goal.

---

## 0. Product vision vs MVP

**Vision [D]:** a web-based coding agent and IDE, "like Lovable but for any software system, not only websites". Users sign in, connect GitHub, browse repos in the browser (like github.dev), and have the agent work on them. Memories and preferences are stored in the cloud. Plans: Free and Pro (uploads > 200 MB need Pro). Voice input.

**MVP [D]:** a narrow slice of that vision — **sign in with GitHub → pick a repo → describe a bug or failing test → approve the plan → receive a GitHub PR with evidence.** Everything else in the vision is in the §5 roadmap.

## 1. Objective

A hosted web application in an **open beta** (anyone can sign in with GitHub, protected by abuse controls) in which a signed-in developer selects one of their GitHub repositories and submits a **failing test or bug report**. An agent (built on mini-swe-agent, extended with additional tools) works in a **per-task Docker container** holding a clone of the repo:
1. It investigates read-only and submits a bounded plan for approval.
2. After approval it edits code and runs verification, asking for approval on non-allowlisted commands and whenever a guardrail flags something, and asking questions when unsure.
3. It must produce **evidence** (diff, commands, test output showing the target test fails before and passes after, no new failures in the `AGENTS.md` suite) **before** it may claim completion.

The harness re-runs the target test once to confirm. The backend applies the diff to a separate clean clone, pushes a new branch and **opens a PR**. A human reviews and merges it in GitHub only.

## 2. Primary user and workflow

- **User [D]:** any developer (open beta) with a GitHub account and repos they can write to (or open PRs on).
- **Job-to-be-done [D]:** "This test fails / this bug exists — find the cause, fix it, prove it, and give me a PR."
- **Workflow:** Sign in with GitHub → choose repo + base branch → describe the task (optional failing test id) → see the pre-flight resource estimate → watch investigation → approve the plan → approve flagged/risky commands, answer questions → review the evidence → the PR is opened → review/merge in GitHub.

## 3. Web application experience (MVP minimum; UI polish deferred, backend/API first)

| User action | Data shown / required |
|---|---|
| Sign in | GitHub OAuth via Supabase Auth; open beta; quota usage visible |
| Pick repo | Repos the user's GitHub authorization can access; base branch |
| Create task | Description, optional failing test id |
| Pre-flight | Estimated memory/CPU/time; warning **before start** if above the threshold or capacity [D] |
| Watch task | Live event stream: phase, model messages, tool calls, outputs, exit codes |
| Approve/reject plan | Plan, files to touch, `AGENTS.md` allowlist extensions |
| Approve/reject command | Exact tool call, why it was flagged (classifier or guardrail) |
| Answer question | Agent question; the answer is appended to the history |
| Review evidence | Diff, before/after test runs, suite result, harness confirmation |
| Result | PR link |

## 4. In scope (MVP)

1. Hosted **open beta**; Supabase (Postgres + Auth, GitHub OAuth); per-user data isolation via row-level security [D].
1a. **Abuse controls [D]:** per-user quotas (tasks/day, cost cap per task and per day), **gVisor** container runtime, API rate limits, admin kill switch (per user and global). Quota values [Q].
2. GitHub integration with the **OAuth user token** from sign-in (encrypted at rest, backend-only, never in the container): list repos, clone, open a PR from a new branch `agent/<task-id>` [D].
3. One task type: fix a failing test / bug [D].
4. `AGENTS.md`: prose → agent prompt; fenced ```` ```yaml agent-config ```` block → image, setup, test command(s), allowlist extensions, resource hints [D].
5. Phases: `prepare → investigate (read-only) → plan (approval) → execute → verify → review → PR` [D].
6. Multi-tool agent (bash plus dedicated tools, including `ask_user` and `submit_plan`) [D].
7. Command classifier: allowlisted → run; other → approval; forbidden → blocked [D].
8. Guardrails [D]:
   - **Input:** a free LLM (`zai/glm-4.7-flash`) screens for prompt injection; Guardrails AI checks for PII, toxicity and malicious intent (hacking, trespassing).
   - **Output:** Portkey guardrails apply regex matching and JSON schema validation to model output.
   - **On a flag:** tell the user and require approval before the next command.
9. Models [D]:
   - **Reasoning:** `opencode_go/beam-free` (investigate + plan).
   - **Tool execution:** `zai/glm-4.7-flash` (execute + verify).
   - **Gateway:** Portkey as the unified gateway.
10. Evidence produced by the agent before any completion claim; the harness re-runs the target test once before the PR [D] (harness re-run proposed and not objected to).
11. Pre-flight resource estimation and admission control: notify the user **before** the task starts [D].
12. **Zero infrastructure cost [D]:** execution host = **Oracle Cloud Always Free Arm VM (2 OCPU / 12 GB total, arm64)**; database/auth = **Supabase Free** (500 MB DB, pauses after ~7 days of low activity); free LLM models. Concurrency: **2 tasks at once**, each limited to **4 GB / 1 OCPU**; further tasks queue [D].
13. Limits: steps, cost, wall time; any failing step retries ≤ 3 times, then asks the user [D].
14. Append-only audit log per user [D].
15. Implementation language: **Python** for backend/agent/harness; SQL for the database; the frontend may use another language [D].

## 5. Out of scope for the MVP (roadmap) [N]

| Roadmap item | Notes |
|---|---|
| Web IDE: file browser, editor, terminal view | Vision item; after the MVP |
| Cloud-stored memories and preferences | Vision item; PROMPT.md also defers memory |
| Free/Pro plans, billing, 200 MB upload limit | Vision item |
| Voice input (`scaleway/openai/whisper-large-v3`) | Vision item |
| Task types beyond bug fix (features, any system type) | Vision item |
| Billing / paid plans for open users | Quotas only in the MVP |
| GitLab/Bitbucket | — |
| Merging, pushing to non-`agent/*` branches, deploy, production access | **Never automatic** |
| Multi-agent orchestration, connectors | Later stage |
| Automatic resume after restart | — |

## 6. mini-swe-agent integration

Verified facts (2026-11-19): a linear `DefaultAgent` loop; `PortkeyModel` uses native tool calling with one `BASH_TOOL`; `DockerEnvironment` uses `docker exec` with no network config; `InteractiveAgent` approvals are stdin-bound with a prefix-regex whitelist. Details in research-plan §2.

| Component | Treatment |
|---|---|
| Agent loop, linear history, trajectory JSON, limits | **Reuse** (pip dependency, pinned) |
| Model class | **Extend**: multiple tools; switch model by phase (reasoning ↔ flash) |
| Environment | **Extend** `DockerEnvironment`: dispatch tools, hardening, egress proxy, read-only backstop |
| Approvals / ask / plan | **Replace** stdin logic with web-backed tools |
| Whitelist | **Replace** with a parser-based classifier |
| CLI, Textual UI, inspector | **Replace** with web API + event stream |
| System prompt | **Adapt** behavioural rules from `system_prompts/Cursor_*` |

## 7. Environment

| Resource | Available to agent? |
|---|---|
| Repo clone (task branch) | Yes — mounted into the container |
| Image / setup / test commands | From the `AGENTS.md` YAML block; if missing, the agent asks [D] |
| Network egress | Only allowlisted package registries via an egress proxy [D] |
| GitHub token / push / PR API | **No** — the backend only [D] |
| Host filesystem, Docker socket, other users' tasks | **No** [D] |
| Package cache | On the execution host, expires after 1 day [D] |
| Workspaces | Kept until the user deletes them [D] |

## 8. Autonomy and permissions

| Category | Actions | Rule |
|---|---|---|
| Autonomous | Read/search files, allowlisted test/lint commands, `git status/diff/log` | Logged |
| Approval (task submitter only) [D] | The plan; non-allowlisted commands; the next command after any guardrail flag | Blocking prompt; decision + user + time + payload audited |
| Forbidden [D] | `rm -rf` (any form), DB drops, push/merge, egress outside the allowlist, reading credentials, Docker/host access | Blocked by the classifier **and** the environment; `AGENTS.md` cannot override |
| Read-only phase [D] | Classifier + workspace-diff backstop | Change detected → task blocked, flagged |
| Escalation | Ambiguity, missing access/info, low confidence, 3 failed retries | `ask_user` |
| PR review [D] | Human, in GitHub only | The agent never merges |

## 9. Task lifecycle

1. **Create** → **Pre-flight:** estimate resources from `AGENTS.md` hints + repo size; if above the per-task threshold or current capacity, tell the user before starting (queue or reject) [D].
2. **Prepare:** the backend clones (token never in the workspace), parses `AGENTS.md`, starts the container, runs setup (≤ 3 retries).
3. **Investigate** (reasoning model, read-only): reproduce the failure → baseline evidence.
4. **Plan:** `submit_plan` → `awaiting_plan_approval`; a rejection returns the comment.
5. **Execute** (flash model): edits + commands; approvals and questions as needed.
6. **Verify:** the agent runs the target test and the suite, records evidence; completion is refused without it.
7. **Confirm:** the harness re-runs the target test once.
8. **PR:** the backend applies the diff to a clean clone, pushes `agent/<task-id>`, opens a PR whose body contains the evidence summary → `completed`.

Other end states: `rejected`, `failed` (with partial evidence), `interrupted` (restart), `cancelled`.

## 10. Success criteria

- [ ] Across **10 seeded bug repos**, each task can end `completed` with a PR whose evidence shows the target test failing before and passing after, with no new `AGENTS.md`-suite failures.
- [ ] Completion without an evidence bundle is impossible; the harness re-run agrees with the agent's evidence.
- [ ] No workspace change before plan approval (backstop proves it).
- [ ] Every non-allowlisted or guardrail-flagged next command waits for approval; rejected ones never execute.
- [ ] Container egress outside the allowlist fails; the GitHub token is unreadable in the container.
- [ ] PRs only target `agent/<task-id>` → base; no merge API call exists in the code.
- [ ] User A cannot read user B's tasks/events (RLS tests).
- [ ] Pre-flight warns before start when the estimate exceeds the threshold.
- [ ] 2 concurrent tasks: wall time ≤ 1.25× single-task; resolve-rate drop ≤ 10%.
- [ ] A user exceeding a quota is refused before the task starts; the kill switch stops running tasks within 30 s [P].
- [ ] Seeded injection/PII/malicious inputs are flagged; malformed model output is caught by Portkey schema/regex checks.

## 11. Failure and limitation behavior

- Ambiguity or missing info → ask; never guess scope.
- Missing access/permission → stop, state exactly what is needed; no workarounds (no disabling tests or weakening assertions).
- Any failing step → ≤ 3 retries, then ask.
- Limits hit → `failed` with partial evidence and what is unverified.
- Cannot reproduce → report with evidence; no speculative fix without approval.
- Resources insufficient → tell the user before the start, not mid-task.

## 12. Open questions [Q]

1. Quota values — proposed defaults: 5 tasks/user/day, $1 cost cap per task, $3 per user per day, max 1 concurrent task per user [P].
2. Quota values above were proposed; accepted as defaults with the spec approval (2026-11-19).

Resolved 2026-11-19: open beta + abuse controls (replaces invite-only); GitHub access = OAuth user token; per-task 4 GB / 2 vCPU; guard model `zai/glm-4.7-flash`; host = Oracle Always Free (2 tasks); Supabase Free.

## 13. Assumptions [A]

1. `zai/glm-4.7-flash` and `opencode_go/beam-free` are reachable through Portkey (required for Portkey output guardrails), support tool calling, and can share one linear history across a model switch.
2. `glm-4.7-flash` executes plans well enough to meet the resolve-rate criteria.
3. Guardrails AI PII/toxicity validators run in-process at acceptable latency.
4. Resource needs can be estimated well enough before start from `AGENTS.md` hints + repo metadata.
5. Target repos' tests run in the declared image within the time limit.
6. Sending repo code to the configured providers is approved for beta users' repos.

## 14. Risks [R]

| Risk | Type | Mitigation direction |
|---|---|---|
| Agent plants git hooks/config that would run with the token | Security | The backend applies only the diff to a separate clean clone, hooks disabled [D] |
| Classifier bypass | Security | Parser-based; unparseable → approval; the sandbox is the boundary |
| Open-beta users run untrusted code; cost abuse | Security/cost | gVisor, hardening flags, no secrets in the sandbox, quotas, rate limits, kill switch |
| Indirect prompt injection via repo content | Security | Input guardrail + approval on flag; detection ≠ prevention |
| Agent-only evidence is wrong or cherry-picked | Correctness | Harness re-runs the target test |
| Free/flash models are weak or rate-limited | Quality/ops | Measure on the 10 seeded repos; model is swappable via config |
| Model switch breaks tool-call history format | Reliability | Spike early (research RQ) |
| Pre-flight estimate wrong → OOM mid-task | Ops/usability | Conservative defaults; hard container limits; clear failure message |
| Long approval waits occupy a worker slot | Ops | Accept for MVP; only 2 slots → idle-approval timeout [P] |
| Free tiers: Oracle idle reclamation / capacity, Supabase pausing and 500 MB cap, Portkey free-plan limits | Ops | Heartbeat activity; event retention/compaction; verify Portkey plan features [A] |
| arm64 host: some images/packages are x86-only | Reliability | Require multi-arch images in `AGENTS.md`; pre-flight checks image architecture |
| OAuth user token has broad, long-lived `repo` scope | Security | Encrypt at rest; backend-only; request the minimal scope; revoke on sign-out/delete |
| Guard model = executor model family; one injection may fool both | Security | Approval on flag; sandbox remains the boundary |
| Working rule: tests first, then infra/implementation | Process [D] | Every milestone starts with failing tests |
