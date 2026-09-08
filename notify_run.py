"""通过 Telegram Notifier 插件运行并汇报一次项目命令；不重试发送。"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
SCRIPTS = ('scan.py', 'pool.py', 'verify.py', 'trade_a.py', 'check_a.py', 'accounting.py', 'manage_a.py')


def notifier_command():
    script = os.environ.get('TELEGRAM_NOTIFY_SCRIPT')
    if not script:
        candidates = list((Path.home() / '.codex/plugins/cache/personal/telegram-notifier').glob('*/scripts/notify.py'))
        if len(candidates) != 1:
            raise RuntimeError('设置 TELEGRAM_NOTIFY_SCRIPT 为插件 notify.py 的绝对路径')
        script = str(candidates[0])
    if not Path(script).is_file():
        raise RuntimeError('Telegram Notifier 脚本不存在')
    cmd = [os.environ.get('TELEGRAM_NOTIFY_PYTHON', '/usr/bin/python3'), script]
    config = os.environ.get('TELEGRAM_NOTIFY_CONFIG')
    if not config and not os.environ.get('TELEGRAM_BOT_TOKEN'):
        config = str(Path.home() / '.agents/telegram-notifier/config.json')
    if config:
        if not Path(config).is_file():
            raise RuntimeError('通知器私有配置不存在')
        cmd += ['--config', config]
    return cmd + ['send']


def summary(script, args, code, elapsed, output):
    mode = '显式实盘参数' if '--live' in args else (
        '真实账户只读' if '--account' in args or script == 'accounting.py' else '公共行情/空跑')
    lines = ['【资金费率套利｜运行报告】', datetime.now().astimezone().isoformat(timespec='seconds'),
             f'入口：{script}；模式：{mode}', f'进程退出码：{code}；耗时：{elapsed:.1f}秒']
    # 只发送白名单字段，不发送原始日志、堆栈、参数、地址或账户 ID。
    try:
        report = json.loads(output)
    except (ValueError, TypeError):
        report = None
    allowed = {'HOLD', 'EXIT', 'BLOCK', 'CLOSED', 'PASS', 'NEEDS_ATTENTION',
               'NO_LOCAL_POSITIONS', 'INVALID_DECLARED_GAP', 'RECONCILIATION_REQUIRED',
               'RECONCILED_AS_OF_QUERY'}
    if isinstance(report, dict):
        entries = report.items() if script == 'manage_a.py' and 'status' not in report else [('结果', report)]
        for name, item in list(entries)[:12]:
            if not isinstance(item, dict):
                continue
            state = item.get('action', item.get('status'))
            if state not in allowed:
                continue
            label = name if name == '结果' or (name.isalnum() and len(name) <= 15) else '标的'
            lines.append(f'{label}：{state}；待核对项 {len(item.get("gaps", item.get("reasons", [])))}')
    if code:
        lines.append('运行失败，请在本机查看日志；不会自动重试命令或消息。')
    lines.append('进程成功不代表两腿成交或盈利；账户和收益以对账结果为准。')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('script', choices=SCRIPTS)
    parser.add_argument('args', nargs=argparse.REMAINDER)
    opts = parser.parse_args()
    command = notifier_command()  # 本地配置无效则在业务启动前停止
    start = time.monotonic()
    result = subprocess.run([sys.executable, '-B', str(ROOT / opts.script), *opts.args],
                            cwd=ROOT, capture_output=True, text=True)
    print(result.stdout, end='')
    print(result.stderr, end='', file=sys.stderr)
    message = summary(opts.script, opts.args, result.returncode, time.monotonic()-start, result.stdout)
    try:
        sent = subprocess.run(command, input=message, text=True, capture_output=True, timeout=35)
        receipt = json.loads(sent.stdout) if sent.returncode == 0 else {}
        if receipt.get('ok') is not True or not receipt.get('message_id'):
            raise RuntimeError('unconfirmed')
        print(f'Telegram 已发送：message_id={receipt["message_id"]}')
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        print('Telegram 投递未确认；先查看群消息，不自动重试。业务状态保持原样。', file=sys.stderr)
        return result.returncode or 2
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
