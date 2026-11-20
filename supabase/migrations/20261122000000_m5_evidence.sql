-- M5: evidence recorded by the harness (m5-contract §3 and §12 A1).
--
-- The M1 grants (authenticated: SELECT) and `evidence_select_own` cover the new columns;
-- writes stay backend-only. `output` holds the tail of a test run (or the head of the diff);
-- the full text lives in the task workspace (`output_ref`, relative to the workspace root).

alter table public.evidence drop constraint evidence_kind_check;
alter table public.evidence add constraint evidence_kind_check
  check (kind in ('baseline', 'baseline_suite', 'post', 'suite', 'harness_rerun', 'diff'));
alter table public.evidence
  add column source           text not null default 'harness'
                              check (source in ('harness', 'agent_run')),
  add column selector         text check (selector is null or length(selector) <= 300),
  add column output           text check (output is null or length(output) <= 20000),
  add column output_truncated boolean not null default false,
  add column failures         jsonb not null default '[]'::jsonb
                              check (jsonb_typeof(failures) = 'array'),
  add column workspace_digest text
                              check (workspace_digest is null or workspace_digest ~ '^[0-9a-f]{64}$'),
  add column tool_call_id     text,
  add column details          jsonb not null default '{}'::jsonb
                              check (jsonb_typeof(details) = 'object');
create index evidence_task_id_idx on public.evidence (task_id, id);

-- A1: an unplanned write in investigate becomes an approval of kind `readonly_violation`.
alter table public.approvals drop constraint approvals_kind_check;
alter table public.approvals add constraint approvals_kind_check
  check (kind in ('plan', 'command', 'guard_flag', 'readonly_violation'));
