# PositionDataManage 位置签到数据管理服务

2026-11-22 更新：当前启动、输入格式和导出说明以 [根目录 README](../README.md) 与 [本轮验收](review-validation.md) 为准。本页保留前期开发说明。

数据库基于云端 `1e1c602`；新版入库器接入及启动兼容说明见 [数据库更新记录](database-upgrade.md)。

基于 Gowalla 五列原始签到数据，实现登录鉴权、网页上传、自动清洗去重、双节点批次快照、核验发布、回退、地图查询和真实分类目录。当前完整使用说明见 [开发与验收手册](full-system.md)。第一轮和第二轮文档保留为历史记录，启动与接口以本文件和完整手册为准。

本机真实数据、测试和性能结果见 [完整验收记录](acceptance-result.md)。

## 本地启动

需要 Python 3.11+。在项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend.manage init
.\.venv\Scripts\python.exe scripts/run_local.py
```

打开 http://127.0.0.1:8000/ 。首次初始化生成 `admin`、`viewer` 两个账号，随机密码保存在本机已忽略的 `data/local-accounts.json`，重复初始化不会改密码。请通过组内独立渠道分发账号，不提交该文件。

已有服务占用端口时先在其终端 Ctrl+C 停止，或为整套服务选择其他端口：

```powershell
.\.venv\Scripts\python.exe scripts/run_local.py --port 8002 --node-port 8200
```

脚本启动两个节点、一个持久化任务工作进程和网页服务，Ctrl+C 只停止它自己启动的子进程。也可以分别启动两个节点和自动带工作进程的网页服务：

2026-11-23 起启动器增加启动前检查与就绪等待。推荐按根目录 README 使用 8002 / 8200 端口；运行后执行 `.\.venv\Scripts\python.exe scripts/doctor.py` 只读检查账号、快照、网页和节点计数，未启动时加 `--offline`。自定义端口通过 `--base` 与 `--node-port` 指定。`/healthz` 公开的仅为最小服务就绪信息，业务数据仍受登录权限保护。以下为可选的分进程启动命令：

```powershell
.\.venv\Scripts\python.exe -m backend.batch_node data/shards/node-0 8100
.\.venv\Scripts\python.exe -m backend.batch_node data/shards/node-1 8101
.\.venv\Scripts\python.exe -m backend.app
```

首次登录后没有活动批次会显示“暂无已发布数据”。管理员在数据导入页选择 `sample/checkins.tsv`，先试导入，再选择正式替换。预期输入 6 行、有效 5 行、无效坐标 1 行、去重 1 行，两节点各 2 条。试导入不改变当前数据；正式替换核验通过后自动发布。

老师的 `Gowalla_totalCheckins.txt` 可直接从网页上传，使用完全相同的流程。不要用旧版 `db.load` 命令直接覆盖服务正在使用的文件。旧 `cleaning.clean`、`db.load` 和 `db.node` 保留为离线工具，完整服务使用新的批次节点 `backend.batch_node`。

## 团队分工与协作

| 负责人 | 任务分支 | 工作范围 |
| --- | --- | --- |
| 数据清洗 | `feature/cleaning` | `cleaning/`、规则、拒绝原因和质量报告 |
| 数据库 | `feature/database` | `db/`、双节点、分片、索引和快照核验 |
| 查询功能全栈 | `feature/checkin-query` | `frontend/`、`backend/`、地图查询和分类浏览 |
| 系统集成与验收 | `feature/integration` | 启动、导入发布、故障注入与全量复跑 |

本轮在 `feature/checkin-query` 完成本地整合；未推送或创建 PR。已有清洗与基础入库代码继续复用，新工作进程和批次节点单独放在新模块中，便于各模块对接。后续保持字段、分片和 API 契约稳定，通过 PR review 合并。

构造样例随代码共享。全量原始数据、清洗结果、SQLite、账号、运行日志和报告保留本地或课程授权共享环境，不需要上传到代码仓库。此前未推送的本地 LFS 数据提交仍属本地历史，之后发布分支前应先整理该历史，避免误上传全量数据。

## 数据与接口

原始数据无表头，以 Tab 分隔：`user_id`、`checkin_time`、`latitude`、`longitude`、`location_id`。ID 为文本，时间归一为整秒 UTC，经纬度为 WGS 84 十进制度。按用户 ID 的 SHA-256 首字节模 2 分片，网页已接入云端 238168f 的完整清洗流程，默认按用户、时刻、地点清洗去重，入库保留五字段唯一约束兜底。升级规则及报告口径见 [最新版清洗接入说明](cleaning-upgrade.md)。

- `POST /api/v2/auth/login`、`GET /api/v2/auth/me`、`POST /api/v2/auth/logout`：账号、角色和会话。
- `POST /api/v2/imports`：管理员上传，`file` 和 `mode=dry_run/replace`，默认试导入。
- `GET /api/v2/imports/{job_id}`、`POST /api/v2/imports/{job_id}/retry`：报告与失败重试。
- `GET /api/v2/batches`、`POST /api/v2/batches/{batch_id}/activate`：批次和回退。
- `GET /api/v2/dataset/active`：活动批次、节点计数与发布时间。
- `GET /api/v2/checkins`：用户、地点、UTC 时间、矩形或半径组合查询与分页。
- `GET /api/v2/catalog`：用户、月份、地点目录及前缀检索，整批聚合计数。

旧 `/api/checkins`、`/api/nodes` 与 v2 采用相同鉴权，退出后均不可查询。写请求需会话及 `X-CSRF-Token`；管理接口只允许管理员。查询翻页传响应的 `batch_id` 固定版本。

## 验证与 Docker

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
node --check frontend/app.js
node --check frontend/system.js
```

浏览器端到端验证使用 `node tests/browser-full-system.mjs`，需先按完整手册启动 Chrome 测试会话和本地 8002/8200/8201 服务。

一万行、百万行和全量导入、SQL 对照与 30 次性能测量：

```powershell
.\.venv\Scripts\python.exe scripts/verify_full_data.py "E:\研一上\空间数据管理与质量控制（测绘工程案例）\Gowalla_totalCheckins.txt"
```

这个脚本会正式导入并切换本地活动批次，明细报告保存在 `data/full-verification/benchmark.json`。

Docker 使用 `docker compose -f compose.fullstack.yaml up --build -d`。首次启动后运行 `docker compose -f compose.fullstack.yaml exec web python -m backend.manage init`，再使用 `docker compose -f compose.fullstack.yaml cp web:/app/data/local-accounts.json ./data/local-accounts.json` 取得本地账号。四个容器分别承担网页、工作进程和两个只读节点；仅网页端口映射到本机。Docker 数据卷与本地 Python 模式独立，两种模式不要共用 8000 端口。

当前采用应用层分片和独立 SQLite 节点。是否满足课程对分布式数据库技术选型的要求，仍需与老师确认；更换数据库时可保持批次和 HTTP 接口不变。

