# 云端文件同步记录

本页记录 2026-11-20 的同步结果。2026-11-22 审查优化随后修改了 README、清洗众数逻辑与批次节点等文件；原云端 README 保存在 [起步版原文](upstream-starter-readme.md)。当前运行以 [项目 README](../README.md) 为准，不能把下述历史一致性结论当作本轮工作区状态。

后续边界调整：根据用户要求，审查新增的清洗算法修改已撤回，`cleaning/`、`db/` 和 `compose.yaml` 已恢复本页所述团队基线原文。自有节点、测试兼容及容器配置移出数据库组文件，分别为 `backend/batch_node.py`、`tests/legacy_api_adapter.py` / `scripts/test_database_compat.py`、`compose.fullstack.yaml`。当前约定见 [模块边界](module-boundaries.md)。

同步来源：`origin/main`，提交 `753ec0f06be8ba083db0f219d2838a3cfc4c9531`。

本次已把根目录 README、清洗模块 README、数据库模块 README 和 `.gitignore` 恢复为云端原文。对云端已有的其余文件逐项核对，除下述本地功能改动外，内容均与云端逐字节一致，包含数据库验收说明、清洗代码、入库器、表结构、离线校验工具和构造样例。

本地尚未推送的完整系统功能继续保留，因此这些已有文件仍有功能分支差异：

- `backend/app.py`
- `frontend/app.js`、`frontend/index.html`、`frontend/style.css`
- `compose.yaml`
- `db/serve.py`、`db/test_database.py`（当前登录/批次服务兼容处理）

新增加的完整系统模块、测试和文档继续保留。没有推送、创建 PR、删除数据库或替换活动批次。本次是工作目录文件同步，未将整个工作目录重置成 main，也未改写本地提交历史。

云端 README 目前描述起步版服务。当前完整功能的本地启动说明另存为 [本地开发说明](local-development.md)，具体使用方法见 [完整系统手册](full-system.md)。不要把起步版节点启动方法与完整批次服务混用。

被替换文档的备份和机器可读核对结果在已忽略的 `data/sync-backups/` 与 `data/upstream-sync-result.json`。
