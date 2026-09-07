# funding-arb-scanner

跨交易所资金费率（funding rate）套利扫描器。**只读，不下单。**

一个 Python 文件，用 ccxt 拉 17 家 CEX/DEX 的永续合约资金费率，按成交量过滤，
用过去 7 天的历史均值算年化和稳定性，输出三张表：

1. **单所费率榜** — 哪些币的资金费率绝对值最高
2. **跨所费率差** — 同一个币在 A 所开空、B 所开多，价差年化多少、几天回本
3. **模式 A 候选** — 同一个所买现货 + 开空永续，扣掉手续费后的净年化

## 用法

```bash
pip3 install ccxt
python3 scan.py
```

需要代理就 `export SCAN_PROXY=http://127.0.0.1:7890`，不需要就 `export SCAN_PROXY=''`。
默认值是 Hiddify 的混合端口 12334。

结果打印到终端，同时存成 `funding_<时间戳>.csv` 和 `spread_<时间戳>.csv`。

## 可调参数（都在文件顶部）

| 参数 | 默认 | 含义 |
|------|------|------|
| `MIN_VOL_USD` | 1000 万 | 24h 成交额门槛，挡掉没法真正建仓的小币 |
| `LOOKBACK_DAYS` | 7 | 历史均值回看天数 |
| `MIN_HIST_DAYS` | 5 | 历史不足这么多天不进榜，挡掉新上市合约 |
| `MIN_SPREAD_APR` | 10% | 跨所价差年化低于此不进榜 |
| `MIN_SAME_SIGN` | 85% | 两腿的费率方向稳定性都得过线 |
| `HOLD_DAYS` | 30 | 估算净收益时假设的持仓天数，开平成本按这个摊薄 |
| `MAJORS` / `MAJORS_ONLY` | 32 个主流币 / True | 模式 A 的币池 |
| `STABLES` | USDT/USDC/USD | 认哪些稳定币做保证金 |

## 踩过的坑（这些是这个脚本存在的理由）

裸调 ccxt 拿到的数据有一堆陷阱，每一条都会让你算出好看但假的年化：

**结算周期交易所会报错。** Binance 的 `fetch_funding_rates` 走 premiumIndex 接口，
不返回 `fundingIntervalHours`，ccxt 给 `None`。如果按 8 小时兜底，而实际是 4 小时，
年化就少算一半。本脚本改用**历史时间戳的间隔中位数**倒推真实周期，发现被 limit
截断还会补拉一次。

**合约的 `baseVolume` 是张数，不是币量。** 要乘 `contractSize` 才是币量。
mexc 的 BTC 一张 0.0001 币，不乘就虚高一万倍——曾经算出 22 万亿美元的日成交额。
`quoteVolume` 各所口径也不统一（okx 返回的是币量不是美元）。本脚本两种算法都跑，取小的。

**瞬时费率没有意义。** 单期费率乘 1095 折出来的「年化 500%」到处都是，
7 天一平大概率掉到几十。而且要看**同号占比**：过去 7 天费率保持同一方向的期数比例，
50% 等于抛硬币，开仓吃两期就被反向费率吐回去还赔手续费。

**新上市合约的「7 天均值」可能只有 4 期数据。** 上市初期费率剧烈波动是常态，
等它稳下来就没了。所以有 `MIN_HIST_DAYS`。

**币名不等于合约。** 同一个所的 BTC 可能有 USDT / USDC / USD1 好几种本位，
按币名配对会把 USDC 合约和 USDT 合约当成一个东西。本脚本按 (币, 本位) 区分。

**有些所的费率是假的。** bingx 一半以上合约的 `lastFundingRate` 直接返回默认值
0.0001，37 行里 23 行年化一模一样。已从列表移除。

## 关于本位（settle）

跨所对腿时两边保证金币种**可以不同**：在 USDC 本位的所做多 BTC、在 USDT 本位的所做空 BTC，
BTC 敞口依然中性，只是同时持两种稳定币，多担一点脱锚风险（历史极端情况约 0.1~0.3%）。
所以 backpack / hyperliquid / pacifica / woofipro 这些 USDC 本位的场子照样能配对。

模式 A 不行——现货和永续必须同本位，用 USDT 买的现货对不上 USDC 保证金的空单。

## 交易所

已接入：binance、bybit、okx、bitget、gate、coinex、bitmex、krakenfutures、
hyperliquid、aster、pacifica、woofipro、backpack、mexc、blofin、kucoinfutures、phemex

没接入的和原因：

- **htx** — ccxt 只实现了它币本位的批量 funding，linear 直接抛 `NotSupported`
- **bingx** — 费率数据是假的，见上
- **lighter** — ccxt 不支持它的 funding 历史
- **paradex / extended / apex** — 连单个 funding 接口都没有
- **dydx** — 没有现货
- **bitrue / latoken** — 没有 funding 历史

没有批量 `fetchFundingRates` 的所（backpack、mexc、blofin、kucoinfutures、phemex）
会逐个 symbol 拉，慢一些但能出数。

## 免责

这是个人项目，只做数据扫描，不构成投资建议。资金费率套利看起来中性，
实际有爆仓、下架、单腿失败、交易所跑路等风险，真金白银之前请自己验证每一个数字。

## License

MIT
