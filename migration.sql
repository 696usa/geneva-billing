-- Geneva Billing — migration v4: keep both ticker and CUSIP on positions and activity
alter table positions add column if not exists cusip text not null default '';
alter table activity  add column if not exists cusip text not null default '';
create index if not exists positions_cusip on positions(cusip);
create index if not exists activity_cusip  on activity(cusip);
