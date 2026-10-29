# Pershing nightly feed

`pershing_pull.py` runs from the GitHub Action in `.github/workflows/pershing.yml`
every weekday morning. It downloads any ZIP in Pershing's SFTP /Inbox that has not
been processed yet, parses it, and writes to Supabase:

- positions  — one snapshot per data date for every Pershing account set up in the app
               (GCUS securities + cash, ECMB sweep balances; "Updated" GCUS deltas are
               overlaid on the previous snapshot, "Refreshed" files replace it)
- activity   — one batch per GACT file (buys/sells, sweeps, coupons, dividends...)
- feed_files — which files were processed; feed_log — a one-line summary per run

Accounts that exist in the files but not in the app are listed in the log; add them
under Clients (custodian Pershing, same account number) and they are picked up on the
next run. Re-run manually: Actions → Pershing nightly pull → Run workflow.

Secrets required (repo Settings → Secrets and variables → Actions):
  SUPABASE_URL, SUPABASE_SERVICE_KEY, PERSHING_SSH_KEY (contents of ~/.ssh/pershing-ecdsa)

Local test against downloaded files:  python feeds/pershing_pull.py --dry-run --local ~/pershing
