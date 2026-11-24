# 数据库模块更新记录

已获取最新 `main`（`753ec0f`），接入数据库提交 `1e1c602`：批量 CSV 导入、规范化字段校验、坐标约束、用户时间索引、ANALYZE、导入锁、禁止覆盖已有数据库、源文件 SHA-256 报告、完整性和分片验收工具。

`db/load.py`、`db/schema.sql`、`db/node.py`、`db/verify.py` 与云端版本保持一致。网页继续通过原来的任务/批次编排调用新版 `load()`，每次写入独立暂存目录，保留核验后发布和历史回退，不覆盖现有快照。入库进度保存在 `load_progress`，导入报告保存在任务的 `database_import` 中，去除其中本机源路径后可下载。

任务流水线版本改为 `gowalla-clean-238168f-db-1e1c602-v1`，防止相同文件复用旧入库器的任务结果。清洗规则和 SHA-256 用户分片规则均未改变。已有已发布数据库不自动重建；未来导入使用新模块。

上游 `db.serve` 和 `db.verify --api-url` 基于未登录、无批次的旧 API。当前完整系统应继续使用 `scripts/run_local.py`（或 `backend.batch_node` 加 `backend.app`），不能直接用旧启动器替代。`db.serve` 保持团队原文，未在组员代码中增加兼容改动。`db.verify` 不带 `--api-url` 时仍可离线验证新版 `db.load` 生成的两个节点和报告。完整系统 API 验证使用 `tests/test_full_system.py`；原测试通过 `scripts/test_database_compat.py` 在独立测试进程注入旧 API 适配器，团队测试文件保持原文，正式服务不暴露旧未鉴权入口。

验证：11 项数据库回归与 16 项完整系统测试通过；通过本机 8002 服务对 10,000 行真实原始数据试导入，经最新版清洗得到 9,992 行 CSV，新入库器实际入库 9,992 行，节点分别为 5,861 / 4,131，导入报告和计数对账通过，原活动批次保持不变。明细在已忽略的 `data/database-upgrade-verification.json`。

复跑：

```powershell
.\.venv\Scripts\python.exe scripts/test_database_compat.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

`db/VALIDATION.md` 是数据库组提交的历史全量验收记录，不表示本轮重新执行了全量重建。所有改动仅在本地，未推送。

