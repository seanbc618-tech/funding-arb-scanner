"""F06：独立定时报告与新鲜度 watchdog；只读账户与账库。"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from accounting import covered, dec
from notify_events import event_view, finite, save
from notify_run import notifier_command, send_message

TZ = ZoneInfo('Asia/Shanghai')


def health(root, now, services):
    issues = []
    for name, filename in [('MONITOR', 'monitor_a_status.json'), ('NOTIFIER', 'notify_events_health.json')]:
        if services.get(name) != 'active':
            issues.append(name + '_SERVICE_INACTIVE')
        try:
            data = json.loads((root / filename).read_text())
            if not 0 <= now - finite(data['checked_at']) <= 180:
                issues.append(name + '_STALE')
            if name == 'MONITOR':
                view = event_view(data)
                if view['status'] in ('ERROR', 'CONFIG_MISSING'):
                    issues.append('MONITOR_DATA_UNAVAILABLE')
            else:
                result = data['result']
                if result['status'] not in ('CONFIRMED', 'UNCHANGED', 'THROTTLED', 'SNAPSHOT_STALE'):
                    issues.append('NOTIFIER_CHECK_FAILED')
                if result.get('delivery') in ('ATTEMPTING', 'DELIVERY_UNKNOWN'):
                    issues.append('NOTIFIER_DELIVERY_UNKNOWN')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            issues.append(name + '_HEARTBEAT_INVALID')
    return sorted(set(issues))


def daily_ledger(path, account, day):
    """北京时间自然日，左闭右开；账户入账不等于策略收益。"""
    result = {'funding_usdt': None, 'fees_by_currency': None,
              'strategy_net_pnl_usdt': None, 'gap': 'DAILY_COVERAGE_MISSING'}
    if not path.is_file():
        return result
    start = int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp() * 1000)
    end = start + 86400000
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if not all(covered(db, account, kind, scope, start, end) for kind, scope in
                   [('fills', 'ALL:SPOT'), ('fills', 'ALL:SWAP'), ('bills', 'ACCOUNT')]):
            return result
        if db.execute('SELECT 1 FROM evidence_conflicts WHERE account=? LIMIT 1', (account,)).fetchone():
            result['gap'] = 'EVIDENCE_CONFLICT'; return result
        funding, fees = dec(0), {}
        for kind, raw in db.execute('SELECT kind,raw FROM evidence WHERE account=?', (account,)):
            row = json.loads(raw)
            if not start <= int(row['ts']) < end:
                continue
            if kind == 'bills' and str(row.get('type')) == '8':
                if row.get('ccy') != 'USDT':
                    result['gap'] = 'FUNDING_CURRENCY_UNSUPPORTED'; return result
                funding += dec(row['balChg'])
            if kind == 'fills':
                ccy = row['feeCcy']
                if not isinstance(ccy, str) or not re.fullmatch('[A-Z0-9]{1,12}', ccy):
                    raise ValueError('INVALID_CURRENCY')
                fees[ccy] = fees.get(ccy, dec(0)) - dec(row['fee'])
        result.update(funding_usdt=str(funding), fees_by_currency={k: str(v) for k, v in fees.items()},
                      gap='DAILY_STRATEGY_ATTRIBUTION_UNKNOWN')
    return result


def worker(root, db, day):
    import trade_a
    from portfolio_a import report
    trade_a.ROOT = root
    trade_a.LIVE = True
    ex = trade_a.exchange()
    data = {'portfolio': report(ex, trade_a.load(), db)}
    if day:
        try:
            uid = ex.private_get_account_config()['data'][0]['uid']
            data['daily'] = daily_ledger(db, hashlib.sha256(str(uid).encode()).hexdigest(), day)
        except Exception:
            data['daily'] = {'gap': 'DAILY_EVIDENCE_UNAVAILABLE'}
    return data


def query(root, db, day):
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', '--root', str(root), '--db', str(db)]
    if day:
        command += ['--day', day]
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=55)
        if p.returncode:
            raise ValueError('REPORT_FAILED')
        return json.loads(p.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'error': 'REPORT_UNAVAILABLE'}


def number(value):
    try:
        n = dec(value)
        return format(n, '.8f') if abs(n) < dec('1e20') else 'null'
    except (ValueError, ArithmeticError):
        return 'null'


def report_message(data, now, day=None):
    lines = ['【资金费率套利｜' + ('日报' if day else '持仓摘要') + '】',
             '生成时间：' + datetime.fromtimestamp(now, TZ).isoformat(timespec='seconds')]
    if day:
        d = data.get('daily', {})
        lines += [f'账目日期：{day}（北京时间自然日）',
                  '账户已入账资金费 USDT：' + number(d.get('funding_usdt')),
                  '账户手续费（正数支出、负数返佣）：' + (', '.join(
                      k + '=' + number(v) for k, v in d.get('fees_by_currency', {}).items()
                      if re.fullmatch('[A-Z0-9]{1,12}', k)) if isinstance(d.get('fees_by_currency'), dict) else 'null'),
                  '昨日策略净收益：null；尚无完整日级策略归属与估值证据。']
        if d.get('funding_usdt') is None:
            lines.append('账目缺口：未取得该日完整且无冲突的账户成交/账单覆盖；不按零处理。')
    p = data.get('portfolio', {})
    try:
        fresh = 0 <= now * 1000 - finite(p['checked_at_ms']) <= 90_000
    except (KeyError, TypeError, ValueError):
        fresh = False
    if not fresh:
        lines.append('当前持仓/收益：null（REPORT_UNAVAILABLE_OR_STALE）')
    else:
        status = p.get('status')
        lines.append('当前查询：' + (status if status in ('DATA_GAP', 'NO_STRATEGY_POSITIONS', 'AS_OF_QUERY') else 'UNKNOWN'))
        account = p.get('account', {})
        lines += ['账户净资产 USD：' + number(account.get('equity_usd')),
                  '账户仓位数：' + (str(len(account['positions'])) if isinstance(account.get('positions'), list) else 'null'),
                  '非策略归属仓位数：' + number(account.get('unowned_position_count'))]
        positions = p.get('positions', {})
        lines.append(f'本地策略记录：{len(positions)} 条（下列为持仓累计/当前值，不是昨日收益）')
        for coin, item in list(positions.items())[:6]:
            label = coin if re.fullmatch('[A-Z][A-Z0-9]{0,11}', coin) else 'UNKNOWN'
            lines.append(label + '：入账资金费=' + number(item.get('booked_funding_usdt'))
                         + '；已实现=' + number(item.get('realized_trade_pnl_usdt'))
                         + '；未实现估值=' + number(item.get('unrealized_pnl_usdt'))
                         + '；退出成本估计=' + number(item.get('exit_total_cost_usdt'))
                         + '；退出后净损益估计=' + number(item.get('estimated_net_after_exit_usdt'))
                         + '；已平仓净损益=' + number(item.get('settled_net_pnl_usdt'))
                         + '；缺口数=' + str(len(item.get('gaps', []))))
        if len(positions) > 6:
            lines.append('更多持仓请查看完整只读报告。')
    lines.append('账户资产不等于策略净值；缺数据为 null。只读报告，未执行交易。')
    return '\n'.join(lines)


def tick(root, state_path, db, services, hours=1, now=None, sender=send_message, provider=query):
    live_clock = now is None
    now = time.time() if live_clock else finite(now)
    with state_path.with_suffix('.lock').open('a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text()) if state_path.exists() else {'version': 1, 'slots': {}}
        if not isinstance(state, dict) or state.get('version') != 1 or not isinstance(state.get('slots'), dict):
            raise ValueError('INVALID_REPORT_STATE')
        for slot in state['slots'].values():
            if (not isinstance(slot, dict) or not isinstance(slot.get('key'), str)
                    or slot.get('status') not in ('BASELINE', 'ATTEMPTING', 'CONFIRMED', 'DELIVERY_UNKNOWN')):
                raise ValueError('INVALID_REPORT_STATE')
            finite(slot['attempted_at'])
        results = []

        def send(slot, key, text):
            command = notifier_command()
            entry = {'key': key, 'attempted_at': now, 'status': 'ATTEMPTING'}
            state['slots'][slot] = entry
            save(state_path, state)
            try:
                receipt = sender(text, command)
                if receipt.get('status') == 'CONFIRMED' and type(receipt.get('message_id')) is int and receipt['message_id'] > 0:
                    entry.update(status='CONFIRMED', message_id=receipt['message_id'])
                else:
                    entry['status'] = 'DELIVERY_UNKNOWN'
            except Exception:
                entry['status'] = 'DELIVERY_UNKNOWN'
            save(state_path, state)
            results.append({'slot': slot, **entry})

        issues = health(root, now, services)
        key = ','.join(issues) or 'HEALTHY'
        old = state['slots'].get('health')
        if old is None and not issues:
            state['slots']['health'] = {'key': key, 'attempted_at': now - 300, 'status': 'BASELINE'}
            save(state_path, state)
        elif old is None or (old['key'] != key and now - old['attempted_at'] >= 300):
            send('health', key, '【资金费率套利｜' + ('进程/数据异常' if issues else '进程/数据恢复')
                 + '】\n' + ('、'.join(issues) if issues else '监控快照与通知心跳均已恢复更新，服务运行中。')
                 + '\n独立定时检查；健康恢复不代表策略收益或风险缺口已解决。')

        dt = datetime.fromtimestamp(now, TZ)
        daily_key = (dt.date() - timedelta(days=1)).isoformat()
        hour_key = dt.strftime('%Y-%m-%d') + '/' + f'{dt.hour // hours:02d}' if hours else None
        daily_due = dt.hour >= 8 and state['slots'].get('daily', {}).get('key', '') < daily_key
        hour_due = hours and state['slots'].get('hourly', {}).get('key', '') < hour_key
        if daily_due or hour_due:
            data = provider(root, db, daily_key if daily_due else None)
            if daily_due:
                # 合并本小时摘要；先持久化抑制标记，重启不再发同小时摘要。
                if hours:
                    state['slots']['hourly'] = {'key': hour_key, 'attempted_at': now, 'status': 'BASELINE'}
                    save(state_path, state)
                send('daily', daily_key, report_message(data, time.time() if live_clock else now, daily_key))
            else:
                send('hourly', hour_key, report_message(data, time.time() if live_clock else now))
        return {'checked_at': now, 'health': key, 'deliveries': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--state', type=Path)
    parser.add_argument('--summary-hours', type=int, choices=(0, 1, 4), default=1)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--day')
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.root, args.db, args.day), ensure_ascii=False, allow_nan=False))
        return
    services = {}
    for name, unit in [('MONITOR', 'funding-arb-monitor.service'), ('NOTIFIER', 'funding-arb-notify.service')]:
        try:
            p = subprocess.run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True, timeout=5)
            services[name] = p.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            services[name] = 'unknown'
    result = tick(args.root, args.state or args.root / 'report_watch_state.json', args.db, services, args.summary_hours)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
