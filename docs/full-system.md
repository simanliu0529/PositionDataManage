# 完整系统开发与验收手册

2026-11-22 本轮新增多格式映射导入、真实质量样本、数据概览、城市底图聚合和 CSV / GeoJSON 导出。清洗众数修改已按分工边界撤回，问题交清洗负责人。当前启动以 [README](../README.md) 为准，新增契约与实际验证见 [本轮验收](review-validation.md)；本文后续早期描述如与该记录冲突，以本轮验收为准。

协作边界已收紧：`cleaning/`、`db/` 及 `compose.yaml` 保持已同步团队基线原文；此前的众数代码修复已撤回，问题交由清洗负责人处理。全栈适配均位于 `backend/`，不更改团队的算法、入库器、业务表结构和索引。当前约定见 [模块边界](module-boundaries.md)。

2026-11-23 后续增强：增加待提交选区与已应用条件区分、查询保留视野、固定批次分享、诊断复制、启动自检、分阶段进度、只读批次摘要对比和有限单用户签到连线。使用与限制见 [README](../README.md)，实际验收见 [本轮验收文末](review-validation.md)。网页和节点的 `GET /healthz` 仅公开最小服务就绪信息，不提供账号或数据详情；业务 API 仍要求登录。新前端逻辑为 `frontend/workspace.js`，没有新增第三方前端依赖。

同日完整导出升级：地图导出按钮使用 `POST /api/v2/exports` 创建后台任务（JSON：`query` 为查询字符串，`format` 为 csv / geojson），固定批次并完整扫描，不受旧同步接口 10000 条限制。`GET /api/v2/exports/{id}` 查看状态、`GET /api/v2/exports/{id}/download` 下载成功文件、`POST /api/v2/exports/{id}/cancel` 请求取消。状态和下载仅任务所属账号可访问，POST 沿用 CSRF 校验；节点内部 `/export-chunk` 使用只读主键窗口，不改数据库模块或索引。元数据、暂存和结果位于 `data/exports/`，全局文件锁限制同时运行一个任务；刷新本标签页恢复最近任务，服务重启后未完成任务明确失败，需重新创建。完整文件按节点与 ID 排序。旧同步导出接口及签到连线数量边界保持兼容。

本文对应《全栈开发设计与 API 接入手册》v3.1 的已实现范围。第一轮、第二轮文档仅保留历史，不再作为当前启动依据。

## 页面与权限

| 页面 | 管理员 | 普通用户 | 数据来源 |
| --- | --- | --- | --- |
| 登录 | 允许 | 允许 | 本地控制库账号，Argon2id 密码哈希 |
| 工作台 | 四个入口及活动批次 | 地图、分类和活动批次概览 | 活动指针和两个节点实时计数 |
| 数据导入 | 上传、模式确认、任务进度、报告、失败重试 | 导航隐藏，接口返回 403 | 持久化任务和报告 |
| 签到地图 | 允许 | 允许 | 指定批次的双节点过滤和分页 |
| 分类浏览 | 允许 | 允许 | 导入时预计算的整批目录 |
| 批次管理 | 报告、激活、回退、审计 | 导航隐藏，接口返回 403 | 控制库批次、清单和审计记录 |

新增账号使用 `python -m backend.manage add-user 用户名 --role viewer`，交互输入至少 12 位密码。`disable-user`、`enable-user`、`reset-password` 提供受控本机管理，停用和重置会撤销该用户全部会话。首版不提供开放注册、地点名称搜索和单条记录手工编辑，这些属于设计书明确延后项。

登录返回 HttpOnly、SameSite=Strict 会话 Cookie，会话 8 小时过期，数据库仅保存会话 token 的 SHA-256。`GET /api/v2/auth/me` 返回账号、角色和 CSRF token。退出立即撤销服务端会话，旧、新查询接口均要求重新登录。反复失败的账号或来源在 5 分钟内达到 8 次会返回 429；成功登录不占用失败额度。启用 HTTPS 时设置 `CHECKIN_SECURE_COOKIE=1`。

## 启动和数据位置

依赖、账号初始化和启动命令见根目录 README。默认数据根目录为 `data/`，可通过 `CHECKIN_DATA` 更改。

| 位置 | 内容 |
| --- | --- |
| `data/control.sqlite` | 账号、会话、限速、导入任务、报告、目录、活动指针和审计 |
| `data/uploads/<随机名>.tsv` | 完整接收的原始文件，保留供失败重试 |
| `data/work/<job_id>/` | 每次尝试独立的暂存目录，任务结束后清理 |
| `data/shards/node-0/<batch_id>.sqlite` | 节点 0 的不可变快照 |
| `data/shards/node-1/<batch_id>.sqlite` | 节点 1 的不可变快照 |
| `data/local-accounts.json` | 首次初始化时生成的本机账号密码，禁止提交 |
| `data/full-verification/` | 浏览器截图、基准测试和验收数据 |

完整服务不会查询旧 `data/node-*.sqlite`；必须经过网页或上传 API 建立和发布新批次。原有 `cleaning/clean.py`、`db/load.py`、`db/node.py` 均保留，工作进程复用 `normalize` 和 `load`，新增编排位于 `backend/worker.py`，新增只读批次节点位于 `backend/batch_node.py`。

默认网页自动启动一个独立工作进程；也可使用 `python -m backend.app --no-worker` 加 `python -m backend.worker` 分开启动。OS 文件锁确保同一控制库只有一个工作进程领取任务，进程异常退出会释放锁。重启后未完成的任务标记失败并可从原文件重试；已完成活动指针切换的任务恢复为成功，避免把已发布数据当作未发布暂存删除。

独立主机部署需要把对应快照分发至节点，再核对哈希和远程探针。本轮实现的是设计书的本机/Compose 双节点部署，没有自动跨主机文件分发服务。

## 上传与发布

上传使用 `multipart/form-data`，字段 `file`、`mode`；模式为 `dry_run` 或 `replace`，缺省试导入。文件仅支持 `.txt`、`.tsv`，UTF-8，无表头、五列 Tab 分隔。

- 文件大小缺省上限 1 GiB，可配置 `CHECKIN_MAX_UPLOAD_BYTES`；同时最多 2 个上传，可配置 `CHECKIN_UPLOAD_SLOTS`；单行缺省最多 64 KiB，可配置 `CHECKIN_MAX_LINE_BYTES`。
- 框架按流解析和暂存 multipart 文件，应用按 1 MiB 分块写入非静态目录并计算 SHA-256。原始文件和数据库不会通过静态路由暴露。中断或拒绝的上传会清理临时目标文件。
- 入队前及处理前检查磁盘空间，保守要求原始体积 8 倍加 16 MiB 的可用空间，覆盖原始、中间文件、索引、暂存与最终快照共存。
- 相同源文件哈希、模式和规则版本的排队、运行、成功任务复用原任务；失败任务可重试，创建新的尝试与独立目录。试导入成功后仍可对同文件执行正式替换。
- 阶段为 `queued → validating → cleaning → loading → verifying → publishing → succeeded`；试导入跳过 publishing。真实行数和阶段持续写入数据库，前端约每 2 秒轮询，终态停止。刷新根据任务 ID 恢复，不依赖网页保持打开。
- 清洗流式复用原规范化规则，报告包括扫描数、有效数、剔除数、去重数、两个节点计数、规则版本、源文件哈希、阶段耗时、拒绝原因、占比和最多 5 个原始行号。
- 对账满足 `原始 = 剔除 + 有效`，`有效 = 去重 + 节点0 + 节点1`。整批无有效记录、无法解码、单行超限、空间不足、节点缺失或清单不符均失败，保留原活动批次。
- 每个节点快照完成索引和事务后关闭文件，在对应节点目录内将 `.tmp` 原子改名为最终快照。检查 SQLite 完整性、用户分片归属，并逐个通过 HTTP 探测目标批次和计数。
- 试导入完成后保存报告，删除暂存快照，不创建可激活批次。正式替换先建立目录并核验，再在控制库事务内切换状态和活动指针。切换时比较任务开始时的活动批次，防止并发任务或回退覆盖更新的发布。
- 历史快照保留，查询固定 `batch_id`；回退前再次探测两个节点，条件更新活动指针，并记录操作人、原批次、目标批次和时间。不提供增量追加或自动删除历史批次。

## 查询、目录和地图

查询条件可组合用户 ID、地点 ID、带时区的整秒时间和一种空间方式。矩形要求西、南、东、北四界齐全，西小于东、南小于北；范围包含边界，不跨 180° 经线。时间为 `[start,end)`，结束不包含。

半径要求中心经纬度和 `radius_m` 齐全，1–50000 米，和矩形互斥。节点先用保守纬度、经度范围预筛，再用 Haversine 精筛，最后排序和限制候选数。靠近极点或跨日期变更线时扩展经度预筛到全部，避免漏点；接口继续使用 WGS 84，不用底图投影坐标筛选。

指定用户仅访问其哈希分片，其他条件访问两个节点。节点返回本分片前 `offset+limit+1` 条过滤后候选；应用按时间、节点内 ID、节点号降序合并后切页。默认 50 条，最大 200 条，offset 上限 10000。结果以 `(node,id)` 标识，点位和列表始终来自同一页。

首次查询不传批次，使用活动批次；响应含 `batch_id`，后续翻页固定它。历史批次不可访问返回 409，前端提示并可点击“按最新批次查询”。无活动批次返回 `409 NO_ACTIVE_BATCH`。单个必需节点不可用返回 503，不返回部分结果。

目录按用户、UTC 月份、地点 ID 聚合，数据来自整个批次，不再使用模拟数据。用户和地点按 count 降序、key 升序，月份按 key 降序。前缀 `q` 仅用于用户和地点，按字面匹配，百分号等字符不作为通配符。点击目录项保留批次并将查询分页归零，月份生成 UTC 月初至下月月初条件。

地图支持点表双向定位、拖动、缩放、框选、地图点选半径中心、半径预览、按当前视野查询。视野操作不自动查询；当前视野查询替换空间方式，保留表单中的用户、地点和时间。刷新保留当前标签页最近一次成功的筛选、每页条数、偏移和批次，结果重新从服务器取得。

底图默认按需加载 OpenStreetMap，保留归属，遵循浏览器缓存，不批量下载。读取 `/api/v2/config` 可获取管理员配置的底图模板和归属；环境变量见 `.env.example`。有合法离线 XYZ 数据包时，可设置 `CHECKIN_TILE_URL=/tiles/{z}/{x}/{y}.png` 与 `CHECKIN_TILE_ROOT`，离线瓦片路由同样鉴权。底图失败或超时回到离线坐标视图，查询仍可继续。Web Mercator 不支持的极地记录使用离线视图。

## HTTP 接口补充

写请求除登录外都需 Cookie 和 `X-CSRF-Token`，并检查跨站 Origin。错误统一为 `code`、`message`、`request_id`。前端兼容网络故障、12 秒查询超时、非 JSON 响应和错误编号。

```text
POST /api/v2/auth/login                   JSON username,password
GET  /api/v2/auth/me
POST /api/v2/auth/logout
POST /api/v2/imports                      multipart file,mode
GET  /api/v2/imports?limit=20&offset=0     最近任务
GET  /api/v2/imports/{job_id}
POST /api/v2/imports/{job_id}/retry
GET  /api/v2/batches?limit=10&offset=0
POST /api/v2/batches/{batch_id}/activate  JSON expected_batch_id
GET  /api/v2/dataset/active
GET  /api/v2/checkins
GET  /api/v2/catalog?group_by=user&q=0&limit=20&offset=0
GET  /api/v2/config
```

激活接口的 `expected_batch_id` 是确认时的当前批次，首次无批次时为 null；状态已变化返回 409。任务和批次列表返回 `items`、`has_more`，批次列表额外返回 `active_batch_id` 和最近审计记录。查询额外返回 `node_metrics`，含每节点耗时和候选数，供性能测量。

## 验收与复跑

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
node --check frontend/app.js
node --check frontend/system.js
```

后端测试在独立临时数据库、临时端口运行，不改正在运行的数据。覆盖登录、会话撤销、限速、角色、CSRF、试导入、重复提交、发布、故障重试、崩溃恢复、报告、目录、时间和空间边界、日期变更线、极点、批次固定、条件回退、未发布快照不可访问及大文件解析限制。

浏览器端到端检查默认使用本机 `8002`，节点 `8200/8201`，读取本机 `data/local-accounts.json`。会正式发布和回退构造样例，因此先在专用本地测试环境运行，测试完成后可回退到所需真实批次。

```powershell
$browserProfile = Join-Path (Get-Location).Path 'data/browser-profile'
Start-Process -FilePath 'C:/Program Files/Google/Chrome/Application/chrome.exe' -WindowStyle Hidden -ArgumentList @('--headless=new','--disable-gpu','--no-first-run','--remote-debugging-port=9224',('--user-data-dir="' + $browserProfile + '"'),'about:blank')
node tests/browser-full-system.mjs
```

Node.js 22+，无 npm 包依赖。截图和检查结果位于 `data/full-verification/`。早期未鉴权 API 和模拟目录的旧验证脚本已由完整流程验证替代。

真实数据性能验收命令：

```powershell
.\.venv\Scripts\python.exe scripts/verify_full_data.py "E:\研一上\空间数据管理与质量控制（测绘工程案例）\Gowalla_totalCheckins.txt"
```

脚本取前 1 万、前 100 万和全量，通过真实 HTTP 上传并发布。逐批核对计数、单节点用户路由和 SQLite 查询对照，对单用户、城市矩形、1 公里半径、宽范围浅页和深页分别预热后重复 30 次，记录中位数、P95、各节点耗时、返回候选数、索引和机器环境。半径 oracle 使用独立的全扫描球面距离计算，不使用生产预筛。报告 `data/full-verification/benchmark.json` 为本机实测，不能替代其他组员机器上的复测或预先冻结性能目标。

Docker 配置已按四服务、双只读节点和独立数据卷组织。无 Docker 运行环境时只验证配置结构，不将配置检查写成容器运行成功。正式对外部署仍应使用 HTTPS 和生产 WSGI 服务，本任务运行范围为本机开发与课程演示。

