> 历史阶段记录。当前启动、接口和验证以 [完整开发与验收手册](full-system.md) 为准；旧模拟目录及未鉴权测试已被完整系统替代。
# 查询功能第一轮本地验证说明

本轮依据《全栈开发设计与 API 接入手册》v3.1 第 3、5、8、10 节及图 2、4、5，完成工作台、签到地图和分类浏览。沿用 `frontend/`、`backend/` 结构，直接复用现有 `/api/checkins`、`/api/nodes`；未改动清洗、导入、数据库节点、批次发布实现。

## 启动

Python 3.11 及以上版本，无新增运行依赖。以下命令均在仓库根目录执行。

首次运行先生成构造样例数据库；若已有服务使用这些数据库，先停止服务再重建，勿覆盖组员正在使用的数据文件。

```powershell
python -m cleaning.clean sample/checkins.tsv data/clean.csv
python -m db.load data/clean.csv data
```

清洗预期为输入 6 行、有效 5 行、无效坐标 1 行；入库去重 1 行，两节点各 2 条。随后分别在三个终端启动：

```powershell
python -m db.node data/node-0.sqlite 8100
python -m db.node data/node-1.sqlite 8101
python -m backend.app
```

访问 http://127.0.0.1:8000/ 。工作台、地图、分类浏览分别使用 `#home`、`#map`、`#catalog`，可直接访问或浏览器前进、后退。已启动原版服务时，刷新网页即可加载新版静态文件。

## 已实现行为

- 工作台显示实时节点计数、功能入口和待集成入口，不伪造发布批次。
- 查询支持用户 ID、精确到秒的 UTC 时间及矩形范围，条件取交集。开始时间包含、结束时间排除、矩形边界包含。
- 地图与列表使用接口同一页结果；以 `node:id` 标识记录，点击点位或表格行可双向高亮并显示详情。
- 地图为无需网络的经纬度坐标视图，页面明确标注“非城市底图”；坐标来自接口，不是模拟签到。支持拖动、缩放、定位结果和拉框填入四界。
- 拖动和缩放只改变视野。框选填入表单后须点查询；“按当前视野查询”替换空间条件，并保留表单中的用户和时间条件。
- 分页使用服务端 `has_more`，新条件从第一页开始。默认每页 50 条，可选 2 条测试分页。请求取消避免旧响应覆盖新结果，失败清空过时数据并允许重试。
- 分类页明确标注模拟目录，固定展示构造样例的用户、UTC 月份和地点。支持前缀检索、目录分页，用户和月份可带条件跳转地图。计数仅表示样例数据，不冒充当前批次总数。
- 半径查询与地点筛选不向旧接口发送伪请求，界面明确提示待联调。

## 联调依赖

| 接口或能力 | 等待对方提供的约定 | 接入后的行为 |
| --- | --- | --- |
| `GET /api/v2/checkins` | `batch_id`、`items`、`queried_nodes`、`has_more`；支持 `location_id` | 切换查询适配入口，地点目录可用，翻页固定批次 |
| 半径查询 | 同一查询接口支持 `center_lon`、`center_lat`、`radius_m`；1–50000 米，先精确筛选再分页，圆边界包含 | 加入地图点选中心、半径输入，不能用前端过滤替代后端结果 |
| `GET /api/v2/catalog` | `group_by=user/month/location`、前缀 `q`、分页和 `batch_id`；返回 `items[{key,count}]` 和 `has_more` | 移除固定演示目录，显示整批聚合数；月份倒序，用户/地点按 count 降序、key 升序 |
| `GET /api/v2/dataset/active` | `batch_id`、`published_at`、`total_count`、`nodes`；无批次时 `409 NO_ACTIVE_BATCH` | 工作台展示真实已发布批次；无数据和版本失效分别提示 |
| 批次失效和统一错误 | 409 版本冲突及 `code/message/request_id` 错误结构 | 提示刷新后从最新批次第一页查询，禁止悄悄混用批次 |
| 登录和权限 | `/api/v2/auth/login`、`me`、`logout` 及旧查询路径同等鉴权 | 接入角色导航和 401/403 状态；本轮显式为未接入登录的本地开发页 |
| 城市底图 | 选定地图提供方、坐标基准与授权；地图 SDK/资源可达 | 替换当前离线坐标视图，保留失败回退；数据与筛选仍使用同一经纬度口径 |

上传、入库、批次发布及管理页面属于其他模块。本轮仅保留导航位置，不实现或模拟写操作。

## 自动验证

API 回归使用临时样例数据库和临时端口，不覆盖运行数据：

```powershell
python -m unittest discover -s tests -p 'test_*.py' -v
node --check frontend/app.js
```

10 项 API 测试覆盖双节点计数、全局排序、用户路由、UTC 边界、矩形边界与组合筛选、分页、空结果、非法参数、必需节点不可用返回 503 及静态资源。也验证旧接口不支持半径参数、v2 目录尚未实现。

浏览器回归无需 npm 包，使用 Node.js 22+ 内置 WebSocket 和 Chrome DevTools Protocol。先按上文启动默认样例服务，再启动测试专用的无界面 Chrome：

```powershell
$browserProfile = Join-Path (Get-Location).Path 'data/browser-profile'
Start-Process -FilePath 'C:/Program Files/Google/Chrome/Application/chrome.exe' -WindowStyle Hidden -ArgumentList @('--headless=new','--disable-gpu','--no-first-run','--remote-debugging-port=9224',('--user-data-dir="' + $browserProfile + '"'),'about:blank')
node tests/browser-smoke.mjs
```

22 项浏览器检查覆盖工作台计数、筛选、点表联动、无重复分页、非法范围、半径禁用、缩放不查询、当前视野查询、拉框、模拟目录跳转和月初边界、断网清空结果与重试、390px 无整页横向溢出和无未捕获异常。

截图与结果保存在 `data/ui-verification/`（已忽略）：`home.png`、`map.png`、`catalog.png`、`map-mobile.png`、`result.json`。截图用于本地核验，不上传数据。

## 手工复查

1. 打开工作台，确认节点各 2 条，总计 4 条。
2. 地图页用户 ID 输入 `1`，查询应返回 1 条，仅访问节点 1。点击表格定位或点位检查联动。
3. 重置后时间输入 `2010-08-02 09:15:00` 至 `2010-08-03 11:30:00`（UTC），仅返回用户 `2`。
4. 重置后选择矩形：西 `114.36`、东 `115`、南 `30.54`、北 `31`，仅返回用户 `1`，包含边界。
5. 重置后每页选 2 条，确认两页合计 4 条；输入不存在的用户验证空状态。
6. 分类页选择用户或月份，点击查看签到；地点项、半径查询应明确提示未就绪。

本轮仅本地实现和验证，不提交 PR、不推送。当前数据库仍为构造样例；之前的完整原始数据 LFS 提交属于本地历史，本轮不上传该文件。

