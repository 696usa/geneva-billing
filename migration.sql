-- Geneva Billing — migration v5: fee debit account per account
alter table accounts add column if not exists debit_account_id uuid references accounts(id) on delete set null;
alter table billing_runs add column if not exists debits jsonb not null default '[]'::jsonb;
