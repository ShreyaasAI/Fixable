# Web Coding Agent — Research Plan (v2)

- **Purpose:** Record what is known, what must be validated, the architecture options with trade-offs, and the recommended minimal architecture and milestones for the MVP in the problem specification.
- **Last updated:** 2026-11-20
- **Status:** Approved (v2, 2026-11-19, via the user's go-ahead to implement — supersedes v1)
- **Related artifacts:** `docs/software-factory/problem-specification.md` (v2, Approved — source of truth for scope) · `docs/software-factory/implementation-plan.md` (Under review) · `PROMPT.md` · `system_prompts/Cursor_*`

Labels: **[D]** decision (from the spec) · **[V]** verified on 2026-11-19 · **[U]** unverified · **[P]** proposal awaiting approval.

---

## 1. Research questions

| # | Question | Why | How to resolve |
|---|---|---|---|
| RQ1 | Can `beam-free` (reasoning) and `glm-4.7-flash` (tools) share one linear tool-call history through Portkey? | Phase-based model switch [D] | Spike: 2-phase scripted task; check the message format survives the switch |
| RQ2 | Are both models reachable through Portkey with tool calling, and does litellm cost tracking know them? | Output guardrails run on Portkey; cost limits | One call each; inspect `cost`; registry JSON if missing |
| RQ3 | Does `glm-4.7-flash` meet the resolve-rate target on the 10 seeded repos? | Quality [A2] | Eval in milestone M9 |
| RQ4 | Can an agent subclass block for minutes–hours on a web approval inside `DefaultAgent.run`? | Approvals, `ask_user` | Spike: wait on a Postgres row |
| RQ5 | Do `DockerEnvironment` + `run_args` give mount, egress proxy, limits, non-root, `cap-drop`? | Sandbox | Sandbox test suite |
| RQ6 | How robust is a bash-parser classifier against chaining, `eval`, `python -c`? | Approval boundary | Adversarial corpus |
| RQ7 | Latency/accuracy of the input guardrails (free-LLM injection check; Guardrails AI PII/toxicity/malicious intent)? | Every input passes through them | Benchmark on seeded + clean corpus |
| RQ8 | What do Portkey output guardrails offer for regex + JSON schema checks on tool-call arguments, and how do they fail (block vs flag)? | Output guardrail [D] | Portkey docs + spike |
| RQ9 | How accurately can memory/CPU be estimated before start? | Pre-flight [D] | Compare estimates vs measured peaks on the seeded repos |
| RQ10 | ~~GitHub App vs OAuth~~ — resolved: OAuth user token [D] | — | Prototype clone + PR with the user token in M6 |

## 2. Already known

### 2.1 mini-swe-agent [V]
- `DefaultAgent`: linear loop `query → execute_actions → add_messages` until a message has role `exit`. Config: templates, `step_limit`, `cost_limit`, `wall_time_limit_seconds`, `max_consecutive_format_errors`. Control flow via `InterruptAgentFlow`, `LimitsExceeded`, `FormatError`, `Submitted`.
- `PortkeyModel`: `portkey_ai` SDK, `PORTKEY_API_KEY` / `PORTKEY_VIRTUAL_KEY`; **native tool calling with one `BASH_TOOL`**; `parse_toolcall_actions`; litellm cost calculation (raises on failure unless `ignore_errors`).
- `DockerEnvironment`: `docker exec … bash -lc`; no network settings; `run_args` is the only hook; background cleanup.
- `InteractiveAgent`: stdin approvals, `re.match` prefix whitelist — **not reused**.
- MIT license.

### 2.2 LiteLLM providers [V, partial]
- The `zai/`, `opencode_go/` and `scaleway/` provider prefixes exist in the LiteLLM docs. The specific model names were not listed on the providers page → **[U]**.

### 2.3 Supabase guidance [V — keyword-extracted portions of the docs only]
- Supabase limits **database connections** by compute size; it does not limit agent containers.
- A persistent backend should use a direct connection or the session-mode pooler + an app-side pool; the transaction pooler is for serverless.
- **Implication:** task concurrency is decided by the execution host's CPU/RAM via our own admission control. Supabase only needs a small pool per worker.

### 2.4 Guardrails AI hub [V, partial]
- Relevant validators: PII (likely local, Presidio-based [U]), toxic language (ML), secrets (rule-based); injection validators are model-based. The user reports its injection accuracy is overstated → injection checks go to a free LLM instead [D].

### 2.5 Harness guidance (Anthropic) [V]
- Agents declare victory early → evidence before completion; prefer end-to-end checks; keep progress outside the context.

### 2.6 Cursor system prompt [V]
- Reuse its behavioural rules (no fabrication, gather info when unsure, root-cause fixes, ≤ 3 attempts on the same error). Its tool set inspires ours but is not copied.

## 3. Assumptions to validate (spec §13)

| Spec assumption | RQ | Exit criterion |
|---|---|---|
| A1 Models via Portkey, tool calling, model switch | RQ1, RQ2 | Scripted 2-phase task completes; costs recorded |
| A2 flash model quality | RQ3 | Meets the M9 criteria |
| A3 Guardrails AI latency | RQ7 | p95 overhead per observation ≤ 500 ms [P] |
| A4 Pre-flight estimate | RQ9 | Measured peak ≤ estimate on ≥ 9/10 seeded repos [P] |
| A5 Tests run in the image | RQ5 | 10/10 seeded repos set up |

## 4. Agent design within mini-swe-agent

### 4.1 Tools [P]
All tools except `ask_user` and `submit_plan` execute **inside the container** (implemented via `docker exec`), so the sandbox stays the single boundary.

| Tool | Phase | Approval |
|---|---|---|
| `bash(command)` | all | Classifier decides |
| `read_file(path, range)` | all | No |
| `search(pattern, path)` / `list_dir(path)` | all | No |
| `edit_file(path, old, new)` / `write_file(path, content)` | execute | No (plan approved); blocked in investigate |
| `run_tests(selector)` — runs the `AGENTS.md` test command | all | No |
| `submit_plan(plan)` — JSON schema | investigate | Pauses for user |
| `ask_user(question)` | all | Pauses for user |
| `record_evidence(kind, ref)` | verify | No |
| `complete_task(summary)` | verify | Refused unless the evidence bundle is complete |

### 4.2 Extension points
| Point | Use |
|---|---|
| Subclass the model | Tool schemas, per-phase model (beam-free ↔ glm-4.7-flash), Portkey config with guardrails |
| Subclass `DefaultAgent.execute_actions` | Dispatch tools → classifier → guardrail → approval wait → execute; emit events |
| Subclass `add_messages` | Persist messages as events |
| `InterruptAgentFlow` | Return approvals, rejections and answers into the history |
| Subclass `DockerEnvironment` | Hardening `run_args`, proxy network, read-only backstop |

### 4.3 Limitations
No persistent shell (`cd`/`export` don't persist) → state it in the prompt · orphan containers → reconcile by label · one model per agent → model subclass handles the switch.

## 5. Prior art to study [U]

| Source | What to learn |
|---|---|
| OpenHands | Event stream, confirmation mode, security analyzer |
| GitHub Copilot coding agent | Own branch prefix, PR as the output, egress firewall |
| OpenAI Codex cloud | Setup phase with network, agent phase with an allowlist |
| Lovable / Bolt / Replit Agent | Vision-level UX (roadmap, not MVP) |
| SWE-bench harness | FAIL_TO_PASS / PASS_TO_PASS evidence model |
| AGENTS.md convention | Compatibility |
| Portkey guardrails docs | Regex/JSON-schema checks, deny vs flag behaviour (RQ8) |

## 6. Security and permissions

1. **Token isolation [D]:** GitHub tokens live only in the backend; the clone remote never embeds a token; nothing credential-like is mounted.
2. **Clean-clone PR [D]:** the backend exports the diff (hooks off, `core.fsmonitor=false`), applies it in a separate clean clone, commits with `core.hooksPath=/dev/null`, pushes `agent/<task-id>`, opens the PR via the API. The branch name is computed server-side. No merge call exists.
3. **Tenant isolation [D]:** Supabase RLS on every user table; per-task container and workspace; workers use a service role and enforce `user_id` in code; tests for cross-user access.
4. **Egress [D]:** internal Docker network → proxy with a registry allowlist; 1-day package cache on the host.
5. **Classifier [D]:** bash parsed per simple command; unparseable, `eval`, subshells, interpreter `-c` → approval; `rm -rf` and DB drops always forbidden.
6. **Read-only backstop [D]:** workspace snapshot at investigate start; any change at `submit_plan` → block.
7. **Guardrails [D]:**
   - Input (issue text, `AGENTS.md`, tool observations): a free LLM checks for injection; Guardrails AI checks PII, toxicity and malicious intent.
   - Output (model responses/tool args): Portkey regex + JSON schema.
   - Any flag → event + user approval before the next command.
8. **Container hardening:** non-root, `--cap-drop ALL`, `no-new-privileges`, pids/memory/CPU limits, no Docker socket.
9. **Open beta + abuse controls [D]:** GitHub OAuth via Supabase Auth, open sign-up; per-user quotas (tasks/day, cost per task/day, 1 concurrent task per user [P]); API rate limits; admin kill switch; **gVisor (`runsc`) runtime required in the MVP**.

## 7. Architecture options

### 7.1 Embedding mini-swe-agent
| Option | Verdict |
|---|---|
| **Library in a worker process, subclassed** | **Recommended** — direct hooks, upstream kept |
| CLI subprocess + trajectory tailing | Reject — no approval hook |
| Fork | Reject — merge burden |

### 7.2 Queue and concurrency
| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Postgres table queue (`FOR UPDATE SKIP LOCKED`) on Supabase + admission control** | One datastore; transactional with task state | Polling/LISTEN code | **Recommended** |
| Supabase Queues (pgmq) | Managed queue semantics | Visibility timeouts fit poorly with hours-long approval waits | Alternative |
| Celery/Redis | Mature | Extra infra | Defer |

**Admission control [D]:** each task gets a reservation (estimated RAM/CPU, max 4 GB / 1 OCPU). A worker claims a task only if `free_capacity ≥ reservation`. Above the per-task threshold → user told at creation; above current capacity → queued with the position shown. Host = Oracle Always Free Arm (2 OCPU / 12 GB) → **2 concurrent tasks**.

### 7.3 Live updates
**SSE** from the API reading the `events` table (LISTEN/NOTIFY) — recommended. Alternative: Supabase Realtime directly to the browser with RLS (fewer API pieces; evaluate in M1).

### 7.4 Sandbox
Docker + internal network + egress proxy + hardening + **gVisor runtime** — required because the beta is open [D].

### 7.5 GitHub auth
| Option | Pros | Cons | Verdict |
|---|---|---|---|
| GitHub App | Per-repo installation, short-lived tokens, fine-grained permissions | App setup | Not chosen |
| **OAuth user token** | Simple; reuses the sign-in | Broad, long-lived `repo` scope | **Chosen [D]** — encrypt at rest (Supabase Vault or app-level KMS), backend-only, revoke on account deletion |

## 8. Recommended minimal architecture [P]

```
Browser ──REST/SSE──▶ API (FastAPI, Python)
                        │ Supabase Auth (GitHub OAuth, open beta), quotas/rate limits, tasks, approvals, events, audit
                        ▼
                Supabase Postgres (RLS) ◀── state/events ──┐
                        ▲ claim if capacity ≥ reservation   │
                 Worker processes (execution host)          │
                 ├─ Preflight: resource estimate, admission
                 ├─ GitHubService (OAuth user token): clone, clean-clone apply, push agent/<id>, open PR
                 ├─ AgentConfigLoader: AGENTS.md prose + yaml agent-config
                 ├─ WebAgent(DefaultAgent): phases, tools, approvals, retries ≤3, events
                 ├─ PhaseModel: beam-free (investigate/plan) ↔ glm-4.7-flash (execute/verify) via Portkey
                 │     └─ Portkey output guardrails (regex, JSON schema)
                 ├─ InputGuard: free-LLM injection check + Guardrails AI (PII, toxicity, malicious intent)
                 ├─ CommandClassifier (bash parser)
                 ├─ SandboxEnv(DockerEnvironment): tools, hardening, read-only backstop
                 └─ EvidenceService: bundle check + harness re-run of the target test
                              │
        Docker task container ── internal net ── egress proxy (registry allowlist, 1-day cache)
```

## 9. MVP milestones (tests written first in each [D])

| ID | Milestone | Acceptance |
|---|---|---|
| M0 | Skeleton, CI (pytest, ruff, mypy), 10 seeded bug repos with `AGENTS.md` + reference fixes | CI green; each seeded repo has a reproducible failing test |
| M1 | Supabase schema + RLS, API, GitHub sign-in, audit log | Cross-user access tests fail as expected; audit row per action |
| M2 | Sandbox + egress proxy + pre-flight/admission control | Egress blocked except registries; no token in the container; over-threshold task warned before start |
| M3 | Command classifier + phase policy | 0 forbidden bypasses on the adversarial corpus; unparseable → approval |
| M4 | WebAgent: tools, approvals, `ask_user`, `submit_plan`, model switch, retries, events | Fake-model tests for every transition; one live 2-model smoke run (RQ1/RQ2) |
| M5 | Evidence + harness re-run + read-only backstop | `complete_task` refused without evidence; the backstop catches a write in investigate |
| M6 | GitHub (OAuth token): clone, clean-clone apply, push, open PR | Hooks never run; branch fixed server-side; PR created on a test repo; no merge call |
| M7 | Guardrails: input (free LLM + Guardrails AI) and output (Portkey) | Seeded injection/PII/malicious corpus flagged; approval required after a flag; malformed output caught |
| M8 | Abuse controls: quotas, rate limits, kill switch, gVisor runtime | Over-quota task refused pre-start; rate limit returns 429; kill switch stops tasks ≤ 30 s; sandbox tests pass under `runsc` |
| M9 | Concurrency + eval on the 10 seeded repos | ≤ 1.25× wall time at 2 tasks; resolve-rate drop ≤ 10% |
| M10 | Minimal web UI for the spec §3 actions | Every action doable in the browser |

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Git hook/config execution with the token | Clean-clone apply, hooks disabled |
| Open-beta untrusted code and cost abuse | gVisor, hardening, quotas, rate limits, kill switch (M8) |
| Free/flash model quality or rate limits | Eval early (M4 smoke, M9); model names in config |
| Model switch breaks history | RQ1 spike in M4 before building on it |
| Guardrail false positives → approval fatigue | Tune thresholds on the clean corpus (M7) |
| Pre-flight underestimates | Conservative default reservation; hard limits; clear OOM message |
| litellm cost unknown for the new models | Registry JSON / own token accounting |
| Long approval waits hold capacity | Accept for MVP; release-and-rehydrate is deferred |

## 11. Deferred (roadmap)

Web IDE (browser, editor, terminal) · cloud memory/preferences · Free/Pro plans, billing, 200 MB uploads · voice input (Scaleway Whisper) · non-bugfix task types · GitLab/Bitbucket · multi-agent · resume after restart.

## 12. Open questions for the user [Q]

1. Live updates via SSE (default) or Supabase Realtime?
2. Does Portkey's free plan include output guardrails (regex/JSON schema)? If not → enforce the same checks in-process [A].

Resolved 2026-11-19: quotas 5 tasks/day, $1/task, $3/day, 1 concurrent per user; zero-cost hosting = Oracle Always Free Arm (2 OCPU/12 GB, 2 concurrent tasks) + Supabase Free.

Resolved 2026-11-19: open beta + abuse controls; OAuth user token; 4 GB / 1 OCPU (= 1 vCPU on Arm) per task on the Oracle Always Free host (2 OCPU / 12 GB, 2 concurrent tasks; corrected 2026-11-20, user); injection guard = `zai/glm-4.7-flash`.
