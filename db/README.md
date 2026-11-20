# 数据库构建与交接

数据库模块直接接收清洗同学提供的 `clean.csv`。不重新运行轨迹清洗、速度过滤或坐标修复；入库时只做字段校验、时间表示统一及五字段组合去重。后端和前端接口保持现有约定。

## 目录约定

```text
db/
  schema.sql          # 两节点共用的表结构
  load.py             # CSV 校验、分片、批量导入和索引
  node.py             # 已有的只读查询节点
  serve.py            # 启动两个节点及网页后端
  verify.py           # 数据库与 API 验收
  test_database.py    # 无需真实数据的回归测试
  run-local.cmd       # Windows 双击启动入口
  README.md           # 本文：构建和交接步骤
  VALIDATION.md       # 本机全量验收摘要
data/                 # 不提交 Git
  clean.csv           # 清洗模块的交接文件，也可使用仓库外绝对路径
  full/
    node-0.sqlite
    node-1.sqlite
    import-report.json
    verification-report.json
```

数据库实现、测试、启动入口与模块文档统一放在 `db/`。真实输入与生成文件放在 `data/` 或组内共享位置。全量结果见 [验收摘要](VALIDATION.md)。

## 表与分片

两个节点都使用 [schema.sql](schema.sql) 的 `checkins` 表：

| 字段 | SQLite 类型 | 规则 |
| --- | --- | --- |
| id | INTEGER | 分片内主键；与节点号一起标识记录 |
| user_id | TEXT | 非空，保留前导零 |
| checkin_time | TEXT | 整秒 UTC，统一 `YYYY-MM-DDTHH:MM:SS+00:00` |
| latitude | REAL | -90 至 90 度 |
| longitude | REAL | -180 至 180 度 |
| location_id | TEXT | 非空，保留前导零 |

五个数据字段组成唯一约束。同一次签到时间的 `Z`、`+00:00`、其他时区写法先统一 UTC 后再去重；数值相等的坐标也视为相同。不会按地点 ID 单独合并记录，也不会假设同一地点只有一组坐标。

分片规则沿用原仓库：

```python
hashlib.sha256(user_id.encode("utf-8")).digest()[0] % 2
```

这不是 `int(user_id) % 2`，也不是 Python 的 `hash()`。相同用户始终进入同一个节点，后端导入同一个 `shard_for` 函数进行路由。两个 SQLite 文件要作为同一批数据库交付。

索引在批量导入完成后创建：

| 索引 | 用途 |
| --- | --- |
| 五字段 UNIQUE 自动索引 | 去重及唯一约束 |
| idx_checkin_time(checkin_time DESC, id DESC) | 全局时间排序及分页 |
| idx_user_time(user_id, checkin_time DESC, id DESC) | 指定用户的时间排序查询 |
| idx_latitude(latitude) | 空间框纬度筛选；经度作为剩余条件过滤 |

目前没有空间树索引，也没有自动复制、故障转移或多节点事务。这是应用层分片配合独立 SQLite 查询进程；需要课程要求指定数据库产品时，应先确认技术选型。

## 本机全量导入

需要 Python 3.11 或以上，只使用标准库，无需安装 pip 依赖。以下命令均从仓库根目录执行。先用 `python --version` 确认版本；Windows 若 `python` 版本过低或命令不存在，可将下文的 `python` 换成 `py -3`（同样需要确认选中 3.11 或以上）。

已有清洗结果时，直接使用 CSV 的实际路径导入，例如：

```sh
python -m db.load data/clean.csv data/full
```

首次构建完成后生成：

```text
data/full/node-0.sqlite
data/full/node-1.sqlite
data/full/import-report.json
```

输出目录中已有任意节点文件或导入报告时，导入器拒绝覆盖。已完成构建的电脑无需重跑。要验证另一批数据，请把 `data/full` 换成 `data/full-v2` 等新目录，不要在服务运行时替换数据库。

导入按默认每 10,000 行批量提交，内存不随 CSV 行数线性增长，每 100,000 行输出进度。CSV 支持 UTF-8 BOM，表头必须依次为 `user_id,checkin_time,latitude,longitude,location_id`。遇到无效字段、日期、非有限坐标或格式错误会停止；不会悄悄跳行。异常记录需交回清洗模块处理。

构建先写入输出目录下的临时文件，校验完并关闭连接后发布。正常异常处理会清理临时文件和已发布的部分文件；进程被强制杀死或断电时可能遗留 `.import.lock` 或部分文件，建议保留现场并改用新目录重建。不要把两个不同批次的节点混用。

报告包含 CSV 的 SHA-256、文件大小、UTC 导入时间、入库数、重复数、两个节点行数、时间范围、文件大小及耗时。必须满足：

```text
valid = duplicates + inserted
inserted = node_counts[0] + node_counts[1]
```

其中 `valid` 指本次成功校验的 CSV 数据行数，不是原始 TSV 行数，也不包含清洗同学此前剔除的记录。

## 启动和验收

终端一启动三个独立进程：

```sh
python -m db.serve data/full
```

看到 `Ready` 后打开 <http://127.0.0.1:8000>。节点分别监听 8100 和 8101，网页后端监听 8000，均限本机访问。Ctrl+C 停止全部进程。端口被占用时先关闭旧实例，或指定 `--port 8200 --node-0-port 8201 --node-1-port 8202`。

Windows 也可双击 `db/run-local.cmd`，自动回到仓库根目录，用 `py -3` 启动 `data/full`。根目录入门样例生成的数据库位于 `data/`，应改用 `python -m db.serve data`。

终端二对比直接 SQL 与现有 API：

```sh
python -m db.verify data/full --api-url http://127.0.0.1:8000 --full-integrity --report data/full/verification-report.json
```

校验内容包括两个节点完整性、与导入报告的行数对账、逐个用户验证分片归属、用户查询索引执行计划，以及节点计数、全局分页、跨页偏移、两个节点用户路由、UTC 时间区间、空间框和空结果。API 的行内容、顺序、节点和 `has_more` 必须与直接 SQL 一致。HTTP 耗时只表示此次本机请求，不是压力测试。

仅检查数据库、不启动服务时：

```sh
python -m db.verify data/full --report data/full/verification-report.json
```

使用构造数据运行回归测试，无需课程文件：

```sh
python -m unittest db.test_database -v
```

`db.serve` 只是现有节点与后端的启动器，不替代其接口。也可按根目录 README 分别启动三个进程，路径改为 `data/full/node-0.sqlite` 和 `data/full/node-1.sqlite`。现有 `compose.yaml` 仍使用 `data/node-*.sqlite`，本次验证采用本地 Python 启动方式；Docker 未验证。

## Git 交付范围

提交数据库代码、表结构、构造数据测试与说明。`data/` 和 `*.sqlite` 已忽略，不提交真实 CSV、原始轨迹、生成的数据库或含真实查询记录的报告。全量验收材料保存在本机 `data/full/`，通过组内共享位置传递。

按仓库协作约定在 `feature/database` 分支开发，本地验证后推送该分支，并向 `main` 提交 PR。提交前检查 `git diff --check`、`git diff --cached` 和 `git status`，确认没有混入真实数据或本机配置。
