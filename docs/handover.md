# 给系统开发同学的交接入口

交接日期：2026-11-24；完整系统功能分支：`feature/checkin-query`。可从该 GitHub 分支获取源码，也可使用发送者生成的当前工作区交接包；不能只取 `main` 起步版。自制包通过 `HANDOVER-MANIFEST.json` 与本地源码逐项核验；GitHub 版本以分支提交为准。本次不合并主分支、不创建 PR。优先阅读本页，再阅读 [课程对照](course-compliance.md)、[接手测试步骤](testing-handover.md) 和 [报告素材](report-guide.md)。另有 [干净环境复验](handover-validation.md) 及 [可直接转发的交接文字](handover-message.md)。

## 当前完成到哪里

课程核心系统已经具备真实闭环：网页导入原始文件 → 调用团队清洗器 → 用户哈希分片 → 双节点不可变快照 → 核验发布 → 用户/时间/空间查询 → 城市地图与列表 → 完整文件导出。全量真实数据已有本地导入、查询和完整导出验证，适合作为后续联调和答辩的基线。

还不能将整个课程作业标为全部完成：清洗众数及计数仍有已知上游缺陷；最终不少于 3000 字的报告、符合格式的图表、五分钟 PPT、组员信息与最终材料提交尚待小组完成。当前是本机双节点验证，不能写成跨主机高可用集群。课程允许虚拟节点，见 [原始要求](references/course-requirements.pdf) 第 1 页。

## 收到压缩包后

解压 `PositionDataManage-20261124.zip`，进入其中 `PositionDataManage` 文件夹。应看到 `README.md`、`backend/`、`frontend/`、`cleaning/`、`db/`、`sample/`、`scripts/`、`tests/`、`docs/` 和 `HANDOVER-MANIFEST.json`。压缩包不含 `.git/`、`.venv/`、`data/`、账号密码、真实数据、生成数据库或大导出文件；这是正常的。不能直接把 `.git` 历史和整个 `data` 文件夹压缩发送：本地历史中存在未推送的真实数据 LFS 提交，且运行目录包含凭据和大量数据。

本机制作包命令为 `python scripts/make_handover.py`；默认产物在 `data/handover-delivery/`，旁边有 ZIP 的 SHA-256 文件。更新代码后应重制包，不要将旧包当作当前文件。发送人通过聊天/网盘等方式自行发送，本次没有代发给其他人。

以下仅为**新电脑首次配置**，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-tested.txt
.\.venv\Scripts\python.exe -m backend.manage init
```

首次配置后，以及以后的**每次日常启动**，都只执行同一条命令：

```powershell
.\.venv\Scripts\python.exe scripts/run_local.py --port 8002 --node-port 8200
```

这与发送者本机完全相同，`run_local.py` 自动启动两个节点、网页和后台工作进程。发送者已经完成首次配置，才不需要重复建环境和账号；不是收到的包少了代码。新电脑没有发送者的持久化数据，需要第一次从网页导入样例或老师原始文件，后续启动会保留自己的数据。

以下是**可选校验/排错**，不用每次启动执行：

```powershell
# 仅适用于自制交接包；git clone 或 GitHub 自带 Download ZIP 不含此清单，跳过本项
.\.venv\Scripts\python.exe scripts/make_handover.py --verify .
.\.venv\Scripts\python.exe scripts/check_team_boundary.py
.\.venv\Scripts\python.exe scripts/doctor.py
```

从 GitHub 获取时，可执行 `git clone --branch feature/checkin-query https://github.com/simanliu0529/PositionDataManage.git`。已有克隆请先保存自己的修改，再获取并切换该功能分支；不要无条件覆盖同学的新模块。不要用 `main` 的旧启动器启动完整系统。

`requirements-tested.txt` 固定交接验证环境版本；`requirements.txt` 为常规版本范围。Python 需要 3.11+；目前交接复验以 Windows Python 3.11 为准，不把其他平台推定为已验收。网页为 http://127.0.0.1:8002/，保持终端运行；Ctrl+C 停止自己启动的整套服务。另开终端运行 `python scripts/doctor.py` 的虚拟环境版本，详细命令见 README。

首次账号为 `admin`、`viewer`，随机密码由接收者本机生成于 `data/local-accounts.json`，没有统一默认密码。不要把发送者密码或数据库复制过来。初次启动没有活动数据时，先按 [接手测试步骤](testing-handover.md) 导入公开构造样例。PowerShell 中 `.env` 文件不会自动加载，需要显式设置 `$env:CHECKIN_DATA` 等变量。

## 代码地图与接入原则

| 文件/目录 | 作用 | 接手时注意 |
| --- | --- | --- |
| `frontend/index.html`、`style.css` | 页面与视觉布局 | 原生 HTML/CSS，没有 npm 构建步骤 |
| `frontend/app.js` | 查询状态、地图投影/瓦片、空间选区、表格与目录 | 改地图后回归即时对齐，不重新引入延迟瓦片定位；WGS 84 输入、Web Mercator 显示 |
| `frontend/system.js` | 登录、CSRF、导入任务、批次和完整导出页面 | 表单未提交条件不能污染成功查询的导出/分享快照；退出清除私有状态 |
| `frontend/dashboard.js`、`workspace.js` | 工作台、摘要对比、连线、分享、诊断 | 当前页聚合不代表全批热力；签到连线不代表真实路线 |
| `backend/server.py`、`security.py` | Flask 路由、账号、权限、会话 | 普通用户可查询/导出，不能导入/切换批次；写请求校验 CSRF |
| `backend/ingestion.py`、`cleaning_adapter.py` | 格式映射、输入适配、有限质量证据 | 不在适配层偷偷替代清洗算法 |
| `backend/worker.py`、`control.py` | 持久化导入编排、控制库、发布与回退 | 发布是核验后的版本切换；不要直接覆盖活动快照，不在在途导入时重启 |
| `backend/batch_node.py`、`spatial.py` | 全栈只读节点、共同空间语义 | 用户筛选路由一个节点，其余条件通常聚合两个；失败不返回部分成功 |
| `backend/data_services.py`、`export_jobs.py` | 概览、旧同步导出、后台完整导出 | 新页面用后台任务；旧同步接口和签到连线仍有 10000 条界限 |
| `cleaning/`、`db/`、`compose.yaml` | 组员原模块 | 保持已同步团队基线，不由全栈直接修改 |
| `compose.fullstack.yaml` | 独立全栈容器配置 | 只有配置，尚未实测容器启动 |

全栈服务控制库属于 `backend/` 的任务、账号与批次编排，不等于改动数据库同学的 `db/` 实现。后续改清洗、索引、业务表和分片时，应由对应负责人提供版本，再做集成。团队基线为已获取的 `753ec0f06be8ba083db0f219d2838a3cfc4c9531`，不是保证云端永远最新。

带 Git 的原仓库边界校验直接比较该提交；无 Git 的压缩包使用 `docs/team-boundary-manifest.json` 的 12 文件 SHA-256。快照哈希用于检查交接后变更，不能代替对更新云端基线的评审。若同学有新的模块版本，记录提交、重新验收并更新基线，不能为让检查变绿而直接重写哈希。

## API 接手速查

浏览器必须请求网页服务 8002 的 `/api/v2/`，不能绕到 8200/8201 的内部节点。节点默认只监听本机，不是公开数据接口。`/healthz` 仅返回最小就绪信息。

| 场景 | 接口 | 约束 |
| --- | --- | --- |
| 登录/会话/退出 | `POST /auth/login`、`GET /auth/me`、`POST /auth/logout` | 此表路径统一省略 `/api/v2`；登录字段 username/password，返回 csrf_token，会话 Cookie 自动携带 |
| 概览/当前版本 | `GET /overview`、`GET /dataset/active` | 普通用户也可访问；无数据与节点异常不是同一种状态 |
| 签到查询 | `GET /checkins` | user_id、location_id、start/end、矩形或半径；limit 1–200，offset 0–10000；传 batch_id 固定版本 |
| 分类目录 | `GET /catalog` | group_by 为 user/month/location，整批计数；具体筛选参数参考服务端契约 |
| 导入/预览/任务 | `POST /imports`、`POST /imports/preview`、`GET /imports/{id}` | 管理员；multipart 文件及 mode=dry_run/replace；预览不是入库 |
| 批次管理 | `GET /batches`、`POST /batches/{id}/activate` | 管理员；激活需 expected_batch_id 防止并发覆盖 |
| 完整导出 | `POST /exports` → `GET /exports/{id}` → `GET /exports/{id}/download` | JSON `{query: "过滤参数字符串", format: "csv或geojson"}`；固定最近成功批次；任务仅所属账号可访问 |
| 取消完整导出 | `POST /exports/{id}/cancel` | 同时最多一个完整导出；成功前禁止下载，取消不提供半成品 |

登录后 POST 添加 `X-CSRF-Token`；前端统一调用 `system.request()`，不要另写没有权限处理的 fetch。时间输入支持带时区值并归一 UTC，筛选 `[start,end)`；矩形四界完整且西<东、南<北；半径 1–50000 米；矩形与半径不能混用。完整参数校验以 `backend/spatial.py` 和实际响应为准，不把未知参数忽略当成功。

完整导出按节点/ID 排列，结果留在服务器 `data/exports/`；用户点击下载后另存在浏览器下载目录。导出文件过 24 小时在创建下一任务时清理。页面刷新可恢复本标签页最近任务；服务重启不支持继续上次未完成导出，需要新建。大文件清洗仍存在内存/排序成本，不应承诺任意规模。

## 接手后工作顺序

| 优先级 | 工作 | 验收标准/负责人 |
| --- | --- | --- |
| P0 | 新电脑完整复现 | 按测试文档发布样例、查询、地图、下载及权限均通过；由系统开发同学记录新环境结果 |
| P0 | 清洗 C-01/C-02/C-03 | 交清洗负责人修复并提供版本，重审 3 项预期失败与 oracle，再重新试导入；不由本轮全栈擅改 |
| P0 | 课程交付物收齐 | 完整代码、≥3000 字技术报告、≤5 分钟 PPT；组长确认原文截止时间与最新通知 |
| P1 | 统一最终截图与实验统计 | 同一批次/规则/环境，截图与表格能追到测试；不混用旧规则全量结果 |
| P1 | 故障与现场网络复验 | 新环境端口冲突、节点失败、查询超时、导出失败、OSM 网络、窄屏与权限实际操作验证 |
| P1 | 最终版本冻结和小组联调 | 清洗/数据库/全栈三方共同确认接口、模块归属和新基线，再形成最终提交 |
| P2 | 性能扩展 | 当前规则 P95/峰值内存、Docker或跨机部署、全批热力等按时间安排，不为堆功能破坏已验收闭环 |

现有文档中标明“历史”的阶段结果仅用于追溯；当前概况以本页、[课程对照](course-compliance.md) 和 [公开证据摘要](evidence/verification-baseline.json) 为入口，详细执行日志仍在发送者本机 `data/`，不包含于源码包。截图原件和全量数据如有需要，通过课程授权渠道另行共享。
