# 2026-09-15 运行复核与修复

复核上线后约 23.6 小时：paper 和通知服务 active/running，自动重启 0，账本审计通过，运行源码与 aa02d6a 一致。没有新增交易、订单或手续费；BTC 保留 0.0253 币对冲，ETH 空仓。三笔资金费合计 +0.294641184964383 USDT，累计资金费 2.993722767128643，手续费仍 26.934864365。03:48 附近新鲜报价估值 19978.0564，累计净盈亏 -21.9436 USDT，并非当日亏损；估算立即平仓后累计 -24.9454。通知回执无失败或未知。

83844 样本决策：WAIT_SCAN 74412、HOLD_CASH 9413、EXIT_REVIEW 18、POSITION_OR_FUNDING_REVIEW 1；最大样本间隔 8.009 秒。旧冻结期 INVALID_DECLARED_GAP，不能宣称通过。资金费随后全部 POSTED，无 EXPECTED_SETTLEMENT_MISSING。

原始响应证明结算切换仍短暂返回上一期 fundingTimestamp；有三次真实盘口过期（含第二个请求耗时导致第一个报价过期），严格阻断正确，不放宽新鲜度或验收规则。确认代码缺陷：collect_funding 丢弃 public_price 的失败原始响应并伪装成成功空响应，CachedFunding 继而把接口错误或未确认蜡烛统一报成缺失；该次过去事件的真正价格失败原因已无法追认。

修复 paper_funding.py/paper_loop.py：完整缓存公共价格响应，失败也保存到结算证据；网络未返回使用 null，并明确 MARK_FETCH_FAILED，不制造成功响应。API 错误、未确认蜡烛继续 INVALID_DECLARED_GAP/null，现金不动，不补造数据。既有检查中新增端到端场景覆盖三类故障。本地 411 项通过（含未部署 F20 19 项），通知专项通过。历史证据和账本均保留。

## 已部署

提交 e1d6681，发布 releases/f16-evidence-e1d6681，远端 392 项及通知专项通过。仅更新 paper_funding/paper_loop，通知脚本沿用同内容的新发布路径。paper/通知 PID 1367895/1367896，active/running、NRestarts=0；扫描成功记录新鲜、pending=false、源码与配置匹配。锁内账本与通知游标字节不变，旧回执不变，无重发。

新目录 evidence/f16-evidence-observation-20260914T195239Z，北京时间 09-15 03:52:39 至最早 09-18 03:52:39。22 样本 OBSERVING/gaps=[]，未验收。旧期 84071 样本保留，旧失败不撤销。重启后新鲜报价净值 19978.0741、累计净盈亏 -21.9259；报价会变。无实盘交易、未推送。
