"""验证费率偏差稳不稳：拉 28 天历史，按周切片，看各所的费率排名是否每周都成立。
复用 scan.py 的交易所列表和过滤逻辑。用法：python3 verify.py
"""
import ccxt, collections, csv, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor
from scan import (PROXY, EXCHANGES, STABLES, MIN_VOL_USD,
                  clean, true_interval)

WEEKS = 4                       # 回看几周
TOP = 25
WATCH = sys.argv[1] if len(sys.argv) > 1 else 'BTC'   # 单独盯哪个币
# 四腿 taker 开平（约 0.22%）按 30 天持仓摊薄后的年化拖累，用来把毛价差折成净的
COST_APR = 0.0022 * 365 / 30


def pull_history(ex, sym, since):
    """分页拉满整个窗口。1 小时结算的币 28 天有 672 期，多数所单次返回覆盖不到，
    而 since 是从旧往新给，不分页就只拿到最老的几周、最近一周反而是空的。"""
    seen, cursor = {}, since
    for _ in range(12):
        try:
            h = ex.fetch_funding_rate_history(sym, since=cursor, limit=1000)
        except Exception as e:
            print(f'{sym} INVALID_DECLARED_GAP: history_fetch_failed:{type(e).__name__}')
            return []
        h = [x for x in h if x.get('timestamp') and x.get('fundingRate') is not None]
        if not h:
            break
        for x in h:
            seen[x['timestamp']] = x['fundingRate']
        newest = max(x['timestamp'] for x in h)
        if newest <= cursor:          # 没往前走，说明该所忽略 since，再拉也是同一批
            break
        cursor = newest + 1
    else:
        print(f'{sym} INVALID_DECLARED_GAP: pagination_limit')
        return []
    return sorted(seen.items())


def fetch(name):
    """返回 [(exchange, coin, 第几周, 该周平均费率年化)]"""
    try:
        cfg = {'timeout': 30000, 'options': {'defaultType': 'swap'}}
        if PROXY:
            cfg['httpsProxy'] = PROXY
        ex = getattr(ccxt, name)(cfg)
        ex.load_markets()
        tickers = ex.fetch_tickers()
    except Exception as e:
        print(f'[{name}] 失败: {type(e).__name__}: {str(e)[:80]}')
        return []

    # 挑出够量的 USDT/USDC 本位永续
    syms = []
    for m in ex.markets.values():
        if not (m.get('swap') and m.get('linear') and m.get('settle') in STABLES
                and m.get('active') is not False):
            continue
        t = tickers.get(m['symbol']) or {}
        px = t.get('last') or t.get('close')
        cands = []
        if t.get('baseVolume') and px:
            cands.append(t['baseVolume'] * px * (m.get('contractSize') or 1))
        if t.get('quoteVolume'):
            cands.append(float(t['quoteVolume']))
        if cands and min(cands) >= MIN_VOL_USD:
            syms.append((m['symbol'], m['base']))

    since = ex.milliseconds() - WEEKS * 7 * 86400_000
    week_ms = 7 * 86400_000
    now = ex.milliseconds()
    out, cover = [], []
    for sym, coin in syms:
        pts = clean([{'timestamp': ts, 'fundingRate': r}
                     for ts, r in pull_history(ex, sym, since)], since)
        if not pts:
            continue
        cover.append((pts[-1][0] - pts[0][0]) / 86400_000)
        iv_h = true_interval(pts)
        if iv_h is None:
            print(f'[{name}] {sym} INVALID_DECLARED_GAP: interval_unknown')
            continue
        buckets = collections.defaultdict(list)
        for ts, rate in pts:
            w = int((now - ts) // week_ms)          # 0 = 最近一周
            if 0 <= w < WEEKS:
                buckets[w].append((ts, rate))
        for w, vals in buckets.items():
            interval = iv_h * 3600_000
            start, end = now - (w + 1) * week_ms, now - w * week_ms
            if (vals[0][0] - start > interval * 1.5 or end - vals[-1][0] > interval * 1.5
                    or any(b[0] - a[0] > interval * 1.5 for a, b in zip(vals, vals[1:]))):
                print(f'[{name}] {sym} week={w} INVALID_DECLARED_GAP: incomplete_week')
                continue
            # 保留合约身份，避免同所多本位在后面的字典中相互覆盖。
            out.append((name, sym, w, sum(r for _, r in vals) / len(vals) * (24 / iv_h) * 365))
    cov = f'{statistics.median(cover):.0f}' if cover else '0'
    print(f'[{name}] {len(syms)} 个永续，{len(out)} 个"币×周"样本，历史覆盖中位 {cov} 天')
    return out


def main():
    with ThreadPoolExecutor(len(EXCHANGES)) as pool:
        rows = [r for rs in pool.map(fetch, EXCHANGES) for r in rs]

    # 表 1：各所每周的费率中位数——排名稳定说明是结构性偏差，乱跳说明是噪音
    g = collections.defaultdict(list)
    for ex, coin, w, apr in rows:
        g[(ex, w)].append(apr)
    exes = sorted({ex for ex, _ in g}, key=lambda e: -statistics.median(g.get((e, 0), [0])))
    print(f'\n== 各所费率年化中位数，按周（第0周=最近7天）==')
    print(f"{'exchange':<15}" + ''.join(f'{"第"+str(w)+"周":>10}' for w in range(WEEKS)) + f"{'样本/周':>9}")
    for e in exes:
        line = f'{e:<15}'
        for w in range(WEEKS):
            v = g.get((e, w))
            line += f'{statistics.median(v):>9.1%}' if v else f'{"-":>10}'
        n = len(g.get((e, 0), []))
        print(line + f'{n:>9}')

    # 落盘，方便自己再分析
    stamp = time.strftime('%Y%m%d_%H%M')
    with open(f'verify_{stamp}.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['exchange', 'symbol', 'week', 'apr'])
        w.writerows(rows)

    # 表 2：盯住的那个币，各所每周——看异常是不是一直存在
    w2 = {(ex + ' ' + sym, w): apr for ex, sym, w, apr in rows if sym.split('/')[0] == WATCH}
    exes2 = sorted({ex for ex, _ in w2}, key=lambda e: -w2.get((e, 0), 0))
    print(f'\n== {WATCH} 各所费率年化，按周 ==')
    print(f"{'exchange':<15}" + ''.join(f'{"第"+str(w)+"周":>10}' for w in range(WEEKS)))
    for e in exes2:
        line = f'{e:<15}'
        for w in range(WEEKS):
            v = w2.get((e, w))
            line += f'{v:>9.1%}' if v is not None else f'{"-":>10}'
        print(line)


    # 表 3：研究候选——同一个币在两个所都有，且价差四周都站得住。
    # 场子中位数只说明偏差存在，能吃到多少要看具体币对具体所。
    m = {}
    for ex, sym, w, apr in rows:
        m.setdefault((sym.split('/')[0], ex + ' ' + sym), {})[w] = apr
    pairs = collections.defaultdict(list)
    for (coin, ex), wk in m.items():
        pairs[coin].append((ex, wk))
    cand = []
    for coin, lst in pairs.items():
        for i, (a, wa) in enumerate(lst):
            for b, wb in lst[i + 1:]:
                if a.split()[0] == b.split()[0]:
                    continue
                common = set(wa) & set(wb)
                if len(common) < WEEKS:            # 四周都得有数据
                    continue
                d = [wa[w] - wb[w] for w in common]
                # 空在费率高的一边；取最差那一周作为下限
                hi, lo, worst = (a, b, min(d)) if sum(d) > 0 else (b, a, -max(d))
                cand.append((worst, coin, hi, lo, statistics.median([abs(x) for x in d])))
    cand.sort(reverse=True)
    print(f"\n== 稳定配对：扣手续费后的名义年化估算（固定成本年化 {COST_APR:.1%}）；未含滑点/基差/总资金占用 ==")
    print(f"{'coin':<10}{'空在':<15}{'多在':<15}{'最差周估算':>10}{'中位估算':>9}")
    n = 0
    for worst, coin, hi, lo, med in cand:
        if worst - COST_APR <= 0:
            break
        print(f'{coin:<10}{hi:<15}{lo:<15}{worst - COST_APR:>9.1%}{med - COST_APR:>9.1%}')
        n += 1
        if n >= TOP:
            break
    if n == 0:
        print('  （没有任何配对做到四周都为正）')
    print(f'\n已存 verify_{stamp}.csv')


if __name__ == '__main__':
    main()
