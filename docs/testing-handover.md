# 接手开发与测试步骤

这是一份新环境复现步骤。已有本机历史测试不能自动证明接收者电脑正常；请记录自己的 Python/浏览器版本、系统、时间、源快照清单、规则、样例及批次。

## 1. 无数据启动与隔离

按 [交接入口](handover.md) 创建环境和账号。开发和试验建议使用专用目录，不复用真实全量环境。PowerShell 在项目根目录执行：

```powershell
$env:CHECKIN_DATA = Join-Path (Get-Location).Path 'data/handover-demo'
.\.venv\Scripts\python.exe -m backend.manage init
.\.venv\Scripts\python.exe scripts/run_local.py --port 8032 --node-port 8230
```

另一个终端须设置相同 `CHECKIN_DATA`，再执行：

```powershell
$env:CHECKIN_DATA = Join-Path (Get-Location).Path 'data/handover-demo'
.\.venv\Scripts\python.exe scripts/doctor.py --base http://127.0.0.1:8032 --node-port 8230
```

访问 http://127.0.0.1:8032/；账号文件在 `data/handover-demo/local-accounts.json`。无活动数据时出现导入引导是正常状态，不应误报节点不可用。试验结束先在启动终端 Ctrl+C；如需回默认环境，新终端不设置该变量，或执行 `Remove-Item Env:CHECKIN_DATA`。不要在有运行/排队导入或运行导出时重启。默认服务不会自动加载 `.env`。

## 2. 构造样例闭环验收

| 操作 | 可核对的预期 |
| --- | --- |
| 管理员上传 `sample/review-anomalies.tsv`，先试导入 | 扫描 19、剔除 7、有效候选 12、重复 2、预计输出 10；不切换活动版本 |
| 同样文件正式替换 | 成功后活动批次 10 条，节点 6/4；有清洗与数据库报告、规则和源文件哈希 |
| CSV/JSON/JSONL 格式回归 | `review-checkins.*` 扫描 18、剔除 6、重复 2、输出 10、节点 6/4；与 TSV 多一个物理空行的口径区别见 oracle |
| 单用户与时间筛选 | 用户 `synthetic-u2`；区间 `[2010-10-19T02:00:00Z,2010-10-19T03:00:00Z)` 应为 1 条 |
| 矩形 | 西 -97.8、东 -97.7、南 30.2、北 30.3，原样团队规则应为 5 条 |
| 半径 | 中心经度 -97.75、纬度 30.27、半径 1500 米，应为 u2 共 1 条 |
| 地图交互 | 实际城市底图可见；点击表格定位点；矩形虚线待提交、实线已应用；连续缩放/拖动不漂移，翻页不自动跳视野 |
| 导出 | 完整导出同一固定批次，成功后能打开 CSV/GeoJSON；字段、行数与条件一致；失败不出现半成品下载 |
| 角色 | viewer 可查询与导出；导入、批次切换 API 为 403，不能仅以按钮隐藏作为权限验收 |
| 退出 | 数据与下载链接清理，原下载/API 再访问为 401 |
| 版本回退 | 在专用环境发布第二个样例，记录两批次后激活历史版；数量与固定版本查询均可核对 |

以上空间预期反映现有上游行为，不能证明坐标清洗语义正确。C-01/C-02/C-03 的理想数学期望保存在 `tests/test_cleaner_regression.py`；由清洗同学修复后应同时审查 oracle 和这些预期失败，不能为了测试变绿删除检查。

## 3. 自动化回归

从项目根目录运行，测试使用临时目录与临时端口，不需要真实原文件：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe scripts/test_database_compat.py
.\.venv\Scripts\python.exe scripts/check_team_boundary.py
node --check frontend/app.js
node --check frontend/system.js
node --check frontend/dashboard.js
node --check frontend/workspace.js
```

Python 测试不依赖 Node；Node 只用于 JS 语法和浏览器脚本。当前测试文件在完整发现时应为 76 项，其中 3 项是清洗模块已知 `expectedFailure`；这不是 76 项全通过。若出现 `unexpectedSuccess`，说明上游行为变化，应重新评审缺陷是否已修及是否移除装饰器。外部数据库兼容脚本额外 11 项，保持组员测试源码不变。

`node --check` 不能替代页面验收。现有 `.mjs` 浏览器脚本通过 Chrome 调试协议运行，需要 Node 支持原生 fetch/WebSocket、专用 Chrome 调试端口与独立浏览器用户目录。不要连接日常浏览器，也不要无准备地运行会上传/发布的脚本。

| 脚本 | 前置条件与风险 |
| --- | --- |
| `tests/map-viewport-browser.mjs` | 默认读取主环境 8002、`data/local-accounts.json` 与专用 Chrome 9224；只读查询；可用 CHECKIN_VIEWPORT_DEBUG/OUTPUT 指定调试地址与结果目录 |
| `tests/full-export-browser.mjs` | 固定主环境 8002、Chrome 9234；依赖真实数据中用户 0 的指定三天 4 条结果；创建导出文件但不重建数据；新电脑尚未导入原数据时不能直接运行 |
| `tests/workspace-browser.mjs` | 依赖本机已忽略的隔离夹具目录、8014/9234 和已发布 4/68 条批次，不是开箱即跑脚本；需要重建同样夹具或先改为你自己的隔离环境 |
| `tests/browser-review.mjs`、`browser-full-system.mjs` | 阶段性验收脚本，有上传/正式发布操作和本机环境约定；先读源码，禁止对共享真实环境直接运行 |

交接后的通用基线为 Python 临时环境集成测试 + 本文人工浏览器清单。历史浏览器结果见证据摘要；不能把专用夹具未打包说成“交接包中所有浏览器脚本无需配置均可运行”。

## 4. 大数据与故障验证

老师原始文件经授权另行共享，不放源码包。先核对 SHA-256/字节数，再选择一个专用 CHECKIN_DATA 环境，从网页上传、试导入核对报告后正式发布。不要直接把原始 TXT 改名成清洗 CSV，不要手动覆盖活动 SQLite。时间/坐标/ID 五字段契约见 README。

完整导出测试至少检查超过 10000、20000 边界、无重复、空筛选、条件筛选、取消、节点失败、其他账号拒绝访问与刷新恢复。自动化 `test_full_exports.py` 会构造 41006 条数据验证完整 CSV，41005 条验证 GeoJSON，并模拟 Windows 文件共享冲突。当前主机已有 6430640 条真实 CSV 完整导出与 HTTP 逐行下载校验；新环境如重复测量应单独记录，不沿用旧耗时。

故障注入只在专用环境：节点停止必须失败而非返回部分记录；超时应建议缩小条件；端口冲突必须在启动前指出；取消与失败不提供 `.part` 文件。不要为了演示临时关闭共享主环境节点。

## 5. 每轮测试记录格式

```text
日期/测试人/操作系统/Python/浏览器：
源码清单或提交、团队基线：
数据文件 SHA-256、样例/真实、规则版本、批次：
操作和期望：
实际结果（通过/失败/未测），请求编号：
日志/截图/导出文件位置：
发现问题、修复归属、复验结果：
```

截图与实验表只采用同一版本实际执行结果；页面能打开不能算完整闭环通过。超过一分钟的大任务宜显示阶段或已处理计数，不用随机百分比。报告不填写没有测过的 P95、峰值内存或 Docker 运行结论。
