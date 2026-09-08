"""模式 A 单次管理：默认只读建议；--live 才允许按规则平仓。无自动开仓。"""
import argparse
import fcntl
import json

import trade_a
from check_a import check


def run(execute=False, account=False):
    if execute and not account:
        raise ValueError('执行必须使用真实账户状态')
    trade_a.LIVE = account
    results = {}
    with trade_a.state_path().with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        d = trade_a.load()
        if not d:
            return {'status': 'NO_LOCAL_POSITIONS'}
        ex = trade_a.exchange()
        for coin, p in d.items():
            r = check(ex, p, private=account)
            r['executed'] = False
            if execute and r['action'] == 'EXIT':
                try:
                    # close 内部重新对账，每笔 IOC 重新查盘口；失败保留余量。
                    trade_a.do_close(coin)
                    r['executed'] = True
                except Exception as exc:
                    r['action'] = 'NEEDS_ATTENTION'
                    r['gaps'].append(f'{type(exc).__name__}:{exc}')
                r['position_after'] = trade_a.load()[coin]['phase']
            results[coin] = r
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', action='store_true', help='只读真实账户，输出管理建议')
    parser.add_argument('--live', action='store_true', help='显式授权本次规则触发的平仓')
    args = parser.parse_args()
    print(json.dumps(run(args.live, args.account or args.live), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
