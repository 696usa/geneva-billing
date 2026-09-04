-- ============================================================
-- Geneva Billing — migration v3: activity upload + deletions
-- Run in Supabase SQL Editor AFTER schema.sql (v2).
-- ============================================================

-- deleting a client removes its accounts (and their positions/exclusions/activity)
alter table accounts drop constraint if exists accounts_client_id_fkey;
alter table accounts add constraint accounts_client_id_fkey
  foreign key (client_id) references clients(id) on delete cascade;

create table if not exists activity_batches (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  row_count integer not null default 0,
  uploaded_at timestamptz not null default now()
);

create table if not exists activity (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references activity_batches(id) on delete cascade,
  account_id uuid not null references accounts(id) on delete cascade,
  trade_date date not null,
  act_type text not null,
  sec_id text not null default '',
  descr text not null default '',
  amount numeric(18,2),
  quantity numeric(18,6)
);
create index if not exists activity_date on activity(trade_date);
create index if not exists activity_acct on activity(account_id, trade_date);
create index if not exists activity_sec on activity(sec_id);
create index if not exists activity_type on activity(act_type);

alter table activity_batches enable row level security;
alter table activity enable row level security;
create policy activity_batches_admin on activity_batches for all using (is_admin()) with check (is_admin());
create policy activity_admin on activity for all using (is_admin()) with check (is_admin());

-- allow deleting voided runs (finalized ones stay protected)
create or replace function block_final_delete() returns trigger language plpgsql as $$
begin
  if old.status = 'final' then raise exception 'Finalized runs cannot be deleted; void first'; end if;
  return old;
end $$;
