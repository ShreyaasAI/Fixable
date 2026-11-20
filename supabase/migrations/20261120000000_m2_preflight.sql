-- M2: pre-flight result on the task row (m2-contract §6).
--
-- The whole `PreflightResult` (decision, image, estimate, warnings, queue position at creation)
-- is stored so the UI can show why a task was rejected or queued. Table-level grants and RLS
-- from M1 already cover the new column (authenticated: SELECT own rows; backend: writes).

alter table public.tasks add column preflight jsonb;

-- Deterministic creation order: tasks created in one transaction share now(), and the
-- Docker Desktop VM clock can step, so "newest first" sorts by an identity sequence.
alter table public.tasks add column created_seq bigint generated always as identity;
create index tasks_user_seq_idx on public.tasks (user_id, created_seq desc);
revoke all on all sequences in schema public from anon, authenticated;
