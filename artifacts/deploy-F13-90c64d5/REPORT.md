# F13 部署与自动退出启用（2026-09-09）

用户授权部署、提交、重启。代码提交 90c64d5，发布 /home/sean/funding-arb/releases/f13-90c64d5。远端 18 组、338 项检查全部通过。

仅替换根目录 paper_loop.py、exit_a.py，其他运行模块预先逐一核对与发布一致。停止 paper 服务后确认无 pending/EXECUTING，备份账本、原代码与服务单元，切换期间账本字节不变。未操作 SQLite 或真实账户。

原 paper 服务参数仅增加 --auto-exit，不设置 --max-hold-hours；本金 20000、单腿 2000、单币双腿上限 5000、组合双腿上限 10000、最多 2 币、备用金 1000 保持不变。Restart=no，仍未设置开机自启。

重启后 active/running、NRestarts=0；新状态包含 EXIT_CHECKED，扫描进程存活且成功时间更新。BTC、ETH 均 HOLD/NO_EXIT_TRIGGER，两笔模拟仓位保持 open，pending=null。源码哈希一致，具体时点见 verification.json。

backup/ 含原单元、原 paper_loop.py 及停止时模拟账本证据。回退仅还原代码与单元；不得回滚持续运行后的账本或持仓。停止时备份不是可随意恢复的资金快照。

本地 21 个原有状态/研究文件不变。未真实下单、撤单、划转或改变账户模式。未推送本轮 Git 提交。F14 未开始，自动退出启用不等于完整 F15/F16 无人值守或实盘验收。
