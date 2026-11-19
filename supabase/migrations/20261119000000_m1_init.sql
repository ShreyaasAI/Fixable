-- M1: data model (implementation-plan §9), RLS, grants, append-only audit log.
--
-- Access model [D, 2026-11-19]:
--   * anon:          nothing.
--   * authenticated: SELECT on its own rows only (RLS). The browser can reach Supabase's REST API
--                    directly with the user's JWT, so no write may be possible on that path.
--                    The API also reads through this role (SET LOCAL ROLE + JWT claims).
--   * backend role:  (postgres / service_role) performs every write; the API takes user_id from
--                    the verified JWT and writes the audit row in the same transaction.
--   * github_tokens: RLS on, no policy, no grant: backend only.

-- ---------------------------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------------------------

create table public.profiles (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  github_login text,
  is_admin     boolean not null default false,
  disabled     boolean not null default false,
  created_at   timestamptz not null default now()
);

create table public.github_tokens (
  user_id         uuid primary key references public.profiles (user_id) on delete cascade,
  encrypted_token text not null,
  scopes          text[] not null default '{}',
  github_user_id  bigint not null,
  updated_at      timestamptz not null default now()
);

create table public.tasks (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references public.profiles (user_id) on delete cascade,
  repo            text not null check (repo ~ '^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$'),
  base_branch     text not null check (length(base_branch) between 1 and 255),
  description     text not null check (length(description) between 1 and 10000),
  failing_test    text check (failing_test is null or length(failing_test) between 1 and 500),
  status          text not null default 'created' check (status in (
                    'created', 'queued', 'preparing', 'investigating', 'awaiting_plan_approval',
                    'executing', 'awaiting_command_approval', 'awaiting_input', 'verifying',
                    'confirming', 'opening_pr', 'completed', 'rejected', 'failed',
                    'interrupted', 'cancelled')),
  phase           text check (phase is null or phase in (
                    'prepare', 'investigate', 'plan', 'execute', 'verify', 'confirm', 'pr')),
  reservation_mb  integer check (reservation_mb > 0),
  reservation_cpu numeric(4, 2) check (reservation_cpu > 0),
  cost_usd        numeric(10, 4) not null default 0 check (cost_usd >= 0),
  pr_url          text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index tasks_user_created_idx on public.tasks (user_id, created_at desc);
create index tasks_status_idx on public.tasks (status);

create table public.events (
  id         bigint generated always as identity primary key,
  task_id    uuid not null references public.tasks (id) on delete cascade,
  seq        integer not null check (seq >= 0),
  type       text not null,
  payload    jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (task_id, seq)
);

create table public.approvals (
  id         uuid primary key default gen_random_uuid(),
  task_id    uuid not null references public.tasks (id) on delete cascade,
  kind       text not null check (kind in ('plan', 'command', 'guard_flag')),
  payload    jsonb not null default '{}'::jsonb,
  status     text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  decided_by uuid references public.profiles (user_id) on delete set null,
  decided_at timestamptz,
  comment    text check (comment is null or length(comment) <= 5000),
  created_at timestamptz not null default now(),
  check ((status = 'pending') = (decided_at is null))
);
create index approvals_task_idx on public.approvals (task_id);

create table public.questions (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references public.tasks (id) on delete cascade,
  question    text not null check (length(question) between 1 and 10000),
  answer      text check (answer is null or length(answer) between 1 and 10000),
  answered_at timestamptz,
  created_at  timestamptz not null default now(),
  check ((answer is null) = (answered_at is null))
);
create index questions_task_idx on public.questions (task_id);

create table public.evidence (
  id          bigint generated always as identity primary key,
  task_id     uuid not null references public.tasks (id) on delete cascade,
  kind        text not null check (kind in ('baseline', 'post', 'suite', 'harness_rerun', 'diff')),
  command     text,
  exit_code   integer,
  output_ref  text,
  duration_ms integer check (duration_ms >= 0),
  created_at  timestamptz not null default now()
);
create index evidence_task_idx on public.evidence (task_id);

-- No foreign keys: audit rows outlive the users and tasks they describe.
create table public.audit_log (
  id         bigint generated always as identity primary key,
  user_id    uuid,
  action     text not null,
  target     text,
  payload    jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index audit_log_user_idx on public.audit_log (user_id, id);

create table public.quotas_usage (
  user_id  uuid not null references public.profiles (user_id) on delete cascade,
  day      date not null,
  tasks    integer not null default 0 check (tasks >= 0),
  cost_usd numeric(10, 4) not null default 0 check (cost_usd >= 0),
  primary key (user_id, day)
);

-- ---------------------------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------------------------

create function public.touch_updated_at() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger tasks_touch_updated_at
  before update on public.tasks
  for each row execute function public.touch_updated_at();

create function public.audit_log_append_only() returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'audit_log is append-only (% refused)', tg_op
    using errcode = 'insufficient_privilege';
end;
$$;

create trigger audit_log_no_update_delete
  before update or delete on public.audit_log
  for each row execute function public.audit_log_append_only();

create trigger audit_log_no_truncate
  before truncate on public.audit_log
  for each statement execute function public.audit_log_append_only();

-- ---------------------------------------------------------------------------------------------
-- Grants: undo Supabase's default "grant all to anon, authenticated", then grant reads only.
-- ---------------------------------------------------------------------------------------------

revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from public, anon, authenticated;

grant select on
  public.profiles, public.tasks, public.events, public.approvals, public.questions,
  public.evidence, public.audit_log, public.quotas_usage
to authenticated;

-- ---------------------------------------------------------------------------------------------
-- Row-level security (every user table; github_tokens deliberately has no policy)
-- ---------------------------------------------------------------------------------------------

alter table public.profiles      enable row level security;
alter table public.github_tokens enable row level security;
alter table public.tasks         enable row level security;
alter table public.events        enable row level security;
alter table public.approvals     enable row level security;
alter table public.questions     enable row level security;
alter table public.evidence      enable row level security;
alter table public.audit_log     enable row level security;
alter table public.quotas_usage  enable row level security;

create policy profiles_select_own on public.profiles
  for select to authenticated using (user_id = (select auth.uid()));

create policy tasks_select_own on public.tasks
  for select to authenticated using (user_id = (select auth.uid()));

create policy events_select_own on public.events
  for select to authenticated using (exists (
    select 1 from public.tasks t where t.id = task_id and t.user_id = (select auth.uid())));

create policy approvals_select_own on public.approvals
  for select to authenticated using (exists (
    select 1 from public.tasks t where t.id = task_id and t.user_id = (select auth.uid())));

create policy questions_select_own on public.questions
  for select to authenticated using (exists (
    select 1 from public.tasks t where t.id = task_id and t.user_id = (select auth.uid())));

create policy evidence_select_own on public.evidence
  for select to authenticated using (exists (
    select 1 from public.tasks t where t.id = task_id and t.user_id = (select auth.uid())));

create policy audit_log_select_own on public.audit_log
  for select to authenticated using (user_id = (select auth.uid()));

create policy quotas_usage_select_own on public.quotas_usage
  for select to authenticated using (user_id = (select auth.uid()));
