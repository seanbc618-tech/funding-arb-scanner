"""资金费率只读扫描器：拉各所永续 funding，按成交量过滤，用过去 N 天历史均值算年化和稳定性。不下单。
用法：pip3 install ccxt && python3 scan.py
"""
import ccxt, csv, os, time, math
from statistics import median
from concurrent.futures import ThreadPoolExecutor

# 默认直连（VPS 上通常不需要代理）。本机被墙就 export SCAN_PROXY=http://127.0.0.1:12334
PROXY = os.environ.get('SCAN_PROXY', '')
EXCHANGES = [
    # 大所
    # htx 去掉：ccxt 只实现了它币本位的批量 funding，linear 直接 NotSupported
    # bingx 去掉：一半以上合约的 lastFundingRate 返回默认值 0.0001，费率是假的
    # blofin 去掉：公开行情接口被 Cloudflare 挡（403 + "Just a moment..."），换浏览器 UA 也过不去。
    # 它的费率偏差是全场最大的，但连不上的场子没法管仓位——开得进平不出更危险。
    'binanceusdm', 'bybit', 'okx', 'bitget', 'gate', 'coinex', 'bitmex', 'krakenfutures',
    # 小所 / DEX（没有批量接口的会逐个拉，慢但能出数）
    'hyperliquid', 'aster', 'pacifica', 'woofipro',
    'backpack', 'mexc', 'kucoinfutures', 'phemex',
]
# 认这些美元稳定币做保证金。跨所对腿时两边本位可以不同（BTC 空在 USDT 所、多在 USDC 所
# 依然是 BTC 中性的，只是同时持两种稳定币，多担一点脱锚风险）；模式 A 则必须现货和永续同本位。
# 不收 USDE/USDH/USDT0 这类合成稳定币。
STABLES = {'USDT', 'USDC', 'USD'}
MIN_SPREAD_APR = 0.10       # 跨所价差年化低于此不进榜
MIN_SAME_SIGN = 0.85        # 两腿同号占比都得过这条线，否则费率来回翻，吃不到
# 模式 A 的币池：主流币白名单。想放宽就往里加，或把 MAJORS_ONLY 设成 False
MAJORS = {
    'BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'DOGE', 'ADA', 'TRX', 'LINK', 'AVAX',
    'DOT', 'LTC', 'BCH', 'XLM', 'ATOM', 'UNI', 'ETC', 'FIL', 'NEAR', 'APT',
    'ARB', 'OP', 'AAVE', 'SUI', 'TON', 'ICP', 'INJ', 'TAO', 'XMR', 'ZEC', 'DASH', 'AR',
}
MAJORS_ONLY = True
MIN_VOL_USD = 10_000_000    # 24h 成交额门槛，低于此不看（挡掉没法真正建仓的小币）
LOOKBACK_DAYS = 7           # 历史均值回看天数
MIN_HIST_DAYS = 5           # 历史不足这么多天不进榜（新上市合约没法判断）
HOLD_DAYS = 30              # 估算净收益时假设的持仓天数（开平成本按这个摊薄）
DEFAULT_INTERVAL_H = 8      # 交易所连结算周期带历史都给不出时的兜底
DEFAULT_TAKER = 0.0005      # 交易所没报手续费时按 0.05% 算
TOP = 25


def clean(hist, since):
    """历史记录 -> [(时间戳, 费率)]，按时间排序，丢掉超期和空值"""
    return sorted({x['timestamp']: x['fundingRate'] for x in hist
                   if x.get('timestamp') and x['timestamp'] >= since
                   and x.get('fundingRate') is not None
                   and math.isfinite(x['fundingRate'])}.items())


def usable(row):
    return (row.get('data_status') == 'VALID' and row.get('apr_7d') is not None
            and math.isfinite(row['apr_7d']) and row.get('days', 0) >= MIN_HIST_DAYS
            and (row.get('vol_usd') or 0) >= MIN_VOL_USD)


def mode_a_candidates(rows):
    return sorted((r for r in rows if usable(r) and r.get('apr_a_net') is not None
                   and r['apr_7d'] > 0 and r.get('positive_share', 0) >= .9
                   and math.isfinite(r.get('spot_vol_usd') or 0)
                   and (r.get('spot_vol_usd') or 0) >= MIN_VOL_USD
                   and (not MAJORS_ONLY or r['coin'] in MAJORS)),
                  key=lambda r: -r['apr_a_net'])


def true_interval(pts):
    """从相邻结算时间戳的间隔中位数推真实结算周期（小时）；点太少返回 None"""
    if len(pts) < 3:
        return None
    gaps = [pts[i + 1][0] - pts[i][0] for i in range(len(pts) - 1)]
    return max(1, round(median(gaps) / 3600_000))


def fetch(name):
    """返回该所所有入选永续的 dict 列表，失败返回 []"""
    try:
        cfg = {'timeout': 30000, 'options': {'defaultType': 'swap'}}
        if PROXY:
            cfg['httpsProxy'] = PROXY
        ex = getattr(ccxt, name)(cfg)
        ex.load_markets()
        syms = [m['symbol'] for m in ex.markets.values()
                if m.get('swap') and m.get('linear') and m.get('settle') in STABLES
                and m.get('active') is not False]
        if not syms:
            got = sorted({str(m.get('settle')) for m in ex.markets.values()
                          if m.get('swap') and m.get('linear')})
            print(f'[{name}] 没有 {sorted(STABLES)} 本位的线性永续，该所实际 settle: {got[:6]}')
            return []
        if ex.has.get('fetchFundingRates'):
            rates = ex.fetch_funding_rates()
        else:                      # 小所常没有批量接口，逐个拉
            rates = {}
            for sym in syms:
                try:
                    rates[sym] = ex.fetch_funding_rate(sym)
                except Exception:
                    pass
    except Exception as e:
        print(f'[{name}] 失败: {type(e).__name__}: {str(e)[:100]}')
        return []
    try:
        tickers = ex.fetch_tickers()
    except Exception as e:
        tickers = {}
        print(f'[{name}] INVALID_DECLARED_GAP: volume_fetch_failed ({type(e).__name__})')

    spot = {}                          # (base, 计价币) -> 该所现货；模式 A 要求现货计价 = 永续本位
    for m in ex.markets.values():
        if m.get('spot') and m.get('quote') in STABLES and m.get('active') is not False:
            spot[(m['base'], m['quote'])] = m
    # 现货腿也得有量才做得了模式 A。行情按 defaultType 分流，现货要单开一个实例拉
    spot_tickers = {}
    if spot:
        try:
            exs = getattr(ccxt, name)({**cfg, 'options': {'defaultType': 'spot'}})
            exs.load_markets()
            spot_tickers = exs.fetch_tickers()
        except Exception as e:
            print(f'[{name}] 取不到现货成交量（{type(e).__name__}），该所模式 A 跳过')

    rows, thin = [], 0
    for sym, r in rates.items():
        m = ex.markets.get(sym)
        if not m or m.get('active') is False or not m.get('swap') or not m.get('linear') or r.get('fundingRate') is None:
            continue
        if m.get('settle') not in STABLES:     # USD1 之类的非美元稳定币跳过
            continue
        t = tickers.get(sym) or {}
        px = t.get('last') or t.get('close')
        # 合约的 baseVolume 常是"张数"不是币量，要乘每张面值；quoteVolume 各所口径也不统一，
        # 两个都算一遍取小的，避免把虚高上万倍的量当成深度
        cands = []
        if t.get('baseVolume') and px:
            cands.append(t['baseVolume'] * px * (m.get('contractSize') or 1))
        if t.get('quoteVolume'):
            cands.append(float(t['quoteVolume']))
        vol = min(cands) if cands else None
        if vol is not None and (not math.isfinite(vol) or vol < MIN_VOL_USD):
            thin += 1
            continue
        iv = r.get('interval')
        iv_h = int(iv.rstrip('h')) if iv else DEFAULT_INTERVAL_H
        rate = float(r['fundingRate'])
        if not math.isfinite(rate):
            print(f'[{name}] {sym} INVALID_DECLARED_GAP: funding_nonfinite')
            continue
        settle = m['settle']
        sp = spot.get((m['base'], settle))
        st = spot_tickers.get(sp['symbol']) if sp else None
        spx = (st.get('last') or st.get('close')) if st else None
        svol = st['baseVolume'] * spx if st and st.get('baseVolume') and spx else (
            st.get('quoteVolume') if st else None)
        if svol is not None:
            svol = float(svol)
            if not math.isfinite(svol):
                svol = None
        rows.append({
            'coin': m['base'], 'exchange': name, 'symbol': sym, 'settle': settle,
            'rate_now': rate, 'interval_h': iv_h,
            'apr_now': rate * (24 / iv_h) * 365,
            'apr_7d': None, 'same_sign': None, 'positive_share': None, 'days': 0,
            'data_status': 'INVALID_DECLARED_GAP',
            'gap_reason': 'volume_missing' if vol is None else 'history_missing',
            'mode_a_gap': 'spot_missing' if not sp else ('spot_volume_missing' if svol is None
                           else ('spot_volume_below_minimum' if svol < MIN_VOL_USD else '')),
            'taker': m.get('taker') or DEFAULT_TAKER, 'vol_usd': vol,
            'has_spot': sp is not None,
            'spot_symbol': sp['symbol'] if sp else '',
            'spot_vol_usd': svol,
            'spot_taker': (sp.get('taker') if sp else None) or DEFAULT_TAKER,
            'apr_a_net': None,
        })

    # 逐个补历史均值；单元缺口留空并保存原因。
    since = ex.milliseconds() - LOOKBACK_DAYS * 86400_000
    done = 0
    for row in rows:
        if row['vol_usd'] is None:
            continue
        limit = int(LOOKBACK_DAYS * 24 / row['interval_h']) + 5
        try:
            h = ex.fetch_funding_rate_history(row['symbol'], since=since, limit=limit)
            pts = clean(h, since)
            # 交易所报的结算周期常常是错的（binance 很多山寨其实 4h，ccxt 拿不到就按 8h 兜底），
            # 真实周期用历史时间戳的间隔中位数算。周期报大了会导致条数被 limit 截断，补拉一次。
            iv_h = true_interval(pts) or row['interval_h']
            if iv_h < row['interval_h'] and len(h) >= limit:
                h = ex.fetch_funding_rate_history(
                    row['symbol'], since=since, limit=int(LOOKBACK_DAYS * 24 / iv_h) + 5)
                pts = clean(h, since)
                iv_h = true_interval(pts) or iv_h
        except Exception as e:
            row['gap_reason'] = f'history_fetch_failed:{type(e).__name__}'
            print(f'[{name}] {row["symbol"]} INVALID_DECLARED_GAP: {row["gap_reason"]}')
            continue
        if not pts:
            continue
        now = ex.milliseconds()
        row['days'] = (pts[-1][0] - pts[0][0]) / 86400_000 + iv_h / 24
        if (row['days'] < MIN_HIST_DAYS or pts[-1][0] > now
                or now - pts[-1][0] > iv_h * 3600_000 * 1.5
                or any(b[0] - a[0] > iv_h * 3600_000 * 1.5 for a, b in zip(pts, pts[1:]))):
            row['gap_reason'] = 'history_short_stale_or_gapped'
            continue
        vals = [r for _, r in pts]
        avg = sum(vals) / len(vals)
        pos = sum(1 for v in vals if v > 0) / len(vals)
        row['interval_h'] = iv_h
        row['apr_now'] = row['rate_now'] * (24 / iv_h) * 365
        row['apr_7d'] = avg * (24 / iv_h) * 365
        row['same_sign'] = max(pos, 1 - pos)      # 同号占比：越接近 1 说明方向越稳
        row['positive_share'] = pos
        row['data_status'], row['gap_reason'] = 'VALID', ''
        row['days'] = (pts[-1][0] - pts[0][0]) / 86400_000 + iv_h / 24
        # 模式 A 净年化：买现货+开空永续，扣掉两腿开平的 taker，按 HOLD_DAYS 摊薄
        if row['has_spot'] and (row['spot_vol_usd'] or 0) >= MIN_VOL_USD and row['apr_7d'] > 0:
            cost = 2 * (row['taker'] + row['spot_taker'])
            row['apr_a_net'] = row['apr_7d'] - cost * (365 / HOLD_DAYS)
        done += 1

    print(f'[{name}] {len(rows)} 个永续入选（{thin} 个量不足被滤），{done} 个拿到 {LOOKBACK_DAYS} 天历史')
    return rows


def main():
    with ThreadPoolExecutor(len(EXCHANGES)) as pool:
        rows = [r for rs in pool.map(fetch, EXCHANGES) for r in rs]
    stamp = time.strftime('%Y%m%d_%H%M')
    cols = ['coin', 'exchange', 'symbol', 'settle', 'rate_now', 'interval_h', 'apr_now',
            'apr_7d', 'same_sign', 'positive_share', 'days', 'data_status', 'gap_reason', 'mode_a_gap', 'taker', 'vol_usd',
            'has_spot', 'spot_symbol', 'spot_vol_usd', 'spot_taker', 'apr_a_net']

    with open(f'funding_{stamp}.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: -abs(r['apr_7d'] or 0)))

    # 1) 单所：7 天平均费率年化最高的（现货+永续对冲候选）
    hist = [r for r in rows if usable(r)]
    print(f'\n{len(rows)} 个入选，{len(hist)} 个有 {MIN_HIST_DAYS} 天以上历史可判断')
    hist.sort(key=lambda r: -abs(r['apr_7d']))
    print(f'\n== 单所 {LOOKBACK_DAYS}天平均 funding 年化 Top {TOP}（正=空头收费率，负=多头收费率）==')
    print(f"{'coin':<10}{'exchange':<14}{'APR_7d':>9}{'APR_now':>9}{'同号':>7}{'周期':>5}{'天数':>6}{'24h量':>9}")
    for r in hist[:TOP]:
        print(f"{r['coin']:<10}{r['exchange']:<14}{r['apr_7d']:>8.1%}{r['apr_now']:>9.1%}"
              f"{r['same_sign']:>7.0%}{str(r['interval_h'])+'h':>5}{r['days']:>6.1f}{r['vol_usd']/1e6:>8.0f}M")

    # 2) 跨所：同一币 7 天均值最高 vs 最低，空高多低
    by_coin = {}
    for r in hist:
        by_coin.setdefault(r['coin'], []).append(r)
    spreads = []
    for coin, rs in by_coin.items():
        if len(rs) < 2:
            continue
        hi, lo = max(rs, key=lambda r: r['apr_7d']), min(rs, key=lambda r: r['apr_7d'])
        if hi['exchange'] == lo['exchange']:
            continue
        spread = hi['apr_7d'] - lo['apr_7d']
        cost = 2 * (hi['taker'] + lo['taker'])        # 两腿开+平，taker 计
        if spread < MIN_SPREAD_APR or min(hi['same_sign'], lo['same_sign']) < MIN_SAME_SIGN:
            continue
        spreads.append({
            'coin': coin, 'short_on': hi['exchange'], 'short_apr_7d': hi['apr_7d'],
            'long_on': lo['exchange'], 'long_apr_7d': lo['apr_7d'], 'spread_apr_7d': spread,
            'settles': f"{hi['settle']}/{lo['settle']}",
            'roundtrip_cost': cost,
            'breakeven_days': cost / (spread / 365) if spread > 0 else float('inf'),
            'min_same_sign': min(hi['same_sign'], lo['same_sign']),
            'min_leg_vol_usd': min(hi['vol_usd'], lo['vol_usd']),
        })
    spreads.sort(key=lambda s: -s['spread_apr_7d'])
    with open(f'spread_{stamp}.csv', 'w', newline='') as f:
        w = csv.DictWriter(f, list(spreads[0]) if spreads else ['coin'])
        w.writeheader()
        w.writerows(spreads)
    print(f'\n== 跨所 {LOOKBACK_DAYS}天平均费率差（价差>={MIN_SPREAD_APR:.0%} 且两腿同号>={MIN_SAME_SIGN:.0%}）==')
    print(f"{'coin':<10}{'空在':<15}{'多在':<15}{'价差':>8}{'成本':>7}{'回本天':>7}{'同号':>6}{'弱腿量':>9}  本位")
    for s in spreads[:TOP]:
        print(f"{s['coin']:<10}{s['short_on']:<15}{s['long_on']:<15}{s['spread_apr_7d']:>7.1%}"
              f"{s['roundtrip_cost']:>7.2%}{s['breakeven_days']:>7.1f}{s['min_same_sign']:>6.0%}"
              f"{s['min_leg_vol_usd']/1e6:>8.0f}M  {s['settles']}")
    if not spreads:
        print('  （没有满足阈值的配对）')

    a = mode_a_candidates(hist)
    skipped = 0
    if MAJORS_ONLY:
        skipped = len([r for r in a if r['coin'] not in MAJORS])
        a = [r for r in a if r['coin'] in MAJORS]
    a.sort(key=lambda r: -r['apr_a_net'])
    print(f'\n== 模式A候选：正费率占比>=90%，扣手续费后的名义年化估算（按 {HOLD_DAYS} 天摊薄）；未含滑点/基差/总资金占用 ==')
    print(f"{'coin':<10}{'exchange':<14}{'扣费估算':>8}{'毛APR':>8}{'同号':>6}{'周期':>5}{'永续量':>9}{'现货量':>9}")
    for r in a[:TOP]:
        print(f"{r['coin']:<10}{r['exchange']:<14}{r['apr_a_net']:>7.1%}{r['apr_7d']:>8.1%}"
              f"{r['same_sign']:>6.0%}{str(r['interval_h'])+'h':>5}{r['vol_usd']/1e6:>8.0f}M"
              f"{r['spot_vol_usd']/1e6:>8.0f}M")
    no_spot = [r for r in hist if r['apr_7d'] > 0 and not r['has_spot']]
    thin_spot = [r for r in hist if r['apr_7d'] > 0 and r['has_spot']
                 and (r['spot_vol_usd'] or 0) < MIN_VOL_USD]
    print(f'（{len(no_spot)} 个正费率永续没有对应现货，{len(thin_spot)} 个现货腿量不足，'
          f'都做不了模式 A；{skipped} 个不在主流白名单被跳过）')

    print(f'\n已存 funding_{stamp}.csv / spread_{stamp}.csv')


if __name__ == '__main__':
    main()
