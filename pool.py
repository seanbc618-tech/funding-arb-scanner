"""费率套利池子管理：扫全市场 -> 排出候选配对 -> 对比当前持仓 -> 决定开/持/平。
只打印决策，不下单。复用 scan.py 的抓取和过滤。
用法：python3 pool.py
"""
import collections, csv, json, os, time
from concurrent.futures import ThreadPoolExecutor
from scan import EXCHANGES, HOLD_DAYS, MAJORS, MIN_SAME_SIGN, fetch, usable

STATE = 'positions.json'
LOG = 'pool_log.csv'         # 每次跑的决策都追加进来，用来回看换仓频率和净值
MAJORS_ONLY = True           # 先只做主流币；meme 池等这边跑顺了再设 False
MAX_POSITIONS = 5           # 同时最多持几个配对，资金分几份
ENTRY_NET_APR = 0.08        # 扣费名义年化估算低于此不开新仓
EXIT_NET_APR = 0.03         # 跌破此才平——和开仓阈值之间留滞后带，价差小幅波动不会来回换仓
MIN_HOLD_DAYS = 3           # 开仓后最少持有天数，防噪音触发换仓（换一次要付四笔 taker）


def candidates(rows):
    """同一个币在两个所都够量、方向都够稳 -> 一个候选配对"""
    by = collections.defaultdict(list)
    for r in rows:
        if MAJORS_ONLY and r['coin'] not in MAJORS:
            continue
        if usable(r) and (r['same_sign'] or 0) >= MIN_SAME_SIGN:
            by[r['coin']].append(r)
    out = {}
    for coin, rs in by.items():
        if len(rs) < 2:
            continue
        # 旧状态未保存 symbol，本所同币多合约不能任选其一，直接跳过歧义单元。
        counts = collections.Counter(r['exchange'] for r in rs)
        rs = [r for r in rs if counts[r['exchange']] == 1]
        for hi in rs:
            for lo in rs:
                if hi['exchange'] == lo['exchange']:
                    continue
                cost = 2 * (hi['taker'] + lo['taker'])
                out[(coin, hi['exchange'], lo['exchange'])] = {
                    'net': hi['apr_7d'] - lo['apr_7d'] - cost * (365 / HOLD_DAYS),
                    'gross': hi['apr_7d'] - lo['apr_7d'],
                    'vol': min(hi['vol_usd'], lo['vol_usd']),
                    'settles': f"{hi['settle']}/{lo['settle']}",
                }
    return out


def main():
    print('模式 C 跨所空跑研究；未接入模式 A 执行器。APR 仅为扣手续费后的名义估算。')
    with ThreadPoolExecutor(len(EXCHANGES)) as pool:
        rows = [r for rs in pool.map(fetch, EXCHANGES) for r in rs]
    cand = candidates(rows)
    held = []
    if os.path.exists(STATE):
        with open(STATE) as f:
            held = json.load(f)['positions']
    today = time.strftime('%Y-%m-%d')
    now = time.time()

    print(f'\n扫到 {len(rows)} 个永续，{len(cand)} 个候选配对，当前持仓 {len(held)} 个\n')

    keep, actions, logged = [], [], []
    for p in held:
        key = (p['coin'], p['short_on'], p['long_on'])
        c = cand.get(key)
        age = (now - p['opened_ts']) / 86400
        if c is None:
            keep.append(p)
            actions.append(f"待核对 {p['coin']}：原配对无有效估值，保留空跑持仓")
            logged.append(('待核对', p['coin'], p['short_on'], p['long_on'], None))
        elif c['net'] < EXIT_NET_APR and age >= MIN_HOLD_DAYS:
            actions.append(f"平仓 {p['coin']:<10}{p['short_on']}/{p['long_on']:<14} "
                           f"扣费名义年化估算 {p['entry_net']:.1%} -> {c['net']:.1%}，跌破 {EXIT_NET_APR:.0%}")
            logged.append(('平仓', p['coin'], p['short_on'], p['long_on'], c['net']))
        else:
            why = '未到最短持仓' if c['net'] < EXIT_NET_APR else ''
            keep.append(p)
            actions.append(f"持有 {p['coin']:<10}{p['short_on']}/{p['long_on']:<14} "
                           f"扣费名义年化估算 {c['net']:.1%}，已持 {age:.1f} 天 {why}")
            logged.append(('持有', p['coin'], p['short_on'], p['long_on'], c['net']))

    open_keys = {(p['coin'], p['short_on'], p['long_on']) for p in keep}
    held_coins = {p['coin'] for p in keep}          # 同一个币不重复开，避免集中在一个标的
    for key, c in sorted(cand.items(), key=lambda kv: -kv[1]['net']):
        if len(keep) >= MAX_POSITIONS:
            break
        coin, hi, lo = key
        if key in open_keys or coin in held_coins or c['net'] < ENTRY_NET_APR:
            continue
        keep.append({'coin': coin, 'short_on': hi, 'long_on': lo, 'opened': today,
                     'opened_ts': now, 'entry_net': c['net']})
        held_coins.add(coin)
        actions.append(f"开仓 {coin:<10}{hi}/{lo:<14} 扣费名义年化估算 {c['net']:.1%}"
                       f"（毛 {c['gross']:.1%}），弱腿量 {c['vol']/1e6:.0f}M，本位 {c['settles']}")
        logged.append(('开仓', coin, hi, lo, c['net']))

    for a in actions:
        print(' ', a)
    if not actions:
        print('  无动作')

    # 不按开仓线过滤：全空的时候也要看得见最好的候选离门槛差多少
    ok = sum(1 for c in cand.values() if c['net'] >= ENTRY_NET_APR)
    print(f'\n== 候选池 Top 15（★ = 够到 {ENTRY_NET_APR:.0%} 开仓线，共 {ok}/{len(cand)} 个）==')
    print(f" {'coin':<9}{'空在':<15}{'多在':<15}{'扣费估算':>8}{'毛APR':>8}{'弱腿量':>9}")
    n = 0
    for (coin, hi, lo), c in sorted(cand.items(), key=lambda kv: -kv[1]['net']):
        if coin in held_coins:
            continue
        mark = '★' if c['net'] >= ENTRY_NET_APR else ' '
        print(f"{mark}{coin:<9}{hi:<15}{lo:<15}{c['net']:>7.1%}{c['gross']:>8.1%}{c['vol']/1e6:>8.0f}M")
        n += 1
        if n >= 15:
            break

    new = not os.path.exists(LOG)
    with open(LOG, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['time', 'action', 'coin', 'short_on', 'long_on', 'net_apr'])
        for act, coin, hi, lo, net in logged:
            w.writerow([time.strftime('%Y-%m-%d %H:%M'), act, coin, hi, lo,
                        '' if net is None else f'{net:.4f}'])

    with open(STATE, 'w') as f:
        json.dump({'positions': keep}, f, indent=2, ensure_ascii=False)
    print(f'\n持仓已写入 {STATE}（{len(keep)} 个），决策已追加到 {LOG}。这是空跑，没有下任何单。')


if __name__ == '__main__':
    main()
