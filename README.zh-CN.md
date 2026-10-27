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
账户检查要求现货余额是明确的有限非负数，持仓及挂单接口返回列表；缺键、null 或非法响应均阻止动作，不能推断为空仓或无挂单。
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
F02 将结果拆为三个可同时查看的部分：`risk.status/reasons/gaps` 表示已知风险和风险资料缺口，
`execution.status/limitations` 表示当前动作限制，`data_status` 表示本次检查资料是否齐全。
例如盘口超时但保证金越线时，返回 `risk.status=EXIT_REQUIRED`、`execution.status=BLOCKED`、
`action=BLOCK`，风险原因仍保留。`CLEAR` 仅表示当前范围内未触发已有风险规则；
`risk.scope=local_and_public` 不包含账户验证。`READY` 不是交易授权，管理器仍只按顶层 `action` 决定。
风险证据有缺口但已经发现越线时保留 `EXIT_REQUIRED`，没有已知越线时为 `UNKNOWN`；
开仓检查的风险越线显示 `ENTRY_BLOCKED`，顶层为 `BLOCK`。
`checked_at/completed_at/evidence_at` 是本机检查/接口返回时间，不代表交易所原始更新时间。
超过 10 秒的整次检查、结束时已过 5 秒的盘口都会阻止动作，已经采集到的风险不会被清空。
`CLOSED` 仅适用于无 pending 且双腿确认为零的本地历史记录，不能证明真实账户无仓。

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
人工按次通知继续使用 notify_run.py；F03 新增独立 notify_events.py 读取只读监控快照发送事件，远端 OKX 凭据与通知配置分别保存在私有文件中。
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

本服务没有 `--live` 参数，不会自动交易。F03 已将插件发送脚本和私有通知配置单独部署到远端；Token 不进入仓库、日志和发布包。

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

### F03 常驻事件通知

`funding-arb-notify.service` 每 10 秒独立读取 `monitor_a_status.json`，不直接访问账户或调用交易入口。
状态、异常类别、本地阶段/确认余量/pending、快照中已有保证金与清算越线类别发生变化时通知。
相同事件跨重启去重；变化频繁时全局最少 300 秒发送一次，期间合并为最新快照，不保证逐条发送瞬态事件。
单纯时间戳和未越线的行情变化不触发消息。风险阈值沿用 check_a.py；仅能报告监控快照已包含的证据。

发送前将意图原子落盘为 ATTEMPTING，成功为 CONFIRMED，无法确认则 DELIVERY_UNKNOWN。
中断留下 ATTEMPTING 或未知回执时，同一事件不重发；此策略可能漏送而避免盲目重复。
状态文件损坏时暂停通知，不自动清空。通知异常只写白名单状态到独立服务日志，不重跑监控或业务。
通知状态 `notify_events_state.json` 权限 600、已忽略；不要删除它来重试未知投递。

消息只包含固定状态、枚举原因、受限资产代码及数字；不透传原始异常、账户标识或配置。
本地 open/closed 阶段只是观察，不作为成交证明。快照超过 180 秒或来自未来时不发送旧事实；
定时报表、外部失联告警属于 F06，F03 尚不提供。通知器失联不能依靠自己告警。

远端配置 `~/.config/funding-arb/notify.env` 指定 TELEGRAM_NOTIFY_SCRIPT、TELEGRAM_NOTIFY_PYTHON、
TELEGRAM_NOTIFY_CONFIG；通知凭据在单独 600 权限文件，不能粘贴进仓库或服务单元。
可用 `systemctl --user status funding-arb-notify.service` 检查状态，
用 `journalctl --user -u funding-arb-notify.service -n 20` 查看白名单回执。
当前实测、版本哈希及部署边界见 artifacts/F03/REPORT.md。

### F04 资金费与成交增量采集

`accounting.py` 保留按本地交易轮次核算入口，新增账户级原始证据入口：

```bash
# 凭据需已加载到当前进程环境；日期为 UTC。此命令仅示例，不会自动执行。
umask 077
python3 accounting.py --account-sync --since 2026-09-08
# 持续采集；Ctrl+C 停止，不安装任何后台服务。
python3 accounting.py --account-sync --since 2026-09-08 --interval 300
```

账户采集不要求已有策略持仓；`EVIDENCE_COLLECTED` 只证明指定接口窗口已查询，收益为 null。
SQLite 新增 collection_windows 记录各账户/数据流的时间覆盖与分页游标；成功页与游标同时提交。
重复 ID 不重复入库。evidence_conflicts 保存更正观察，原 evidence 不覆盖；存在冲突时阻止净收益结论，
不自动清除冲突或选取“最新值”。重叠复查最近七天，七天前迟发/更正需明确扩大复查窗口才能发现。

89 天之外若已有完整的本地查询覆盖，可继续使用真实证据；旧 evidence 行本身不证明窗口完整，
没有覆盖证明或采集中断跨越源窗口时保留 INVALID_DECLARED_GAP/null，不补造、不自动申请长历史。
账户级 ALL:SPOT/ALL:SWAP 与按合约采集分别记录覆盖，当前不自动将账户级覆盖转成策略轮次覆盖。
本版本未安装采集服务，常驻调度整合仍在 F12；目前的远端验证副本和独立原始数据库见 F04 报告。

### F05 只读持仓收益与资金报表

`python3 portfolio_a.py --db accounting_a.sqlite --markdown` 输出账户与策略分开的报表（省略 --markdown 输出 JSON）。
当前进程需有 OKX 三项凭据；systemd 环境不会自动注入普通 SSH 终端。该入口不包含 --live、不下单、不写账目库。
收益需要 F04 同账户、同合约与时段覆盖证据，以及本地确认订单；没有本策略记录不自动认领外部仓位。

分别展示已入账资金费、原币手续费、移动加权成本下已实现/未实现损益、盘口估计退出成本与退出后净损益。
现货币量手续费已经影响净数量及成本，不重复扣费；其他无换算依据的费用保留缺口。
当前估值使用新鲜盘口中间价，退出使用双腿 VWAP 和账户 taker 费率；不是成交或结算承诺。
已关闭且证据满足原会计校验时才显示查询时点已平仓净损益。

现货成本/市值用 USDT，OKX 账户净资产/初始保证金用 USD，分开显示。账户净资产含外部资产；
无策略资本分配记录时 strategy_nav_usdt、capital_return 为 null，不能以账户总净值冒充策略回报。
实例、验证和远端独立报表位置见 artifacts/F05/REPORT.md。报价、费率、覆盖或归属不足时字段保持 null。


### F06 定时报告与独立 watchdog

`report_watch.py` 由独立 systemd oneshot/timer 每分钟触发，检查监控快照、通知心跳与服务状态。阈值 180 秒；异常变化/恢复冷却 300 秒。进程存在但快照不更新也会告警。同机检查不能覆盖整台 VPS 或网络失联。

默认每小时摘要、北京时间 08:00 日报，日报包含当时摘要。支持 `--summary-hours 4` 或 `0`（仅日报和异常）；停机重启只报告当前时段，不补发大量历史消息。持久发送意图防重，未知回执不重试，不能随意删除 report_watch_state.json。

日报要求 F04 同账户完整自然日覆盖；缺口保持 null。账户资金费与费用不代表策略日收益，当前日级策略收益尚无完整证据。报表只读查询，未启动采集任务；默认本地数据库 accounting_a.sqlite，线上显式读取既有 F04 独立证据库。采集调度尚待 F12。

线上使用专用发布目录中的模块，原运行目录交易模块不变。`funding-arb-reports.timer` 应 enabled/active；oneshot 的 service 在完成后 inactive/dead 属正常，检查 Result=success。详细交付与回退见 artifacts/F06/REPORT.md。


### F01–F06 综合复核状态

2026-09-09 复核修复 5 处本地问题，198 项检查通过；修补尚未部署。详细差异与限制见 artifacts/F01-F06-review/REPORT.md。历史回归已恢复到 checks/funding_arb_regression.py 和 checks/funding_modules_regression.py，不再依赖临时目录。


### F07 持久执行状态与启动门槛

新周期保存交易 ID、两腿目标与确认余量、订单身份/确认状态、费用与敞口。`trade_a.py startup` 检查空跑状态；`--live startup` 查询真实账户并维护执行锁/路径绑定，不下单。挂单、未知订单、其他币种未完成动作、账户模式/归属或对账不符时暂停新增动作。

开/平/recover 的函数入口统一取得状态锁与账户锁。账户锁位于 `~/.local/state/funding-arb/execution-locks/`，同机同用户的不同 checkout 共用且绑定唯一状态路径；不可删除文件绕过绑定，也不提供跨主机保证。订单接受后中断只能查询原客户订单号，不能重发；已知但未终结的成交观察与终结确认余量分开。

旧无版本状态仍可只读查看，修改须先人工审阅迁移；本次不迁移、不部署、不启用实盘。默认空跑还不是 F08 的真实行情 paper 模型。验收与边界见 artifacts/F07/REPORT.md，F07 待审阅。


### 最新部署状态（2026-09-09）

F01–F07 与综合复核/F07 复核修补均已部署，发布目录 f01-f07-20260909-586bd8ed1c65。监控、事件通知、定时报表已恢复；F07 执行器仅安装，未启动交易。此前“未部署”段落为历史交付状态。验收、哈希和回退边界见 artifacts/deploy-F01-F07-20260909/REPORT.md。


## F08 真实盘口模拟成交（本地交付）

独立命令只读取公共行情，不需要 OKX 凭据。首次初始化显式填写模拟本金及费用率；以下费率只是示例假设，不代表你的真实账户等级。

```bash
python3 -B paper_a.py init --cash 10000 --spot-fee 0.001 --perp-fee 0.0005
python3 -B paper_a.py open BTC --notional 200
python3 -B paper_a.py status
python3 -B paper_a.py close BTC
```

`--notional` 是单腿目标 USDT 金额，两腿合计需要额外资金占用及费用。盘口不足会部分成交，余量取消；`needs_close` 保留已成交仓位，需要继续执行 close 处理剩余数量。`pending` 表示落盘过程未完成，禁止自动重试或删除账本；应先核查意图和状态。重复 init 不覆盖已有账本。

状态在项目目录 `paper_a_state.json`，与实盘状态隔离。永续采用 1 倍初始名义资金占用，费用均按 USDT 模拟；没有资金费结算、动态保证金/强平、连续调度或实盘成交保证。详见 [F08 验收报告](artifacts/F08/REPORT.md)。

F08 复核已修复限价取整的深度遗漏及平仓资金检查，详见 [复核报告](artifacts/F08-review/REPORT.md)。现货出售所得可支付模拟费用，平空同时核算释放保证金与价差亏损；资金不足时保留持仓并拒绝该次模拟成交。


## F09 单周期模拟资金费（本地复核及公共接口验证通过）

```bash
python3 -B paper_a.py settle BTC --at-ms <实际结算时间的毫秒时间戳>
python3 -B paper_a.py status
```

用历史资金费接口的 fundingTime 替换占位符，至少等待该分钟结束。按当时已确认空仓、实际结算费率和该分钟标记价格开盘价计算纸面资金费；分钟开盘价是模拟约定，不代表精确真实账单。重复调用不会重复支付。

结果保存在 paper_a_state.json 的 funding 字段；POSTED 为模拟记账，NO_POSITION 为当时无仓位，INVALID_DECLARED_GAP/payment_usdt=null 为缺数据。缺口不改变现金。旧 F08 订单缺执行时间不会自动补写或猜测，单个周期成功不代表全历史完整覆盖。F09 没有启动定时器或改真实账簿。详见 [F09 报告](artifacts/F09/REPORT.md)。

F09 公共接口补验已通过：真实已结算费率和标记价格驱动合成持仓记账、重启去重；全套 268 项检查通过。本轮未部署、未真实交易，详见 [F09 复核报告](artifacts/F09-review/REPORT.md)。


## F10 按金额的 OKX 净收益排名（本地交付）

```bash
python3 -B rank_a.py BTC ETH --notional 1000 --hold-hours 720 --margin-ratio 1 --reserve-usdt 100 --basis-stress-bps 50
```

复用既有 OKX 环境凭据，只读取账户费率和行情，输出 JSON。保证金比例和备用金是明确的场景参数，不代表账户实际可用资金。按预计净收益/总资金占用排名，缺数据、深度不足或净收益非正时排除；可全部返回 HOLD_CASH。CANDIDATE 只供审阅，不授权执行。

当前预测与历史已结算费率分列，四笔盘口/费用成本、预计收入、净收益、回本天数和基差压力都有明细。预测假设与数据限制见 [F10 报告](artifacts/F10/REPORT.md)。旧 scan.py 仍是原研究扫描入口，未替换为自动交易逻辑。


## F11 paper 组合预算（本地交付）

```bash
python3 -B allocate_a.py reserve ranking.json --per-coin-gross-usdt 2500 --total-gross-usdt 5000 --max-positions 2 --buffer-usdt 100
python3 -B allocate_a.py execute <reservation_id>
python3 -B allocate_a.py release <reservation_id>
```

额度按两腿名义之和，例值需自行确定；读取 F10 原始 JSON，paper 费率须一致且保证金比例为 1。预留扣除其他候选可用资金，部分成交保留实际占用、释放剩余。未知订单阻止继续执行。报价与预留仅有效 5 秒，过期需 release 并重新报价；F12 尚未提供自动串联。首次配置后直接 paper open 禁止绕过限额，close 保留。详见 [F11 报告](artifacts/F11/REPORT.md)。


## F12 paper 主循环（本地交付）

```bash
python3 -B paper_loop.py BTC ETH --cycles 10 --interval 1 --notional 1000 --hold-hours 720 --per-coin-gross-usdt 2500 --total-gross-usdt 5000 --max-positions 2 --buffer-usdt 100 --basis-stress-bps 50
```

先初始化 F08 paper 账本、配置账户只读费率凭据，保持 F11 限额与费率一致。扫描独立进程，主循环先检查已有持仓，后消费排名及资金费资料，再按预算模拟执行。默认一轮，--cycles 0 为持续模拟；首次 WAIT_SCAN 正常。Ctrl-C 停止，不平仓。状态输出在 paper_loop_status.json。

额度为示例；没有 live 路径或自动退出，缺口和未完成执行阻止新仓。报价过期不执行，网络请求仍可能延迟。详见 [F12 报告](artifacts/F12/REPORT.md)。

F10—F12 联合复核已修复执行数量与排名脱节、结算边界、过期持仓检查和整批预留归属问题，322 项检查通过；补丁尚未部署。详见 [联合复核报告](artifacts/F10-F12-review/REPORT.md)。

## F13 自动退出（本地待审阅）

在现有 paper_loop.py 命令后添加 `--auto-exit` 才启用；可选 `--max-hold-hours 720` 为明确最长持有时限，默认不设。优先处理敞口风险、已确认退出余量、负资金费率及显式期限；缺数据或未知订单暂停。退出使用原有保守盘口限制。当前线上服务尚未启用，详见 [F13 报告](artifacts/F13/REPORT.md)。
