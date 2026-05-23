"""Aggregate traffic stats via clash_api /connections diff polling.

sing-box 1.13 clash_api does NOT expose per-user identity for VLESS inbounds.
We therefore aggregate to a single daily total.
"""
import sqlite3
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = '/opt/proxy-bot/stats.db'
CLASH_URL = 'http://127.0.0.1:18090'
try:
    CLASH_SECRET = Path('/etc/sing-box/clash_secret').read_text().strip()
except Exception:
    CLASH_SECRET = ''
HEADERS = {'Authorization': f'Bearer {CLASH_SECRET}'}

# In-memory: { conn_id: (last_upload, last_download) }
_conn_state: dict = {}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS daily_total (
            date TEXT PRIMARY KEY,
            bytes_in INTEGER DEFAULT 0,
            bytes_out INTEGER DEFAULT 0
        );
    ''')
    conn.commit()
    conn.close()


def fetch_connections():
    try:
        r = httpx.get(f'{CLASH_URL}/connections', headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return []
        return r.json().get('connections', [])
    except Exception:
        return []


def update():
    init_db()
    conns = fetch_connections()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    current_ids = set()
    d_up_total = 0
    d_down_total = 0

    for c in conns:
        cid = c.get('id', '')
        if not cid:
            continue
        current_ids.add(cid)
        up = int(c.get('upload', 0))
        down = int(c.get('download', 0))
        prev = _conn_state.get(cid)
        if prev is None:
            d_up, d_down = up, down
        else:
            p_up, p_down = prev
            d_up = max(0, up - p_up)
            d_down = max(0, down - p_down)
        _conn_state[cid] = (up, down)
        d_up_total += d_up
        d_down_total += d_down

    for cid in list(_conn_state.keys()):
        if cid not in current_ids:
            del _conn_state[cid]

    if d_up_total or d_down_total:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            'INSERT INTO daily_total(date, bytes_in, bytes_out) VALUES(?,?,?) '
            'ON CONFLICT(date) DO UPDATE SET bytes_in=bytes_in+excluded.bytes_in, '
            'bytes_out=bytes_out+excluded.bytes_out',
            (today, d_down_total, d_up_total)
        )
        conn.commit()
        conn.close()


def online_count():
    conns = fetch_connections()
    src_ips = set()
    for c in conns:
        md = c.get('metadata') or {}
        ip = md.get('sourceIP', '')
        if ip:
            src_ips.add(ip)
    return len(conns), len(src_ips)


def human(n):
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.2f} {unit}" if unit != 'B' else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} PB"


def today_total():
    init_db()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        'SELECT bytes_in, bytes_out FROM daily_total WHERE date=?', (today,)
    ).fetchone()
    conn.close()
    return (row[0], row[1]) if row else (0, 0)


def today_summary():
    b_in, b_out = today_total()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if b_in == 0 and b_out == 0:
        return f"\U0001f4ca *Today ({today} UTC)*: no data yet"
    return (
        f"\U0001f4ca *Today ({today} UTC)*\n"
        f"  ↓ {human(b_in)}\n"
        f"  ↑ {human(b_out)}\n"
        f"  total {human(b_in + b_out)}"
    )


def range_summary(days):
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days-1)).strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        'SELECT date, bytes_in, bytes_out FROM daily_total WHERE date >= ? ORDER BY date',
        (cutoff,)
    ).fetchall()
    conn.close()
    if not rows:
        return f"\U0001f4ca No data in last {days} days"
    lines = [f"\U0001f4ca *Last {days} days (since {cutoff} UTC)*"]
    total_in = total_out = 0
    for date, b_in, b_out in rows:
        lines.append(f"  `{date}`: ↓{human(b_in)} ↑{human(b_out)}")
        total_in += b_in
        total_out += b_out
    lines.append(f"\n*Total*: ↓{human(total_in)} ↑{human(total_out)} ({human(total_in + total_out)})")
    return '\n'.join(lines)
