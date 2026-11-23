# M9 results

- Run id: run-20261123b
- Date: 2026-11-22
- Git sha: 1562d27
- Repo: ShreyaasAI/swf-pr-sandbox
- Host: NCPU 16, MemTotal 6216302592, runtime runc
- Model: @glm-4-7-flash/glm-4.7-flash
- Agent limits: step_limit 80, wall_time_limit_seconds 1800
- Budget used: $0.0, 463 calls, 19 PRs, 2763.6 s
- Abort reason: none
- Portkey usage: Portkey dashboard, user-reported 2026-11-23, hourly buckets (local time): 01:00 hour 87 requests, 11 LLM errors (HTTP 429), ~153k tokens (128k request, 24k response); 02:00 hour 129 requests, 5 LLM errors, ~200k request + 27k response tokens. Eval tasks ran 01:47-02:31, so the 01:00 bucket also holds the aborted run `run-20261123a` and the format-error probes. Dashboard requests (216) are fewer than the harness count (463 = 263 agent calls + 200 guard chunks); the reason is not established. Cost $0 (free models).

## Summary

| Metric | Single | Concurrent | Value | DoD | Met |
|---|---|---|---|---|---|
| Median per-task wall (s) | 90.3 | 114.1 | 1.26 | ≤ 1.25 | no |
| Median wall, completed only (s) | 90.3 | 105.8 | 1.17 | info | — |
| Resolve rate | 10/10 | 9/10 | 10 points | ≤ 10 | yes |
| Batch wall (s) | 1646.9 | 943.9 | 0.57 | info | — |
| Agent calls / guard chunks | 107 / 93 | 156 / 107 | — | info | — |
| Cost (booked $) | 0.0000 | 0.0000 | 0.0000 | ≤ $1 total | yes |

## Per task

| Pass | Seed | User | Status | Error | Wall s | Guard s | Agent calls | Retries | Approvals (plan/cmd/flag/ro) | Questions | Cost $ | PR | Tests touched | Files added | Target | Suite | Resolved | Peak MB |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| single | 01-paginate-off-by-one | eval-a | completed |  | 681.4 | 254.4 | 11 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/2 |  |  | 0 | 0 | yes | 30 |
| single | 02-mutable-default | eval-b | completed |  | 216.7 | 170.1 | 12 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/3 |  |  | 0 | 0 | yes | 18 |
| single | 03-none-last-name | eval-a | completed |  | 76.0 | 36.3 | 10 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/4 |  |  | 0 | 0 | yes | 30 |
| single | 04-average-int-division | eval-b | completed |  | 89.2 | 50.5 | 9 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/5 |  |  | 0 | 0 | yes | 38 |
| single | 05-top-scores-order | eval-a | completed |  | 78.4 | 34.4 | 11 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/6 |  |  | 0 | 0 | yes | 48 |
| single | 06-word-count-keyerror | eval-b | completed |  | 91.4 | 46.3 | 10 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/7 |  |  | 0 | 0 | yes | 36 |
| single | 07-email-plus-sign | eval-a | completed |  | 112.6 | 48.5 | 14 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/8 |  |  | 0 | 0 | yes | 39 |
| single | 08-iso-date-parse | eval-b | completed |  | 87.3 | 40.9 | 10 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/9 |  |  | 0 | 0 | yes | 24 |
| single | 09-slugify-spaces | eval-a | completed |  | 85.2 | 40.5 | 11 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/10 |  |  | 0 | 0 | yes | 45 |
| single | 10-clamp-upper-bound | eval-b | completed |  | 100.8 | 61.6 | 9 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/11 |  |  | 0 | 0 | yes | 31 |
| concurrent | 01-paginate-off-by-one | eval-a | failed | retries_exhausted | 473.3 | 188.1 | 53 | 13 | 1 / 0 / 0 / 0 | 1 | 0.0000 |  |  |  |  |  | no | 48 |
| concurrent | 02-mutable-default | eval-b | completed |  | 122.3 | 70.4 | 13 | 1 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/12 |  |  | 0 | 0 | yes | 49 |
| concurrent | 03-none-last-name | eval-a | completed |  | 155.1 | 114.0 | 9 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/17 |  |  | 0 | 0 | yes | 16 |
| concurrent | 04-average-int-division | eval-b | completed |  | 92.2 | 46.6 | 9 | 1 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/13 |  |  | 0 | 0 | yes | 23 |
| concurrent | 05-top-scores-order | eval-a | completed |  | 98.2 | 49.6 | 14 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/18 |  |  | 0 | 0 | yes | 24 |
| concurrent | 06-word-count-keyerror | eval-b | completed |  | 175.4 | 83.9 | 15 | 4 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/14 |  |  | 0 | 0 | yes | 8 |
| concurrent | 07-email-plus-sign | eval-a | completed |  | 82.5 | 39.1 | 10 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/19 |  |  | 0 | 0 | yes | 32 |
| concurrent | 08-iso-date-parse | eval-b | completed |  | 105.8 | 36.6 | 11 | 2 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/15 |  |  | 0 | 0 | yes | 24 |
| concurrent | 09-slugify-spaces | eval-a | completed |  | 124.1 | 51.7 | 12 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/20 |  |  | 0 | 0 | yes | 30 |
| concurrent | 10-clamp-upper-bound | eval-b | completed |  | 74.8 | 36.9 | 10 | 0 | 1 / 0 / 0 / 0 | 0 | 0.0000 | https://github.com/ShreyaasAI/swf-pr-sandbox/pull/16 |  |  | 0 | 0 | yes | 8 |

## Auto-approved guard flags

- none


## Controls

| Seed | Pristine target exit | Reference resolved |
|---|---|---|
| 01-paginate-off-by-one | 1 | yes |
| 02-mutable-default | 1 | yes |
| 03-none-last-name | 1 | yes |
| 04-average-int-division | 1 | yes |
| 05-top-scores-order | 1 | yes |
| 06-word-count-keyerror | 1 | yes |
| 07-email-plus-sign | 1 | yes |
| 08-iso-date-parse | 1 | yes |
| 09-slugify-spaces | 1 | yes |
| 10-clamp-upper-bound | 1 | yes |

## Replaced records

- none

## Notes

- DoD miss: median per-task wall ratio 1.26 vs ≤ 1.25. It comes from one task: concurrent 01 failed `retries_exhausted` after 473 s. In verify, after its tests passed, z-ai returned HTTP 429 "Rate limit reached for requests" three times in a row. The eval client does not answer `retry`, so the task failed. §0.13 does not list a provider 429 as an infra failure, so the seed was not re-run. Over completed tasks only, the ratio is 1.17.
- Concurrency also showed up as model-step retries: 21 in the concurrent pass, 0 in the single pass. The provider rate limit is the bottleneck under 2 concurrent users, not the host.
- The first live run (`run-20261123a`) was aborted after 2 tasks and 0 PRs: every task hit `repeated_format_error` (contract §13 A2). Fixed in 1562d27 before this run.
- The first two single tasks (681 s, 217 s) include cold-start guard latency (254 s, 170 s guard).
- Portkey guardrails dashboard (user, 2026-11-23): 688 requests "unscreened". Expected: Portkey saved guardrails are opt-in (m7 §13 A1) and `SWF_PORTKEY_CONFIG` was unset, so no request carried a guardrail config. All M9 screening was done by the in-process guard (glm judge on issue, AGENTS.md and tool observations, plus regex output checks): 200 judged chunks, 0 flags. The judge calls themselves also appear in Portkey as unscreened requests. Turning Portkey guardrails on by default is still deferred (m7 §12) and is an M10 decision.
- `swf_eval report` to stdout fails on a Windows cp1252 console (`≤`). Use `--write` or `PYTHONIOENCODING=utf-8`.

## PR cleanup log

- 2026-11-23: PRs #2-#20 on ShreyaasAI/swf-pr-sandbox (the 19 eval PRs) closed, not merged. Their `agent/<task-id>` branches deleted.
- PR #1 (older live test from 2026-11-21, branch `agent/580616c5-...`) left open; not part of the eval.
- The 10 `seed/*` branches were deleted after the user's M9 approval (2026-11-23).
