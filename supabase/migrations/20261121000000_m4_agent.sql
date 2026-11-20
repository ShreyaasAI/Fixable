-- M4: worker claim, leases, task events sequence, expiring approvals/questions
-- (m4-contract §3 and §12 A2).
--
-- The existing table grants and RLS cover the new columns (authenticated: SELECT own rows;
-- backend: writes). `events` (M1) is unchanged: `unique (task_id, seq)` already exists and
-- `seq` comes from `tasks.event_seq` (the task row lock serialises writers).
-- Lease times always come from the database clock (`now()`), never a worker's clock; an active
-- row with `lease_expires_at is null` counts as expired.

alter table public.tasks
  add column error            text check (error is null or length(error) <= 10000),
  add column worker_id        text,
  add column claimed_at       timestamptz,
  add column finished_at      timestamptz,
  add column event_seq        integer not null default 0 check (event_seq >= 0),
  add column lease_expires_at timestamptz,
  add column heartbeat_at     timestamptz,
  add check (worker_id is null or worker_id ~ '^[A-Za-z0-9_.-]{1,64}$');
create index tasks_queued_idx on public.tasks (created_seq) where status = 'queued';
create index tasks_lease_idx on public.tasks (lease_expires_at)
  where status not in ('created', 'queued', 'completed', 'rejected', 'failed', 'interrupted',
                       'cancelled');

alter table public.approvals drop constraint approvals_status_check;
alter table public.approvals add constraint approvals_status_check
  check (status in ('pending', 'approved', 'rejected', 'expired'));
create unique index approvals_one_pending_idx on public.approvals (task_id)
  where status = 'pending';

alter table public.questions
  add column source     text not null default 'agent' check (source in ('agent', 'system')),
  add column expired_at timestamptz,
  add constraint questions_answer_or_expiry check (not (answer is not null and expired_at is not null));
create unique index questions_one_open_idx on public.questions (task_id)
  where answer is null and expired_at is null;
