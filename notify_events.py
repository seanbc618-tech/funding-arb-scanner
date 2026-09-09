"""F03：独立读取只读监控快照并通知；不访问账户、不调用交易入口。"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from notify_run import notifier_command, send_message
from check_a import MIN_MARGIN_RATIO, MIN_LIQ_DISTANCE

ROOT = Path(__file__).resolve().parent
STATUSES = {'OK', 'NO_POSITIONS', 'NO_LOCAL_POSITIONS', 'CONFIG_MISSING', 'DATA_GAP', 'ERROR'}
PHASES = {'opening', 'open', 'closing', 'closed', 'needs_close'}
# 精确枚举来源代码；冒号后的任意内容、原始异常和账户标识永不进入消息。
CODES = set('''MISSING_CREDENTIALS LOCAL_STATE_ERROR API_ERROR POSITION_ROW_INVALID
POSITION_CONTRACTS_UNKNOWN POSITION_CONTRACTS_INVALID POSITION_SYMBOL_UNKNOWN
LIQUIDATION_DATA_UNKNOWN USDT_FREE_UNKNOWN USDT_USED_UNKNOWN USDT_TOTAL_UNKNOWN
BALANCE_INVALID BALANCE_TOTAL_UNKNOWN ACCOUNT_INFO_UNKNOWN MARGIN_RATIO_UNKNOWN
LOCAL_PENDING_UNKNOWN LOCAL_QUANTITY_UNKNOWN LOCAL_CLOSED_WITH_REMAINDER
LOCAL_SYMBOL_UNKNOWN LOCAL_POSITION_AMBIGUOUS ACCOUNT_POSITION_AMBIGUOUS
POSITION_SIDE_MISMATCH POSITION_COMPARE_INVALID LOCAL_POSITION_NOT_FOUND
POSITION_CONTRACTS_MISMATCH ACCOUNT_POSITION_UNOWNED SPOT_SYMBOL_UNKNOWN
SPOT_OWNERSHIP_AMBIGUOUS SPOT_OWNERSHIP_UNKNOWN SPOT_BALANCE_UNKNOWN
SPOT_BALANCE_MISMATCH ACCOUNT_BALANCE_UNOWNED'''.split())


def finite(value):
    if isinstance(value, bool):
        raise ValueError('INVALID_NUMBER')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('INVALID_NUMBER')
    return value


def event_view(report):
    if report.get('mode') != 'account_read_only' or report.get('status') not in STATUSES:
        raise ValueError('INVALID_SNAPSHOT')
    gaps = report.get('gaps')
    rows = report.get('local_positions')
    if not isinstance(gaps, list) or not isinstance(rows, list):
        raise ValueError('INVALID_SNAPSHOT')
    codes = sorted({g.split(':', 1)[0] if isinstance(g, str) and g.split(':', 1)[0] in CODES
                    else 'OTHER_DATA_GAP' for g in gaps})
    positions = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('INVALID_LOCAL_ROW')
        coin = row.get('coin')
        # 资产代码仅保留有限大写字母数字，拒绝路径、用户名和地址。
        label = coin if isinstance(coin, str) and re.fullmatch(r'[A-Z][A-Z0-9]{0,11}', coin) else 'UNKNOWN'
        phase = row.get('phase') if row.get('phase') in PHASES else 'unknown'
        quantities = []
        for key in ('base', 'contracts'):
            try:
                value = finite(row.get(key))
                quantities.append(value if value >= 0 else None)
            except (TypeError, ValueError):
                quantities.append(None)
        positions.append([label, phase, *quantities, bool(row.get('pending'))])
    positions.sort(key=lambda p: json.dumps(p))
    account_positions = [] if 'account_positions' in report else None
    for row in report.get('account_positions', []):
        if not isinstance(row, dict):
            raise ValueError('INVALID_ACCOUNT_ROW')
        symbol = row.get('symbol')
        label = symbol if isinstance(symbol, str) and re.fullmatch(r'[A-Z0-9/:-]{1,48}', symbol) else 'UNKNOWN'
        side = row.get('side') if row.get('side') in ('long', 'short') else 'unknown'
        try:
            qty = finite(row.get('contracts'))
            if qty < 0: qty = None
        except (TypeError, ValueError):
            qty = None
        account_positions.append([label, side, qty])
    if account_positions is not None:
        account_positions.sort(key=lambda p: json.dumps(p))
    risk_codes = set()
    balance = report.get('balance') or {}
    if isinstance(balance, dict) and 'margin_ratio' in balance:
        try:
            if finite(balance['margin_ratio']) <= MIN_MARGIN_RATIO:
                risk_codes.add('MARGIN_RATIO_LOW')
        except (TypeError, ValueError):
            risk_codes.add('MARGIN_RATIO_UNKNOWN')
    for pos in report.get('account_positions', []):
        try:
            mark, liq = finite(pos.get('markPrice')), finite(pos.get('liquidationPrice'))
            side = pos.get('side')
            if mark <= 0 or liq <= 0 or side not in ('short', 'long'):
                raise ValueError('UNKNOWN')
            distance = (liq - mark) / mark if side == 'short' else (mark - liq) / mark
            if distance <= MIN_LIQ_DISTANCE:
                risk_codes.add('LIQUIDATION_DISTANCE_LOW')
        except (AttributeError, TypeError, ValueError):
            risk_codes.add('LIQUIDATION_DATA_UNKNOWN')
    return {'status': report['status'], 'gap_codes': codes, 'risk_codes': sorted(risk_codes), 'positions': positions, 'account_positions': account_positions}


def message(view, checked_at):
    stamp = datetime.fromtimestamp(checked_at, timezone.utc).isoformat(timespec='seconds')
    lines = ['【资金费率套利｜监控事件】', f'快照时间：{stamp}', f'状态：{view["status"]}',
             '异常类别：' + ('、'.join(view['gap_codes']) or '未报告异常'),
             '风险类别：' + ('、'.join(view['risk_codes']) or '快照未显示越线'),
             f'本地记录：{len(view["positions"])} 条']
    for coin, phase, base, contracts, pending in view['positions'][:4]:
        lines.append(f'{coin}：{phase}；现货余量 {base}；永续张数 {contracts}；未确认订单 {pending}')
    actual = view.get('account_positions')
    lines.append('账户实际仓位：' + (str(len(actual)) + ' 条（不自动归属策略）' if actual is not None else '未知'))
    for symbol, side, qty in (actual or [])[:4]:
        lines.append(f'{symbol}：{side}；张数 {qty}')
    lines.append('仅报告监控观察；本地阶段不等于成交确认，配置缺失不代表无仓，未执行交易。')
    return '\n'.join(lines)


def save(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(data, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def tick(snapshot, state_path, cooldown=300, now=None, sender=send_message):
    now = time.time() if now is None else now
    with state_path.with_suffix('.lock').open('a') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        if state is not None:
            if (not isinstance(state, dict) or state.get('version') != 1
                    or not isinstance(state.get('signature'), str)
                    or state.get('status') not in ('ATTEMPTING', 'CONFIRMED', 'DELIVERY_UNKNOWN')):
                raise ValueError('INVALID_NOTIFICATION_STATE')
            finite(state['attempted_at'])
        report = json.loads(snapshot.read_text())
        checked = finite(report['checked_at'])
        if not 0 <= now - checked <= 180:
            return {'status': 'SNAPSHOT_STALE'}  # 失联告警属于 F06，不发送旧事实。
        view = event_view(report)
        signature = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()
        if state and state['signature'] == signature:
            return {'status': 'UNCHANGED', 'delivery': state['status']}
        if state and now - state['attempted_at'] < cooldown:
            return {'status': 'THROTTLED'}
        command = notifier_command()  # 配置错误发生在发送意图落盘之前。
        # 先落盘再发送；进程中断留下 ATTEMPTING，重启不重复同一事件。
        state = {'version': 1, 'signature': signature, 'attempted_at': now, 'status': 'ATTEMPTING'}
        save(state_path, state)
        try:
            receipt = sender(message(view, checked), command)
            if (receipt.get('status') == 'CONFIRMED' and type(receipt.get('message_id')) is int
                    and receipt['message_id'] > 0):
                state.update(status='CONFIRMED', message_id=receipt['message_id'])
            else:
                state['status'] = 'DELIVERY_UNKNOWN'
        except Exception:
            state['status'] = 'DELIVERY_UNKNOWN'
        save(state_path, state)
        return {'status': state['status'], **({'message_id': state['message_id']} if 'message_id' in state else {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, default=ROOT / 'monitor_a_status.json')
    parser.add_argument('--state', type=Path, default=ROOT / 'notify_events_state.json')
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--cooldown', type=float, default=300)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 10 or not math.isfinite(args.cooldown) or args.cooldown < 300:
        parser.error('interval 至少 10 秒，cooldown 至少 300 秒，均须为有限数')
    while True:
        try:
            result = tick(args.snapshot, args.state, args.cooldown)
        except Exception as exc:
            # 不记录异常原文、配置路径、Token 或子进程日志。
            result = {'status': 'NOTIFICATION_CHECK_FAILED', 'error_type': type(exc).__name__}
        # 独立 watchdog 读取心跳；不含消息正文、凭据或原始异常。
        save(ROOT / 'notify_events_health.json', {'checked_at': time.time(), 'result': result})
        print(json.dumps(result), flush=True)
        if args.once:
            return 0 if result['status'] in ('CONFIRMED', 'UNCHANGED', 'THROTTLED') else 2
        time.sleep(args.interval)


if __name__ == '__main__':
    raise SystemExit(main())
