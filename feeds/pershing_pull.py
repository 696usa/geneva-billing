#!/usr/bin/env python3
"""Nightly Pershing SFTP pull for Geneva Billing.

Downloads new standard files (GCUS, ECMB, GACT, ISCA, ACCT) from Pershing's SFTP
inbox, parses them per Pershing's fixed-width layouts, and writes positions and
activity into Supabase for the accounts set up in the billing app.

Environment:
  SUPABASE_URL, SUPABASE_SERVICE_KEY   - Supabase project + service-role key
  PERSHING_HOST, PERSHING_PORT, PERSHING_USER, PERSHING_SSH_KEY (private key text)
Flags:
  --dry-run       parse local files, print what would be written, write nothing
  --local DIR     use files already in DIR instead of SFTP
"""
import os, sys, io, re, json, zipfile, argparse, datetime as dt, uuid
import requests

# ---------------------------------------------------------------- helpers
def num(s, dec):
    s = s.strip()
    return int(s) / (10 ** dec) if s.isdigit() else 0.0

def sgn(c):
    return -1 if c == '-' else 1

OVER = {'{': ('+', 0), '}': ('-', 0)}
for i, ch in enumerate('ABCDEFGHI', 1): OVER[ch] = ('+', i)
for i, ch in enumerate('JKLMNOPQR', 1): OVER[ch] = ('-', i)
def overpunch(s, dec):
    """Zoned-decimal with sign in the last character (Pershing ISCA)."""
    s = s.strip()
    if not s: return 0.0
    last = s[-1]
    if last in OVER:
        sign, d = OVER[last]; digits = s[:-1] + str(d)
    else:
        sign, digits = '+', s
    if not digits.isdigit(): return 0.0
    v = int(digits) / (10 ** dec)
    return -v if sign == '-' else v

def ccyymmdd(s):
    s = s.strip()
    if len(s) != 8 or not s.isdigit() or s == '00000000': return None
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"

def header_date(line):
    m = re.search(r'DATA OF\s+(\d\d)/(\d\d)/(\d{4})', line)
    return f"{m.group(3)}-{m.group(1)}-{m.group(2)}" if m else None

def is_refreshed(line):
    return 'REFRESHED' in line[100:140]

# ---------------------------------------------------------------- parsers
def parse_gcus(text):
    lines = text.splitlines()
    date = header_date(lines[0]); refreshed = is_refreshed(lines[0])
    rows = []
    for ln in lines:
        if ln[:3] != 'GCA': continue
        acct10 = ln[11:21]; acct = acct10[:9]; atype = acct10[9]
        cusip = ln[21:30].strip(); cs = ln[53]
        qty = num(ln[73:91], 5) * sgn(ln[91])
        if qty == 0 and ln[91] != '+' and ln[91] != '-':
            qty = num(ln[92:110], 5) * sgn(ln[110])
        val = num(ln[664:682], 3) * sgn(ln[682])
        if ln[682] not in '+-': val = num(ln[713:731], 3) * sgn(ln[731])
        sym = ln[434:443].strip()
        desc = ' '.join(x.strip() for x in (ln[537:557], ln[557:577], ln[577:597]) if x.strip())
        rows.append(dict(acct=acct, atype=atype, cusip=cusip, cash=(cs == 'C'), sym=sym, desc=desc, qty=qty, value=val))
    return date, refreshed, rows

def parse_ecmb(text):
    lines = text.splitlines(); date = header_date(lines[0]); rows = []
    for ln in lines:
        if ln[:3] != 'ECA': continue
        rows.append(dict(acct=ln[11:20], fund=ln[55:59].strip(), cusip=ln[242:251].strip(),
                         desc=ln[254:284].strip(), value=num(ln[98:116], 2) * sgn(ln[116])))
    return date, rows

SOURCE_TYPES = {'MMK': 'Money market sweep', 'MCS': 'Coupon/interest', 'DIV': 'Dividend', 'INT': 'Interest',
                'JNL': 'Journal', 'CHK': 'Check', 'WIR': 'Wire', 'ACH': 'ACH', 'FEE': 'Fee', 'MGN': 'Margin interest'}
def parse_gact(text):
    lines = text.splitlines(); date = header_date(lines[0]); A = {}; B = {}
    for ln in lines:
        if ln[:3] == 'GAA':
            seq = ln[3:11]
            A[seq] = dict(acct=ln[11:21][:9], cusip=ln[21:30].strip(), sym=ln[47:63].strip(), ttype=ln[69], bs=ln[70],
                          src=ln[74:77].strip(), tdate=ccyymmdd(ln[89:97]), sdate=ccyymmdd(ln[97:105]), pdate=ccyymmdd(ln[81:89]),
                          cancel=ln[138] == '1', ref=ln[114:120].strip(),
                          qty=num(ln[145:163], 5) * sgn(ln[163]), price=num(ln[164:182], 9),
                          net=num(ln[191:209], 3) * sgn(ln[209]))
        elif ln[:3] == 'GAB':
            seq = ln[3:11]; n = int(ln[353:355] or 0)
            B[seq] = ' '.join(ln[357 + 20 * i:377 + 20 * i].strip() for i in range(min(n, 12)) if ln[357 + 20 * i:377 + 20 * i].strip())
    rows = []
    for seq, a in A.items():
        if a['ttype'] in 'TS' and a['bs'] == 'B': typ = 'Buy'
        elif a['ttype'] in 'TS' and a['bs'] == 'S': typ = 'Sell'
        else: typ = SOURCE_TYPES.get(a['src'], a['src'] or 'Other')
        if a['cancel']: typ = 'Cancel ' + typ
        rows.append(dict(acct=a['acct'], date=a['tdate'] or a['sdate'] or a['pdate'] or date, type=typ, cusip=a['cusip'],
                         sym=a['sym'], desc=B.get(seq, ''), qty=a['qty'] or None,
                         amount=-a['net'] if a['net'] else None,  # Pershing: + = debit; app: money in = +
                         ref=a['ref']))
    return date, rows

def parse_isca(text):
    sec = {}
    for ln in text.splitlines():
        t = ln[:1]; cu = ln[7:16]
        if t == 'A': sec.setdefault(cu, {})['sym'] = ln[61:71].strip(); sec[cu]['stype'] = ln[16]
        elif t == 'C': sec.setdefault(cu, {})['desc'] = ' '.join(x.strip() for x in (ln[21:41], ln[41:61], ln[61:81]) if x.strip())
        elif t == 'F': sec.setdefault(cu, {})['price'] = overpunch(ln[74:92], 9)
    return sec

# ---------------------------------------------------------------- supabase
class DB:
    def __init__(self, url, key):
        self.url = url.rstrip('/') + '/rest/v1'; self.h = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    def get(self, table, params):
        out = []; off = 0
        while True:
            r = requests.get(f"{self.url}/{table}", headers={**self.h, 'Range': f'{off}-{off+999}'}, params=params); r.raise_for_status()
            d = r.json(); out += d
            if len(d) < 1000: return out
            off += 1000
    def insert(self, table, rows):
        for i in range(0, len(rows), 500):
            r = requests.post(f"{self.url}/{table}", headers={**self.h, 'Prefer': 'return=minimal'}, data=json.dumps(rows[i:i+500])); r.raise_for_status()
    def delete(self, table, params):
        r = requests.delete(f"{self.url}/{table}", headers=self.h, params=params); r.raise_for_status()
    def upsert(self, table, rows, on):
        r = requests.post(f"{self.url}/{table}", headers={**self.h, 'Prefer': 'resolution=merge-duplicates,return=minimal'}, params={'on_conflict': on}, data=json.dumps(rows)); r.raise_for_status()

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--dry-run', action='store_true'); ap.add_argument('--local'); args = ap.parse_args()
    dry = args.dry_run
    db = None if dry else DB(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])

    # 1. fetch files
    files = {}  # name -> bytes
    if args.local:
        for f in sorted(os.listdir(args.local)):
            if f.upper().endswith('.ZIP'): files[f] = open(os.path.join(args.local, f), 'rb').read()
    else:
        import paramiko
        key = paramiko.ECDSAKey.from_private_key(io.StringIO(os.environ['PERSHING_SSH_KEY']))
        t = paramiko.Transport((os.environ['PERSHING_HOST'], int(os.environ.get('PERSHING_PORT', '2222'))))
        t.connect(username=os.environ['PERSHING_USER'], pkey=key); sftp = paramiko.SFTPClient.from_transport(t)
        done = set() if dry else {r['name'] for r in db.get('feed_files', {'select': 'name', 'custodian': 'eq.Pershing'})}
        for f in sftp.listdir('/Inbox'):
            if f.upper().endswith('.ZIP') and f not in done:
                with sftp.open('/Inbox/' + f, 'rb') as fh: files[f] = fh.read()
        sftp.close(); t.close()
    if not files: print('No new files.'); return
    print('Files:', ', '.join(sorted(files)))

    # unzip + group by data date
    texts = {}
    for name, data in files.items():
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.namelist():
                texts[name] = z.read(member).decode('latin-1')
    bydate = {}
    for name, txt in texts.items():
        kind = name.upper().split('.')[-2]
        d = header_date(txt.splitlines()[0])
        bydate.setdefault(d, {})[kind] = (name, txt)
    isca = {}
    for d in sorted(bydate):
        if 'ISCA' in bydate[d]: isca.update(parse_isca(bydate[d]['ISCA'][1]))

    # accounts in the app (Pershing custodian), keyed by normalized number
    norm = lambda s: re.sub(r'[^0-9A-Z]', '', s.upper())
    if dry:  # pretend every account in the files is set up, so the parse can be inspected end to end
        seen = set()
        for kinds in bydate.values():
            for k in ('GCUS', 'ECMB', 'GACT'):
                if k in kinds:
                    for ln in kinds[k][1].splitlines():
                        if ln[:3] in ('GCA', 'ECA', 'GAA'): seen.add(ln[11:20])
        accts = [dict(id='acct-' + n, number=n, client_id='c', custodian='Pershing', active=True) for n in sorted(seen)]
    else:
        accts = db.get('accounts', {'select': 'id,number,client_id,custodian,active'})
    by_num = {norm(a['number']): a for a in accts}

    log = []
    for d in sorted(bydate):
        kinds = bydate[d]
        # ---- positions: GCUS + ECMB
        if 'GCUS' in kinds or 'ECMB' in kinds:
            new_pos = {}  # (acct, sec) -> row
            carry = {}
            if 'GCUS' in kinds:
                _, refreshed, rows = parse_gcus(kinds['GCUS'][1])
                if not refreshed and not dry:
                    # carry forward prior snapshot for Pershing accounts, then overlay today's changes
                    pid = [a['id'] for a in accts if a['custodian'] == 'Pershing']
                    if pid:
                        prev = db.get('positions', {'select': 'account_id,as_of,sec_id,cusip,descr,value', 'as_of': f'lt.{d}', 'account_id': 'in.(' + ','.join(pid) + ')', 'order': 'as_of.desc'})
                        if prev:
                            last = prev[0]['as_of']
                            for p in prev:
                                if p['as_of'] == last and p['sec_id'] != 'BNYFDIC':
                                    carry[(p['account_id'], p['cusip'] or p['sec_id'])] = dict(account_id=p['account_id'], as_of=d, sec_id=p['sec_id'], cusip=p['cusip'], descr=p['descr'], value=p['value'])
                unknown = set()
                for r in rows:
                    a = by_num.get(norm(r['acct']))
                    if not a:
                        unknown.add(r['acct']); continue
                    key = (a['id'], r['cusip'])
                    if r['cash']:
                        sec = 'CASH'; cusip = ''; desc = 'Cash (' + r['cusip'][:3] + ')'
                    else:
                        info = isca.get(r['cusip'], {}); sec = r['sym'] or info.get('sym') or r['cusip']; cusip = r['cusip']; desc = r['desc'] or info.get('desc', '')
                    if r['qty'] == 0 and r['value'] == 0:
                        carry.pop(key, None); continue
                    carry[key] = dict(account_id=a['id'], as_of=d, sec_id=sec, cusip=cusip, descr=desc, value=round(r['value'], 2))
                if unknown: log.append(f"{d} GCUS: {len(unknown)} account(s) not set up in app: {', '.join(sorted(unknown)[:8])}")
                new_pos.update(carry)
            if 'ECMB' in kinds:
                _, rows = parse_ecmb(kinds['ECMB'][1])
                for r in rows:
                    a = by_num.get(norm(r['acct']))
                    if not a: continue
                    sec = (r['fund'] + 'FDIC') if 'FDIC' in r['cusip'].upper() else (isca.get(r['cusip'], {}).get('sym') or r['cusip'] or r['fund'])
                    new_pos[(a['id'], sec)] = dict(account_id=a['id'], as_of=d, sec_id=sec, cusip=r['cusip'] if r['cusip'].isalnum() else '', descr=r['desc'], value=round(r['value'], 2))
            rows_out = list(new_pos.values())
            ids = sorted({r['account_id'] for r in rows_out})
            log.append(f"{d}: positions {len(rows_out)} rows across {len(ids)} accounts, total {sum(r['value'] for r in rows_out):,.2f}")
            if dry:
                for r in rows_out[:12]: print('  POS', r)
            elif ids:
                for i in range(0, len(ids), 200):
                    db.delete('positions', {'as_of': f'eq.{d}', 'account_id': 'in.(' + ','.join(ids[i:i+200]) + ')'})
                db.insert('positions', rows_out)
        # ---- activity: GACT
        if 'GACT' in kinds:
            _, rows = parse_gact(kinds['GACT'][1]); out = []; batch_id = str(uuid.uuid4())
            for r in rows:
                a = by_num.get(norm(r['acct']))
                if not a: continue
                info = isca.get(r['cusip'], {})
                out.append(dict(id=str(uuid.uuid4()), batch_id=batch_id, account_id=a['id'], trade_date=r['date'], act_type=r['type'],
                                sec_id='CASH' if r['cusip'].startswith('USD') else (r['sym'] or info.get('sym') or r['cusip'] or ''), cusip=r['cusip'] if r['cusip'] and not r['cusip'].startswith('USD') else '',
                                descr=(r['desc'] or info.get('desc', ''))[:200], amount=r['amount'], quantity=r['qty']))
            log.append(f"{d}: activity {len(out)} rows (of {len(rows)} in file)")
            if dry:
                for r in out[:8]: print('  ACT', {k: r[k] for k in ('trade_date', 'act_type', 'sec_id', 'quantity', 'amount')})
            elif out:
                db.insert('activity_batches', [dict(id=batch_id, name=f'Pershing GACT {d}', row_count=len(out))])
                db.insert('activity', out)
        # ---- record processed files
        if not dry:
            db.upsert('feed_files', [dict(custodian='Pershing', name=kinds[k][0], data_date=d, processed_at=dt.datetime.utcnow().isoformat(), note='') for k in kinds], 'custodian,name')
    for l in log: print(l)
    if not dry and log:
        db.insert('feed_log', [dict(custodian='Pershing', message='\n'.join(log))])

if __name__ == '__main__':
    main()
