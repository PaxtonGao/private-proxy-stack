#!/usr/bin/env python3
"""Proxy Manager Bot - 仅响应 owner chat_id"""
import json
import logging
import subprocess
import sqlite3
import os
import re
import ssl
import socket
import hashlib
import tarfile
import httpx
from pathlib import Path
from datetime import datetime, timezone, timedelta, time as dtime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler, filters,
)

import stats

CONFIG = json.loads(Path('/opt/proxy-bot/config.json').read_text())
OWNER = CONFIG['owner_chat_id']

logging.basicConfig(level=logging.WARNING,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('proxy-bot')


def owner_only(handler):
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != OWNER:
            return
        return await handler(update, ctx)
    return wrapped


def reply(update):
    """Return the message object to reply to (works for both /commands and button clicks)."""
    return update.effective_message


def run(cmd, timeout=30):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


# ============ basic commands ============

@owner_only
async def cmd_start(update, ctx):
    await reply(update).reply_text(
        "Proxy Manager 已就绪。\n\n"
        "发 /menu 弹出按钮面板（推荐）\n"
        "发 /help 查看所有命令"
    )


@owner_only
async def cmd_status(update, ctx):
    rc1, sb = run(['systemctl', 'is-active', 'sing-box'])
    rc2, caddy = run(['systemctl', 'is-active', 'caddy'])
    rc3, since = run(['systemctl', 'show', '-p', 'ActiveEnterTimestamp', 'sing-box'])
    rc4, mem = run(['free', '-h'])
    started = since.split('=', 1)[-1] if '=' in since else '未知'
    sb_zh = '运行中' if sb == 'active' else f'❌ {sb}'
    caddy_zh = '运行中' if caddy == 'active' else f'❌ {caddy}'
    msg = (
        f"🟢 *服务状态*\n"
        f"sing-box：`{sb_zh}`\n"
        f"caddy：`{caddy_zh}`\n"
        f"启动时间：`{started}`\n\n"
        f"*内存*\n```\n{mem}\n```"
    )
    await reply(update).reply_markdown(msg)


@owner_only
async def cmd_list(update, ctx):
    rc, out = run(['/opt/proxy-sub/gen.sh', 'list'])
    if not out:
        await reply(update).reply_text("（暂无用户）")
    else:
        await reply(update).reply_text("用户列表：\n" + out)


# ============ traffic / online ============

@owner_only
async def cmd_traffic(update, ctx):
    args = ctx.args or []
    if not args:
        await reply(update).reply_markdown(stats.today_summary())
    elif args[0] == 'week':
        await reply(update).reply_markdown(stats.range_summary(7))
    elif args[0] == 'month':
        await reply(update).reply_markdown(stats.range_summary(30))
    else:
        await reply(update).reply_text("用法：/traffic [week|month]（已不再支持按用户细分）")


@owner_only
async def cmd_online(update, ctx):
    n_conn, n_ip = stats.online_count()
    if n_conn == 0:
        await reply(update).reply_text("👤 当前无人在线")
    else:
        await reply(update).reply_markdown(
            f"👤 *当前在线*\n  活跃连接：{n_conn}\n  来源 IP 数：{n_ip}"
        )


async def periodic_stats(ctx):
    try:
        stats.update()
    except Exception as e:
        log.warning(f'stats.update failed: {e}')


# ============ user mgmt ============

@owner_only
async def cmd_add(update, ctx):
    if not ctx.args:
        await reply(update).reply_text("用法：/add <名字>")
        return
    name = ctx.args[0]
    rc, out = run(['/opt/proxy-sub/gen.sh', 'add', name])
    if rc != 0:
        await reply(update).reply_text(f"❌ {out}")
        return
    await reply(update).reply_markdown(f"✅ 已添加 `{name}`\n\n订阅 URL：\n`{out}`")


@owner_only
async def cmd_revoke(update, ctx):
    if not ctx.args:
        await reply(update).reply_text("用法：/revoke <名字>")
        return
    name = ctx.args[0]
    rc, out = run(['/opt/proxy-sub/gen.sh', 'revoke', name])
    await reply(update).reply_text(out)


@owner_only
async def cmd_show_sub(update, ctx):
    if not ctx.args:
        await reply(update).reply_text("用法：/show_sub <名字>")
        return
    name = ctx.args[0]
    rc, out = run(['/opt/proxy-sub/gen.sh', 'show-sub', name])
    if rc == 0:
        await reply(update).reply_markdown(f"`{out}`")
    else:
        await reply(update).reply_text(out)


@owner_only
async def cmd_rotate(update, ctx):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ 确认全部轮换", callback_data="rotate_confirm"),
        InlineKeyboardButton("取消", callback_data="rotate_cancel"),
    ]])
    await reply(update).reply_text(
        "⚠️ 即将重新生成 Reality 密钥对、所有 short_id 和订阅 token。"
        "所有客户端需要重新订阅。确认继续？",
        reply_markup=kb
    )


async def on_rotate_callback(update, ctx):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    if q.data == 'rotate_cancel':
        await q.edit_message_text("已取消。")
        return
    rc, out = run(['/opt/proxy-sub/gen.sh', 'rotate'], timeout=60)
    await q.edit_message_text(f"已轮换。\n```\n{out}\n```", parse_mode='Markdown')


# ============ menu / GUI ============

MENU_LAYOUT = [
    [("📊 状态", "cb:status"), ("👤 在线", "cb:online"), ("📋 用户列表", "cb:list")],
    [("📈 今日流量", "cb:traffic_today"), ("📈 本周", "cb:traffic_week"), ("📈 本月", "cb:traffic_month")],
    [("🎯 IP纯净度", "cb:ip_check"), ("🛡️ Reality检测", "cb:sni_check"), ("🔐 证书到期", "cb:cert_expire")],
    [("🔄 切换SNI", "cb:switch_sni"), ("🔁 轮换密钥", "cb:rotate")],
    [("💾 磁盘", "cb:disk"), ("📝 日志", "cb:logs"), ("📦 备份", "cb:backup")],
    [("⏸️ 重载", "cb:reload"), ("🔃 重启", "cb:restart")],
    [("📖 帮助", "cb:help")],
]


def build_menu():
    rows = []
    for row in MENU_LAYOUT:
        rows.append([InlineKeyboardButton(label, callback_data=data) for label, data in row])
    return InlineKeyboardMarkup(rows)


@owner_only
async def cmd_menu(update, ctx):
    await reply(update).reply_text("🎛 *Proxy 控制面板*\n点击下方按钮执行操作：", reply_markup=build_menu(), parse_mode='Markdown')


# Map cb actions to (handler, args-injection)
CB_ROUTES = {
    'status':        ('status',        []),
    'online':        ('online',        []),
    'list':          ('list',          []),
    'traffic_today': ('traffic',       []),
    'traffic_week':  ('traffic',       ['week']),
    'traffic_month': ('traffic',       ['month']),
    'ip_check':      ('ip_check',      []),
    'sni_check':     ('sni_check',     []),
    'cert_expire':   ('cert_expire',   []),
    'switch_sni':    ('switch_sni',    []),
    'rotate':        ('rotate',        []),
    'disk':          ('disk',          []),
    'logs':          ('logs',          []),
    'backup':        ('backup',        []),
    'reload':        ('reload',        []),
    'restart':       ('restart',       []),
}


HELP_TEXT = (
    "📖 *命令说明*\n\n"
    "*带按钮的*：用 /menu 弹出面板，点按钮即可。\n\n"
    "*需要输入参数的命令（只能手动打）*：\n"
    "• `/add <名字>` — 新增用户，返回订阅 URL\n"
    "• `/revoke <名字>` — 吊销用户\n"
    "• `/show_sub <名字>` — 显示某用户的订阅 URL\n"
    "• `/traffic <名字>` — 单用户 7 天流量明细\n\n"
    "*所有命令也可手动输入：*\n"
    "/status /online /list /traffic /menu\n"
    "/ip_check /sni_check /cert_expire\n"
    "/switch_sni /rotate /reload /restart\n"
    "/disk /logs /backup\n"
)


async def cmd_help(update, ctx):
    await reply(update).reply_markdown(HELP_TEXT)


async def on_menu_callback(update, ctx):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    data = q.data
    # rotate has its own confirmation flow handled elsewhere
    if data in ('rotate_confirm', 'rotate_cancel'):
        return  # handled by on_rotate_callback
    if not data.startswith('cb:'):
        return
    action = data[3:]
    if action == 'help':
        await ctx.bot.send_message(OWNER, HELP_TEXT, parse_mode='Markdown')
        return
    route = CB_ROUTES.get(action)
    if not route:
        await ctx.bot.send_message(OWNER, f"Unknown action: {action}")
        return
    cmd_name, args = route
    # Lazy lookup of the cmd_X function in globals
    fn = globals().get(f'cmd_{cmd_name}')
    if not fn:
        await ctx.bot.send_message(OWNER, f"Handler cmd_{cmd_name} not found")
        return
    # Fake ctx.args for the duration of the call
    ctx.args = args
    await fn(update, ctx)


@owner_only
async def cmd_switch_sni(update, ctx):
    rc, out = run(['/opt/proxy-sub/gen.sh', 'switch-sni'])
    await reply(update).reply_text(out)


# ============ security checks ============

@owner_only
async def cmd_ip_check(update, ctx):
    await reply(update).reply_text("🔍 正在检测 IP 纯净度（约 30-60 秒）...")
    rc, out = run([CONFIG['ip_probe_script'], '-y', '-j', '-4'], timeout=300)
    if '{' not in out:
        await reply(update).reply_text(
            f'❌ 探测失败（脚本无输出）\nrc={rc}\n```\n{out[:800]}\n```',
            parse_mode='Markdown'
        )
        return
    # Strip leading non-JSON noise
    json_str = out[out.index('{'):]
    # Strip trailing non-JSON (find last balanced })
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        # Try truncating to last valid }
        depth = 0
        last_close = -1
        for i, ch in enumerate(json_str):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_close = i
        if last_close > 0:
            try:
                data = json.loads(json_str[:last_close+1])
            except json.JSONDecodeError as e:
                await reply(update).reply_text(f'解析 JSON 失败：{e}\n原始片段：\n```\n{json_str[:500]}\n```', parse_mode='Markdown')
                return
        else:
            await reply(update).reply_text(f'JSON 不完整\n```\n{json_str[:500]}\n```', parse_mode='Markdown')
            return

    head = data.get('Head', {})
    info = data.get('Info', {})
    score = data.get('Score', {})
    city = info.get('City', {}) if isinstance(info.get('City'), dict) else {}
    region = info.get('Region', {}) if isinstance(info.get('Region'), dict) else {}
    media = data.get('Media', {})
    mail = data.get('Mail', {})

    def m(*keys, default='N/A'):
        v = score
        for k in keys:
            if isinstance(v, dict):
                v = v.get(k, default)
            else:
                return default
        if v is None or v == '' or v == 'null':
            return 'N/A'
        return v

    def media_status(name):
        if not isinstance(media, dict):
            return 'N/A'
        m_obj = media.get(name, {}) or {}
        status = m_obj.get('Status', '?')
        reg = m_obj.get('Region', '')
        if '解锁' in str(status):
            return f'🟢 {reg or "解锁"}'
        if any(x in str(status) for x in ['屏蔽', '失败', '禁', '中国']):
            return f'🔴 {status}'
        return f'⚪ {status}'

    p25 = '✅ 畅通' if (isinstance(mail, dict) and mail.get('Port25') is True) else '❌ 封堵'

    msg = (
        f'🎯 *IP 纯净度报告*\n'
        f'IP：`{head.get("IP", "?")}`\n'
        f'位置：`{region.get("Name", "?")} - {city.get("Name", "?")}`\n'
        f'ASN：`AS{info.get("ASN","?")}` `{info.get("Organization","?")}`\n'
        f'类型：`{info.get("Type","?")}`\n\n'
        f'*风险评分（0 = 最佳）*\n'
        f'• Scamalytics：`{m("SCAMALYTICS")}/100`\n'
        f'• AbuseIPDB：`{m("AbuseIPDB")}/100`\n'
        f'• IPQS：`{m("IPQS")}/100`\n'
        f'• IP2Location：`{m("IP2LOCATION")}/100`\n\n'
        f'*流媒体解锁*\n'
        f'• YouTube：{media_status("Youtube")}\n'
        f'• Netflix：{media_status("Netflix")}\n'
        f'• Disney+：{media_status("DisneyPlus")}\n'
        f'• ChatGPT：{media_status("ChatGPT")}\n\n'
        f'*邮件出站*\n'
        f'• 25 端口：{p25}\n'
    )
    await reply(update).reply_markdown(msg)


@owner_only
async def cmd_sni_check(update, ctx):
    """非法 TLS 握手测试 — Reality 应当透明转发并返回 amazon 真证书"""
    try:
        ctx_ssl = ssl.create_default_context()
        ctx_ssl.check_hostname = False
        ctx_ssl.verify_mode = ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1', 443), timeout=5) as raw:
            with ctx_ssl.wrap_socket(raw, server_hostname='m.media-amazon.com') as s:
                cert = s.getpeercert(binary_form=True)
                proto = s.version()
                cert_size = len(cert) if cert else 0
        ok = cert_size > 1500
        emoji = "✅" if ok else "⚠️"
        status = "正常" if ok else "可疑"
        await reply(update).reply_text(
            f'{emoji} Reality 反向探测{status}\n'
            f'协议：{proto}\n证书大小：{cert_size} 字节'
        )
    except Exception as e:
        await reply(update).reply_text(f'❌ Reality 检测失败：{e}')


@owner_only
async def cmd_cert_expire(update, ctx):
    rc, out = run(['bash', '-c',
        'find /var/lib/caddy -name "*.crt" -print0 | xargs -0 -I{} sh -c "echo {}: ; openssl x509 -in {} -enddate -subject -noout"'])
    await reply(update).reply_text(out or '（未找到证书）')


# ============ system commands ============

@owner_only
async def cmd_disk(update, ctx):
    rc, df = run(['df', '-h', '/'])
    rc, mem = run(['free', '-h'])
    rc, up = run(['uptime', '-p'])
    await reply(update).reply_markdown(
        f'*磁盘*\n```\n{df}\n```\n*内存*\n```\n{mem}\n```\n开机时长：`{up}`'
    )


@owner_only
async def cmd_logs(update, ctx):
    rc, out = run(['journalctl', '-u', 'sing-box', '-n', '20', '--no-pager', '-p', 'warning'])
    text = out[-3500:] if out else '（近期无 warn/error 日志）'
    await reply(update).reply_text(text)


@owner_only
async def cmd_reload(update, ctx):
    rc, out = run(['systemctl', 'reload', 'sing-box'])
    await reply(update).reply_text('✅ 已重新加载' if rc == 0 else f'❌ {out}')


@owner_only
async def cmd_restart(update, ctx):
    await reply(update).reply_text('⏳ 重启中（所有连接将瞬断重连）...')
    rc, out = run(['systemctl', 'restart', 'sing-box'])
    await reply(update).reply_text('✅ 已重启' if rc == 0 else f'❌ {out}')


@owner_only
async def cmd_backup(update, ctx):
    stamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    out = f'/opt/proxy-sub/backups/proxy-backup-{stamp}.tar.gz'
    files_to_backup = [
        '/etc/sing-box/users.txt',
        '/etc/sing-box/reality.key',
        '/etc/sing-box/reality.pub',
        '/etc/sing-box/config.json',
        '/etc/sing-box/clash_secret',
        '/etc/caddy/Caddyfile',
        '/etc/duckdns/domain',
        '/etc/duckdns/token',
        '/etc/nftables.conf',
        '/opt/proxy-bot/config.json',
        '/opt/proxy-bot/stats.db',
        '/opt/proxy-sub/gen.sh',
    ]
    os.makedirs('/opt/proxy-sub/backups', exist_ok=True)
    with tarfile.open(out, 'w:gz') as tar:
        for f in files_to_backup:
            if os.path.exists(f):
                tar.add(f)
    os.chmod(out, 0o600)
    size = os.path.getsize(out)
    md5 = hashlib.md5(open(out, 'rb').read()).hexdigest()
    msg = (
        f'📦 *备份完成*\n'
        f'```\n'
        f'路径：{out}\n'
        f'大小：{size} 字节\n'
        f'md5： {md5}\n\n'
        f'从 Mac 拉取：\n'
        f'  scp proxy-vps:{out} ~/Documents/proxy-backups/\n'
        f'```'
    )
    await reply(update).reply_markdown(msg)


# ============ auto-alerts ============

_alert_state = {'sing-box_alive': True, 'sni_ok': True, 'cert_days_warn_sent': False}
_known_ssh_cursors: set = set()


async def alert_service_health(ctx):
    rc, st = run(['systemctl', 'is-active', 'sing-box'])
    alive = (st == 'active')
    if _alert_state['sing-box_alive'] and not alive:
        await ctx.bot.send_message(OWNER, '🚨 *sing-box 已停止运行*', parse_mode='Markdown')
    elif not _alert_state['sing-box_alive'] and alive:
        await ctx.bot.send_message(OWNER, '✅ sing-box 已恢复')
    _alert_state['sing-box_alive'] = alive


async def alert_ssh_login(ctx):
    rc, out = run(['journalctl', '_COMM=sshd', '--since', '90 sec ago',
                   '--no-pager', '-q', '-o', 'short-iso'])
    for line in out.splitlines():
        if 'Accepted publickey' not in line:
            continue
        if line in _known_ssh_cursors:
            continue
        _known_ssh_cursors.add(line)
        m = re.search(r'from ([\d\.a-fA-F:]+)', line)
        ip = m.group(1) if m else '?'
        ts = line[:19]
        await ctx.bot.send_message(
            OWNER,
            f'🔐 *SSH 登录*\n来源：`{ip}`\n时间：`{ts}`',
            parse_mode='Markdown',
        )
    if len(_known_ssh_cursors) > 500:
        _known_ssh_cursors.clear()


async def alert_sni(ctx):
    try:
        ctx_ssl = ssl.create_default_context()
        ctx_ssl.check_hostname = False
        ctx_ssl.verify_mode = ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1', 443), timeout=5) as raw:
            with ctx_ssl.wrap_socket(raw, server_hostname='m.media-amazon.com') as s:
                cert = s.getpeercert(binary_form=True)
                ok = bool(cert and len(cert) > 1500)
    except Exception:
        ok = False
    if _alert_state['sni_ok'] and not ok:
        await ctx.bot.send_message(OWNER, '🚨 Reality 反向探测保护已失效，请检查 sing-box')
    elif not _alert_state['sni_ok'] and ok:
        await ctx.bot.send_message(OWNER, '✅ Reality 反向探测保护已恢复')
    _alert_state['sni_ok'] = ok


async def alert_cert(ctx):
    rc, out = run(['bash', '-c',
        'find /var/lib/caddy -name "*.crt" | head -1 | xargs -r openssl x509 -enddate -noout 2>/dev/null'])
    m = re.search(r'notAfter=(.+)', out)
    if not m:
        return
    try:
        exp = datetime.strptime(m.group(1).strip(), '%b %d %H:%M:%S %Y %Z')
        days = (exp - datetime.utcnow()).days
        if days <= 14 and not _alert_state['cert_days_warn_sent']:
            await ctx.bot.send_message(OWNER, f'⚠️ Caddy 证书将在 {days} 天后到期')
            _alert_state['cert_days_warn_sent'] = True
        elif days > 14:
            _alert_state['cert_days_warn_sent'] = False
    except Exception:
        pass


async def alert_user_traffic(ctx):
    b_in, b_out = stats.today_total()
    total = b_in + b_out
    threshold = CONFIG['alert_thresholds'].get('daily_total_gb', 200) * 1024 ** 3
    if total > threshold:
        await ctx.bot.send_message(
            OWNER,
            f'⚠️ 今日总流量已达 {stats.human(total)}（阈值 {CONFIG["alert_thresholds"].get("daily_total_gb", 200)} GB）',
            parse_mode='Markdown',
        )


async def alert_daily_summary(ctx):
    await ctx.bot.send_message(OWNER, stats.today_summary(), parse_mode='Markdown')


# ============ main ============

def main():
    app = ApplicationBuilder().token(CONFIG['bot_token']).build()
    h = CommandHandler
    app.add_handler(h('start',       cmd_start))
    app.add_handler(h('menu',        cmd_menu))
    app.add_handler(h('help',        cmd_help))
    app.add_handler(h('status',      cmd_status))
    app.add_handler(h('list',        cmd_list))
    app.add_handler(h('traffic',     cmd_traffic))
    app.add_handler(h('online',      cmd_online))
    app.add_handler(h('add',         cmd_add))
    app.add_handler(h('revoke',      cmd_revoke))
    app.add_handler(h('show_sub',    cmd_show_sub))
    app.add_handler(h('rotate',      cmd_rotate))
    app.add_handler(h('switch_sni',  cmd_switch_sni))
    app.add_handler(h('ip_check',    cmd_ip_check))
    app.add_handler(h('sni_check',   cmd_sni_check))
    app.add_handler(h('cert_expire', cmd_cert_expire))
    app.add_handler(h('disk',        cmd_disk))
    app.add_handler(h('logs',        cmd_logs))
    app.add_handler(h('reload',      cmd_reload))
    app.add_handler(h('restart',     cmd_restart))
    app.add_handler(h('backup',      cmd_backup))
    # Route callback queries: rotate_* go to on_rotate_callback, cb:* go to on_menu_callback
    from telegram.ext import CallbackQueryHandler as CBQ
    app.add_handler(CBQ(on_rotate_callback, pattern=r'^rotate_'))
    app.add_handler(CBQ(on_menu_callback,   pattern=r'^cb:'))

    jq = app.job_queue
    jq.run_repeating(periodic_stats,       interval=60,      first=15)
    jq.run_repeating(alert_service_health, interval=60,      first=30)
    jq.run_repeating(alert_ssh_login,      interval=60,      first=45)
    jq.run_repeating(alert_sni,            interval=6*3600,  first=120)
    jq.run_repeating(alert_cert,           interval=12*3600, first=180)
    # 23:00 Beijing = 15:00 UTC
    jq.run_daily(alert_user_traffic,  dtime(hour=15, minute=0,  tzinfo=timezone.utc))
    # 00:30 Beijing next day = 16:30 UTC
    jq.run_daily(alert_daily_summary, dtime(hour=16, minute=30, tzinfo=timezone.utc))

    log.warning('Bot starting...')
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
