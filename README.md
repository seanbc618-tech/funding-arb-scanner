# funding-arb-scanner

资金费率研究与 OKX 模式 A 执行原型。扫描与跨所池子不下单；执行器默认空跑，显式 `--live` 才提交订单。

七个业务脚本加一个通知入口，依赖 ccxt（记账使用 Python 自带 SQLite），扫描 16 家 CEX/DEX 的永续合约资金费率，按成交量过滤，
用过去 7 天的历史均值算年化和稳定性，输出三张表：

1. **单所费率榜** — 哪些币的资金费率绝对值最高
2. **跨所费率差** — 同一个币在 A 所开空、B 所开多，价差年化多少、几天回本
3. **模式 A 候选** — 同一个所买现货 + 开空永续，扣手续费后的名义年化估算

## 用法

```bash
pip3 install ccxt
python3 scan.py
```

默认直连。本机被墙就 `export SCAN_PROXY=http://127.0.0.1:12334`（Hiddify 混合端口）。

结果打印到终端，同时存成 `funding_<时间戳>.csv` 和 `spread_<时间戳>.csv`。

研究与执行入口：

```bash
python3 verify.py [币名]    # 拉 28 天历史按周切片，验证费率偏差是不是稳定的（默认盯 BTC）
python3 pool.py             # 模式 C 跨所研究空跑，未接入模式 A
python3 trade_a.py candidates    # 只查 OKX 模式 A 候选（公共行情）
python3 trade_a.py open ZEC 200  # 空跑，200 是单腿目标名义，非总投入资金
python3 trade_a.py status
python3 trade_a.py close ZEC
```

实盘动作须显式加 `--live`，凭证使用 `OKX_API_KEY` / `OKX_SECRET` / `OKX_PASSWORD`。
当前仅支持 OKX Futures mode (`acctLv=2`)、净持仓 (`net_mode`)、cross 永续和 cash 现货。
程序只检查，不自动修改账户、杠杆或划转资金。同一币的现货余额和永续仓位不得由其它程序同时操作。

空跑和实盘分别保存到脚本目录下的 `positions_a_dry.json` / `positions_a_live.json`，
进程锁防止同模式并发操作，状态原子写入。旧 `positions_a.json` 非空只会阻止实盘开/平仓和 recover，需人工核对后迁移；空跑与只读检查不会自动读取或修改它。
订单号在提交前落盘；`create_order` 返回不等于成交。程序查询原单终态、实际成交量和手续费，
按扣除币本位手续费后的实际现货数量开空；平永续显式 `reduceOnly`。
真实开平仓均使用带价格上限的 IOC 限价单；未即时成交的部分由交易所撤销。
下单前重查盘口，记录限价、盘口时点和预估 VWAP；部分成交仍按实际余量恢复。

遇到超时、未终结订单、缺手续费资料：先运行 `python3 trade_a.py recover ZEC --live`。
它只查询原订单并核对账户，不补开、不重发、不自动撤单或回滚；查不到订单仍保留待核对记录。
确认后 `close ZEC --live` 按余量退出。每条腿成交后立即保存，部分平仓可再次 close；
低于最小数量的尘埃保留待人工处理。已平仓流水保留，status 会核对实际账户与本地余量。
真实账户和无人值守运行仍未验收；本项目没有安装任何交易定时任务。

## 模式 A 记账、检查与管理

```bash
python3 check_a.py ZEC --notional 200           # 公共盘口与计划对冲检查
python3 check_a.py ZEC --notional 200 --account # 再查真实账户模式、余额、保证金
python3 check_a.py ZEC --account                # 检查本地实盘持仓与真实账户
python3 accounting.py ZEC                      # 只读真实账户，采集当前轮成交/账单并核算
python3 accounting.py ZEC --previous 1         # 上一轮；不会更改仓位记录
python3 manage_a.py                            # 单次空跑状态检查与建议
python3 manage_a.py --account                  # 单次真实账户只读建议
python3 manage_a.py --live                     # 显式允许本次退出决策触发真实平仓
```

`check_a.py` 的 `--account` 和 `accounting.py` 仅查询私有接口，不提交交易。
实盘状态仍来自独立的 `positions_a_live.json`；旧状态迁移门禁继续有效。

已选保守默认规则（常量在 `check_a.py` 顶部）：

| 检查 | 默认边界 | 动作 |
|---|---|---|
| 盘口 | 两腿 50 档内可完成数量，单腿限价距最优价不超过 20bp | 不足则 BLOCK，平仓也不突破限价 |
| 时效 | 盘口不超过 5 秒，整次检查不超过 10 秒 | 数据缺失/陈旧则 BLOCK |
| 开仓基差 | 两腿可执行均价差绝对值不超过 100bp | 超过则禁止开仓 |
| 开仓资金 | 现货 + 按 1x 预留的永续名义，再加 10% 缓冲 | 不足则禁止开仓，不自动划转或改杠杆 |
| 净敞口 | 相对较大腿币量不超过 2% | 开仓禁止；持仓建议 EXIT |
| 保证金率 | OKX 原始 `mgnRatio` 大于 3（300%） | 越线建议 EXIT；缺失则 BLOCK |
| 空头清算距离 | `(清算价−标记价)/标记价` 大于 10% | 越线建议 EXIT；缺失则 BLOCK |
| 当前资金费率 | 非负 | 转负建议 EXIT（预测费率仍可能变化） |

这些是可修改的工程阈值，不是经实盘验证的最佳参数。跨保证金会受账户其它仓位影响，
管理器平掉本策略不保证解除整个账户的风险。策略不自动开仓、不追逐更高费率、不自动改杠杆。

`manage_a.py` 返回 HOLD / EXIT / BLOCK / CLOSED。opening、closing、needs_close 且资料可核对时
建议退出。只有 `--live` 且 EXIT 才调用平仓；pending、账户不匹配、盘口不足或资料不全一律 BLOCK。
退出失败返回 NEEDS_ATTENTION，保留订单和余量，下次须先 recover/核对再处理。
这是单次运行入口；不会常驻或自行安装 cron。行情、账户检查不是原子快照，IOC 可能部分成交。

`accounting.py` 把原始成交、账单和每次报告保存在被忽略的 `accounting_a.sqlite`，
按账户标识哈希、记录类型和 billId 去重；同 ID 内容变更报冲突，旧证据不覆盖。
用 billId 分页至空页，按订单 ID 核对成交总量和手续费；源头超过保守的 89 天窗口、
分页失败或记录不一致返回 INVALID_DECLARED_GAP / null，不自动申请长期归档或补造数据。

成交现金流和资金费率入账使用 Decimal 计算。币本位现货手续费通过减少可卖币量计入，
不重复扣费；其它无法计价的费用、未归属成交/账户扣费、未平仓、待确认订单均阻止净收益结论。
平仓至少一小时后且成交/手续费/账单核对通过才返回 `RECONCILED_AS_OF_QUERY` 和 `net_pnl_usdt`。
该状态只表示本次 API 窗口查询下已对账，不保证供应商档案之后不会补发资金费率或更正数据，
可再次查询复核。账户总投入资本尚未分配，所以资金收益率和年化收益率保留 null。

账单字段和游标依据 [OKX 账户账单文档](https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-bills-details-last-3-months)，
本轮使用离线 fixture 验证，真实账户档案完整性仍需上线前独立核对。

订单参数依据 [OKX V5 官方文档](https://www.okx.com/docs-v5/en/#order-book-trading-trade-place-order)，
并离线核对本机 ccxt 4.5.77 生成的请求；这不等于真实账户接口验收。

`candidates` 只提供研究筛选，`open` 是手动链路验证入口，不自动按 APR 下单。

收益公式：历史费率年化 − 两腿开平手续费率 × 365 / 假设持仓天数。
CSV 的旧字段 `apr_a_net`、pool 的 `net_apr` / `entry_net` 为兼容保留，均仅指此名义估算。
它未扣实际滑点、基差变化、融资成本，也未以现货资金加保证金等总投入为分母。
真实策略损益由 accounting 的实际流水核算，不能将扫描估算称为账户净收益率。

成交量/历史缺失标记 `INVALID_DECLARED_GAP`，原因进入扫描 CSV，APR 留空；不足五天、
过旧或有内部缺口的历史不进候选。模式 A 要求正费率占比 ≥90%，两腿成交额均 ≥1000万。
verify 按完整周检查端点和内部间隔，CSV 保留 `symbol` 身份（旧 CSV 不重写）。

`pool.py` 把持仓状态存在 `positions.json`，每次的开/持/平决策追加到 `pool_log.csv`。
开仓和平仓阈值之间留了滞后带（默认 8% 开 / 3% 平 / 最少持 3 天）——
换一次仓要付四笔 taker（约 0.22%，按 30 天摊薄是 2.7 个点年化），
用同一个阈值开平会被噪音推着来回换，比赚的还多。

## 可调参数（都在文件顶部）

### Telegram 运行通知

使用个人插件 Telegram Notifier（目标群由插件私有配置决定）。通过以下入口运行，每次结束后
发送一条摘要；失败也报告退出码，不发送完整日志、凭据、个人路径或服务器地址。

```bash
python3 notify_run.py manage_a.py              # 空跑检查与运行报告
python3 notify_run.py manage_a.py --account    # 真实账户只读检查与报告
python3 notify_run.py accounting.py ZEC        # 记账与报告
python3 notify_run.py scan.py                  # 扫描结束报告
```

本机自动发现唯一已安装插件版本及用户私有配置，不复制 Token 到项目。
其它环境通过 `TELEGRAM_NOTIFY_SCRIPT`、`TELEGRAM_NOTIFY_CONFIG`、`TELEGRAM_NOTIFY_PYTHON`
指定插件脚本、私有配置和 Python。也可沿用插件的 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`。
通知不确认时退出码为 2（业务本身失败则保留业务退出码），先检查群消息，禁止盲目重跑交易。
包装器不会追加 `--live`、自动恢复或启动定时任务；直接运行原脚本不会发送通知。
Telegram 通知目前仍是本机按次调用；常驻持仓监控由独立的只读 systemd 服务负责，远端 OKX 凭据需另存为私有环境文件。
当前远端服务已启用；在私有环境文件尚未配置前，状态会保持 `CONFIG_MISSING`，不会伪报账户为空。

### 常驻只读持仓监控

`monitor_a.py` 只查询本地实盘状态和 OKX 账户，不调用任何下单、撤单或平仓函数。
`--account` 需要 `OKX_API_KEY` / `OKX_SECRET` / `OKX_PASSWORD`；缺凭据时明确报告
`CONFIG_MISSING`，不会把“查不到账户”误报成“无仓位”。每轮 JSON 同时输出到日志并原子写入
`monitor_a_status.json`（权限 600）。

VPS 使用用户级 systemd（不需要 root）：

```bash
mkdir -p ~/.config/funding-arb
chmod 700 ~/.config/funding-arb
# 在 ~/.config/funding-arb/monitor.env 中写入三项私有 OKX_* 凭据，chmod 600
systemctl --user daemon-reload
systemctl --user enable --now funding-arb-monitor.service
systemctl --user status funding-arb-monitor.service
journalctl --user -u funding-arb-monitor.service -f
cat ~/funding-arb/monitor_a_status.json
```

本服务没有 `--live` 参数，不会自动交易。当前 Telegram Notifier 是本机插件；未把 Token
复制到仓库或远端。若要远端持续推送 Telegram，需另行把私有通知配置放在远端，再接入通知命令。

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
hyperliquid、aster、pacifica、woofipro、backpack、mexc、kucoinfutures、phemex

没接入的和原因：

- **htx** — ccxt 只实现了它币本位的批量 funding，linear 直接抛 `NotSupported`
- **bingx** — 费率数据是假的，见上
- **lighter** — ccxt 不支持它的 funding 历史
- **paradex / extended / apex** — 连单个 funding 接口都没有
- **dydx** — 没有现货
- **bitrue / latoken** — 没有 funding 历史

Blofin 当前因公开接口连接问题排除。

没有批量 `fetchFundingRates` 的所（backpack、mexc、kucoinfutures、phemex）
会逐个 symbol 拉，慢一些但能出数。

## 免责

这是个人研究与执行原型项目，不构成投资建议。资金费率套利看起来中性，
实际有爆仓、下架、单腿失败、交易所跑路等风险，真金白银之前请自己验证每一个数字。

## License

MIT
