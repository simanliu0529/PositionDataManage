# 社交媒体位置签到数据管理服务系统

当前为 `feature/checkin-query` 的本地完整系统：原始文件导入 → 自动清洗 → 用户分片与批次存储 → 时空查询 → 城市地图 → CSV / GeoJSON 服务。

2026-11-24 交接：接手开发先读 [系统开发交接入口](docs/handover.md)、[课程要求与完成情况](docs/course-compliance.md)、[复现测试步骤](docs/testing-handover.md)；报告素材见 [写作指南](docs/report-guide.md)，正式汇报使用 [五分钟脚本](docs/defense-five-minutes.md)。当前完整系统的 GitHub 分支为 `feature/checkin-query`，不要将 `main` 起步版误认为这份完整交接版；自制交接包通过文件清单逐项核对源码。

## 启动

需要 Python 3.11+。首次运行，在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend.manage init
.\.venv\Scripts\python.exe scripts/run_local.py --port 8002 --node-port 8200
```

环境已经安装时，只需最后一条命令。打开 http://127.0.0.1:8002/，保持终端运行；Ctrl+C 停止整套服务。该命令启动网页、独立工作进程和两个只读数据库节点，无需再开三个终端。已有服务占用端口时先在原终端停止，或为整套服务选择其他端口。

**首次配置与日常启动使用同一套代码。** 新电脑先创建虚拟环境、安装依赖、初始化本机账号，以上前三条只需首次执行；以后与发送者一样，只执行最后一条 `run_local.py` 即可。新电脑默认没有真实数据，从网页导入样例或老师原文件后，数据持久保存，不用每天重新导入。文档中的校验、doctor 和测试脚本是可选检查，不是每次启动的前置步骤。

启动器会先检查依赖、账号、端口和已发布快照，再确认网页与节点就绪，最后启动工作进程。服务运行后可执行以下只读诊断；未启动服务时加 `--offline` 仅检查本地文件。自定义端口时同时传入 `--base http://127.0.0.1:端口 --node-port 节点起始端口`。

```powershell
.\.venv\Scripts\python.exe scripts/doctor.py
```

初始化生成 `admin` 和 `viewer`，随机密码保存在本机 `data/local-accounts.json`；重复初始化不改变密码。没有开放注册。账号新增、停用和重置见 [完整手册](docs/full-system.md)。不要将账号文件、原始真实数据和生成的数据库提交到仓库。

## 使用

1. 管理员进入“数据导入”，选择 UTF-8 文件。TXT/TSV 无表头，固定五列；CSV 需要表头；JSON 为平面对象数组或单个对象；JSONL 为每行对象。CSV/JSON 可预览并映射五个字段。
2. 先选“试导入”查看真实清洗报告，再选“正式替换”。正式替换是整个数据集的新版本，核验成功后发布；没有增量追加。
3. 在工作台查看用户、地点、月份与清洗留存；在分类目录钻取，或在地图按用户、地点、UTC 时间、矩形和半径组合查询。
4. 地图与表格来自同一页；聚合也是当前页。“完整导出 CSV / GeoJSON”使用最近成功的条件及固定批次，后台覆盖全部匹配记录，不受 10000 条限制。页面显示已扫描和写出数量，完成后点击下载；可以取消，同一账号在本标签页刷新可恢复最近任务。GeoJSON 保留原始 ID；CSV 对可能被电子表格执行的 ID 加单引号保护。
5. 管理员可查看批次、报告、审计并回退。普通用户可查询、浏览目录及导出，不能上传或切换版本。

地图中橙色虚线表示尚未提交的选区，实线表示最近成功查询的条件。首次有结果时自动定位；后续查询、翻页和加载连线保留视野，可点“定位结果”查看当前页。视野外有结果时会给出提示。

“分享查询”复制最近成功查询的固定批次与条件，接收者仍需登录，并且所在服务须保留该批次；链接不包含账号凭据。失败时可“复制诊断”提供请求编号和条件，剪贴板不可用时弹窗手动复制。分享链接不是公网部署。

选定单个用户及开始、结束时间并查询后，可加载最多 10000 条匹配记录的签到连线；按 UTC 同一天、相邻时间不超过 6 小时分段，不跨日期或日期变更线连接。这只是时间顺序的签到连线，不能推断实际移动路线。管理员在批次页可比较最近 200 个批次中的两个已发布版本的摘要、规则与质量计数；对比不切换当前版本，也不是逐行数据差异。

导入进度按实际阶段显示：有明确总量时显示数值，否则显示处理中；核验与发布不会继续沿用入库阶段的百分比。

完整导出每次从节点扫描最多 20000 条，逐块写入 `data/exports/`，全部成功后才提供下载；失败或取消不提供半成品。文件按节点、记录 ID 升序排列，与地图按时间倒序的列表不同。全系统同时运行一个完整导出，完成文件超过 24 小时会在创建下一任务时清理；服务重启中断的任务需重新创建，不会伪装成成功。导出需要足够磁盘空间，下载仍验证任务所属账号。原同步 `/api/v2/checkins/export` 保留 10000 条限制，供兼容及有限签到连线使用。

原始五字段顺序为 `user_id, checkin_time, latitude, longitude, location_id`。输入和输出为 WGS 84 经纬度；时间统一到整秒 UTC。地图显示使用 Web Mercator 与真实 OSM 城市瓦片，不进行 GCJ-02 偏移。底图需要网络，失败会明确提示并可重试；合法授权的本地 XYZ 配置见 `.env.example`。

## 样例与验证

仓库 `sample/review-*` 全部为构造数据，不包含真实个人记录。`review-anomalies.tsv` 有 19 个物理行：剔除 7、重复 2，最终入库 10，节点 6 / 4；其他三格式有 18 条数据记录（不含表头或额外空行），剔除 6，其余相同。清洗规则和统计原样使用组员模块；其中坐标众数与校正计数问题已登记给清洗负责人，不能将模块报告数当作已证实的真实变更数。当前接入预期见 [样例 oracle](sample/review-oracle.json)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe scripts/test_database_compat.py
.\.venv\Scripts\python.exe scripts/check_team_boundary.py
node --check frontend/app.js
node --check frontend/system.js
node --check frontend/dashboard.js
node --check frontend/workspace.js
```

测试使用临时数据库。浏览器测试会正式发布构造样例，必须使用独立环境；详细步骤见 [本轮验收](docs/review-validation.md)。当前数据位于 `data/`，已发布的历史批次不会因代码升级自动重建；修复后的清洗规则需要重新试导入核对后再发布。

## 文档与实现边界

- [系统审查与 P0/P1/P2 清单](docs/system-review.md)
- [本轮验证与已知限制](docs/review-validation.md)
- [课程五分钟汇报](docs/defense-five-minutes.md)、[九分钟内部验收操作](docs/defense-demo.md)
- [完整架构、接口与配置](docs/full-system.md)
- [清洗模块](cleaning/README.md)、[数据库模块](db/README.md)

系统是单数据集、多批次、应用层双 SQLite 分片，适合本机课程开发与演示；不是已部署的跨主机高可用集群。每页最多 200 条、offset 最多 10000；没有全批热力、网页账号管理、手工编辑签到或多租户。

协作边界：`cleaning/`、`db/` 以及数据库组的 `compose.yaml` 保持已同步团队基线原文。前后端接入、格式转换、任务编排、只读 HTTP 适配均放在 `backend/` 等全栈目录；不改清洗算法、入库器、业务表结构或索引。全栈容器配置独立为 `compose.fullstack.yaml`（`docker compose -f compose.fullstack.yaml up --build`），未做容器运行验收。详情及待负责同学处理的问题见 [模块边界](docs/module-boundaries.md)。

此前按要求同步的 [云端起步版 README 原文](docs/upstream-starter-readme.md) 已保留。它的三终端及旧 API 启动流程适用于起步版，当前完整系统请使用本页启动命令。[云端同步记录](docs/upstream-sync.md) 是历史记录。完整交接版使用 `feature/checkin-query` 功能分支，不直接改写 `main`，也不包含本地真实数据或数据 LFS 历史；是否已推送，以实际 GitHub 分支及交接包清单核验为准。
