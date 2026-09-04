-- ============================================================
-- Geneva Billing — Supabase schema v2 (matches the web app)
-- Paste the whole file into Supabase SQL Editor and Run.
-- ============================================================

-- admin allow-list (single-advisor model)
create table if not exists app_admins (
  user_id uuid primary key references auth.users(id) on delete cascade,
  added_at timestamptz not null default now()
);
create or replace function is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from app_admins where user_id = auth.uid());
$$;
revoke all on function is_admin() from public;
grant execute on function is_admin() to authenticated;

create table clients (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  active boolean not null default true,
  notes text not null default '',
  created_at timestamptz not null default now()
);

create table accounts (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references clients(id) on delete restrict,
  custodian text not null,
  number text not null,
  nickname text not null default '',
  active boolean not null default true,
  created_at timestamptz not null default now()
);
create index on accounts(client_id);

create table fee_tiers (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references clients(id) on delete cascade,
  tier_min numeric(18,2) not null,
  tier_max numeric(18,2),
  annual_rate numeric(10,8) not null
);
create index on fee_tiers(client_id);

create table exclusions (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references clients(id) on delete cascade,
  account_id uuid references accounts(id) on delete cascade,
  sec_id text not null,
  note text not null default ''
);
create index on exclusions(client_id);

create table positions (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  as_of date not null,
  sec_id text not null,
  descr text not null default '',
  value numeric(18,2) not null
);
create index on positions(as_of);
create index on positions(account_id, as_of);
create index on positions(sec_id);

create table billing_runs (
  id uuid primary key default gen_random_uuid(),
  client_id uuid not null references clients(id) on delete restrict,
  client_name text not null,
  month text not null,              -- 'YYYY-MM'
  as_of date not null,
  days integer not null,
  gross numeric(18,2) not null,
  excluded numeric(18,2) not null,
  billable numeric(18,2) not null,
  annual_fee numeric(18,2) not null,
  fee numeric(18,2) not null,
  eff_rate numeric(12,10) not null,
  lines jsonb not null,
  excl_list jsonb not null,
  tiers jsonb not null,
  status text not null default 'draft' check (status in ('draft','final','void')),
  invoice_no text,
  created_at timestamptz not null default now(),
  finalized_at timestamptz
);
create index on billing_runs(client_id, month);
create unique index billing_runs_one_live on billing_runs(client_id, month) where status <> 'void';

create table invoices (
  id uuid primary key default gen_random_uuid(),
  no text not null unique,
  run_id uuid not null references billing_runs(id) on delete restrict,
  client_id uuid not null references clients(id),
  client_name text not null,
  month text not null,
  fee numeric(18,2) not null,
  billable numeric(18,2) not null,
  path text not null,               -- storage object path
  created_at timestamptz not null default now()
);

create table settings (
  key text primary key,
  value jsonb not null
);

-- finalized runs are immutable except -> void
create or replace function guard_final_run() returns trigger language plpgsql as $$
begin
  if old.status = 'final' and new.status = 'final' and
     (new.fee, new.billable, new.lines::text, new.tiers::text) is distinct from (old.fee, old.billable, old.lines::text, old.tiers::text) then
    raise exception 'Finalized billing run is immutable';
  end if;
  if old.status = 'final' and new.status = 'draft' then
    raise exception 'Cannot revert a finalized run to draft';
  end if;
  return new;
end $$;
create trigger trg_guard_final_run before update on billing_runs
  for each row execute function guard_final_run();

create or replace function block_final_delete() returns trigger language plpgsql as $$
begin
  if old.status <> 'draft' then raise exception 'Only draft runs can be deleted'; end if;
  return old;
end $$;
create trigger trg_block_final_delete before delete on billing_runs
  for each row execute function block_final_delete();

-- audit log
create table audit_log (
  id bigserial primary key,
  table_name text not null, row_id text, action text not null,
  old_row jsonb, new_row jsonb, actor uuid, at timestamptz not null default now()
);
create or replace function audit_trigger() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into audit_log(table_name,row_id,action,old_row,new_row,actor)
  values (tg_table_name, coalesce(new.id::text, old.id::text), tg_op,
          case when tg_op in ('UPDATE','DELETE') then to_jsonb(old) end,
          case when tg_op in ('INSERT','UPDATE') then to_jsonb(new) end, auth.uid());
  return coalesce(new, old);
end $$;
do $$ declare t text; begin
  foreach t in array array['clients','accounts','fee_tiers','exclusions','billing_runs','invoices'] loop
    execute format('create trigger audit_%1$s after insert or update or delete on %1$I for each row execute function audit_trigger()', t);
  end loop; end $$;

-- RLS: deny by default, admin only
do $$ declare t text; begin
  foreach t in array array['app_admins','clients','accounts','fee_tiers','exclusions','positions','billing_runs','invoices','settings','audit_log'] loop
    execute format('alter table %I enable row level security', t);
  end loop; end $$;
create policy admins_self on app_admins for select using (user_id = auth.uid());
do $$ declare t text; begin
  foreach t in array array['clients','accounts','fee_tiers','exclusions','positions','billing_runs','invoices','settings'] loop
    execute format('create policy %1$s_admin on %1$I for all using (is_admin()) with check (is_admin())', t);
  end loop; end $$;
create policy audit_read on audit_log for select using (is_admin());

-- private bucket for invoice PDFs
insert into storage.buckets (id, name, public) values ('invoices','invoices', false)
on conflict (id) do nothing;
create policy invoices_admin on storage.objects for all
  using (bucket_id = 'invoices' and is_admin()) with check (bucket_id = 'invoices' and is_admin());

-- ============================================================
-- After creating your login under Authentication > Users, run:
--   insert into app_admins(user_id) values ('<that user's UUID>');
-- ============================================================
