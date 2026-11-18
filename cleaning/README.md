# cleaning 模块说明

把原始签到 TSV（无表头，`user_id / checkin_time / latitude / longitude / location_id`）清洗为
带表头 CSV，供 `db.load` 导入两个 SQLite 分片节点。

## 对下游的固定约定

| 约定 | 位置 | 使用者 |
| --- | --- | --- |
| `FIELDS`：表头与列顺序 | `clean.py` | `db/load.py` 校验 CSV 表头 |
| `normalize(row)`：单行/查询时间规范化 | `clean.py` | `backend/http_io.py` 规范化 `start`/`end` |
| 时间格式 `YYYY-MM-DDTHH:MM:SS+00:00`（整秒 UTC） | `clean.py` | 节点内按字符串比较时间范围 |
| 输出 CSV 含表头、无重复记录 | `clean()` | 入库、查询 |

改动上述任一项都要同步 `db/`、`backend/` 并让下游 review。

## 清洗规则

| 层级 | 规则 | 统计键 | 全量实测（6,442,892 条） |
| --- | --- | --- | --- |
| L1 规范化 | 去首尾空白、时间统一整秒 UTC、经纬度 6 位小数 | — | — |
| L2 结构校验 | 字段数必须为 5 | `invalid_field_count` | 0 |
| L3 合法性 | 用户/地点非空 | `missing_id` | 0 |
| L3 合法性 | 时间可解析且必须带时区、无微秒 | `invalid_time` | 0 |
| L3 合法性 | 纬度 ±90、经度 ±180 | `invalid_coordinates` | 29 |
| L3 合法性 | 剔除 (0,0) 空值哨兵 | `null_island` | 135 |
| L4 唯一性 | 按 `(user_id, checkin_time, location_id)` 去重，保留首次 | `duplicate` | 601 |
| L5 一致性 | 同一 `location_id` 多套坐标按众数校正 | `coord_repair` / `coord_conflict_locations` | 2,207 条 / 297 个 POI |
| L6 时空一致 | 相邻签到隐含速度 > 1000 km/h 判为瞬移并剔除 | `speed_anomaly` | 11,434 |
| L6 时空一致 | 同一用户同一时刻出现在不同地点，保留首条 | `conflict_same_time` | 53 |
| L7 可用性（可选） | 用户 ≥ N 次、地点 ≥ M 次才保留 | `filtered_output` / `filtered_users` | 默认关闭 |

**守恒校验**：`input − (invalid + null_island + duplicate + conflict_same_time + speed_anomaly) = output`
即 `6,442,892 − 12,252 = 6,430,640`。

## 用法

```sh
python -m cleaning.clean sample/checkins.tsv data/clean.csv
python -m cleaning.clean data/Gowalla_totalCheckins.txt data/clean.csv --report data/clean-report.json
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--report` | 无 | 把计数与剔除原因写成 JSON |
| `--max-speed-kmh` | 1000 | 瞬时速度上限，`0` 关闭该项（民航巡航约 900 km/h） |
| `--no-dedupe` | 去重开启 | 关闭去重（去重也可留给入库阶段的 UNIQUE 约束） |
| `--no-coord-repair` | 校正开启 | 关闭 POI 坐标众数校正 |
| `--min-user-checkins` / `--min-loc-checkins` | 0 | >0 时额外输出分析集（过滤稀疏用户/地点） |
| `--buckets` | 32 | 分桶数，内存不足时调大 |

## 实现要点

- **两遍式处理**：第一遍校验、去重并按 `user_id` 哈希分桶落盘；第二遍逐桶载入、按时间排序后做
  时空一致性判定。避免对 644 万行整体排序，峰值内存 < 1 GB。
- **速度判定用 Haversine**（R = 6371.0088 km），以上一个**被接受**的点为参照，避免误差级联。
- **分桶按 `crc32(user_id)`**，保证同一用户的记录必在同一桶内，分组完整。
- 全量 644 万条实测：127 秒，输出 6,430,640 条。

## 样例基线

`python -m cleaning.clean sample/checkins.tsv data/clean.csv` 应输出：

```
input 6 / valid 5 / invalid_coordinates 1 / duplicate 1 / output 4
```

随后 `python -m db.load data/clean.csv data` 得到 `valid 4 / duplicates 0 / node_counts [2,2]`。
（去重已前移到清洗阶段，入库阶段不再产生重复，最终入库行数仍为 4 行。）
