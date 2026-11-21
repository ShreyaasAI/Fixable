-- M8: global kill switch (singleton). Backend-only: no grant, RLS on, no policy.
create table public.system_state (
  id           boolean primary key default true check (id),
  tasks_paused boolean not null default false,
  updated_at   timestamptz not null default now(),
  updated_by   uuid
);
insert into public.system_state default values;
alter table public.system_state enable row level security;
revoke all on public.system_state from anon, authenticated;
