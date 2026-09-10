# F14–F16 提交、部署与重启（2026-09-11）

用户授权提交、部署、重启。代码提交 da780e8，发布目录 /home/sean/funding-arb/releases/f16-da780e8；远端完整 377 项检查通过。

只更新 root 下 paper_loop.py、rank_a.py 并新增 rotate_a.py、recovery_a.py、paper_acceptance.py、paper_replay.py，其他 root Python 模块预先核对一致。取得 paper 账本锁后停止服务，确认 pending/EXECUTING 均无，备份原模块、单元及停止时纸面账本后切换；切换期间账本字节不变。backup 仅用于证据和代码回退，不得回滚随后已推进的账本。

paper 服务已重启：MainPID=1191366，active/running，NRestarts=0；新状态年龄约 0.25 秒，成功扫描约 1.87 秒，scanner_alive=true。运行模块哈希与发布一致；BTC 模拟仓 open，ETH 历史仓 closed，pending=false、EXECUTING=0。详见 verification.json 和 installed.json。

本次服务单元与运行参数完全保留：本金 20000、单腿 2000、组合双腿额度 10000，BTC/ETH，最多 2 币，备用金 1000。auto_exit=true；auto_recover=false、switch_policy=null，未启用 F16 证据目录。新功能已部署，但 F14/F15 自动运行开关和 F16 连续观察需先确定参数，不能把部署当完成连续验收。

本次未推送 Git；未真实买卖、撤单、划转或更改账户模式；未操作 SQLite。监控、通知和报告服务无需代码切换，本轮未重启它们。
