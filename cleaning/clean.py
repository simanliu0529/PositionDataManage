# 清洗 Gowalla 签到 TSV，产出带表头的 CSV

import argparse
import csv
import json
import math
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

FIELDS = ("user_id", "checkin_time", "latitude", "longitude", "location_id")

# 规则参数（可在命令行覆盖）
MAX_SPEED_KMH = 1000.0     # 相邻签到隐含速度上限；民航巡航约 900 km/h，留 10% 余量
COORD_DECIMALS = 6         # 输出经纬度小数位，约 0.1 m
NULL_ISLAND_EPS = 1e-9     # (0,0) 判定容差
BUCKETS = 32               # 按 user_id 哈希分桶数，控制单桶内存
EARTH_RADIUS_KM = 6371.0088


def _parse_time(text):
    """返回 (UTC 整秒 epoch, 规范化 ISO 字符串)，格式与 normalize 保持一致。"""
    if (len(text) == 20 and text[10] == "T" and text[19] == "Z"
            and text[4] == "-" and text[7] == "-"
            and text[13] == ":" and text[16] == ":"):
        try:
            moment = datetime(int(text[0:4]), int(text[5:7]), int(text[8:10]),
                              int(text[11:13]), int(text[14:16]), int(text[17:19]),
                              tzinfo=timezone.utc)
        except ValueError:
            raise ValueError("invalid_time") from None
        return int(moment.timestamp()), text[:19] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
        if moment.utcoffset() is None or moment.microsecond:
            raise ValueError
        moment = moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError("invalid_time") from None
    return int(moment.timestamp()), moment.isoformat(timespec="seconds")


def _validate(values):
    """字段级校验与规范化，返回 (user, iso, lat, lon, location, epoch)。"""
    user, text, lat_text, lon_text, location = (value.strip() for value in values)
    if not user or not location:
        raise ValueError("missing_id")
    epoch, iso = _parse_time(text)
    try:
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        raise ValueError("invalid_coordinates") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("invalid_coordinates")
    return user, iso, lat, lon, location, epoch


def normalize(row):
    """兼容接口：backend 用它对 start/end 查询参数做同样的时间规范化。"""
    user, iso, lat, lon, location, _ = _validate(row)
    return user, iso, lat or 0.0, lon or 0.0, location


def haversine_km(lat1, lon1, lat2, lon2):
    """球面两点距离（km）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _majority_coordinates(locations, conflicts):
    """同一 location_id 出现多套坐标时，取出现次数最多的坐标作为权威坐标。"""
    fixes = {}
    for location, counts in conflicts.items():
        base = (locations[location][0], locations[location][1])
        counts.setdefault(base, locations[location][2])
        best = max(counts.items(), key=lambda item: item[1])[0]
        if abs(best[0] - base[0]) > 1e-9 or abs(best[1] - base[1]) > 1e-9:
            fixes[location] = best
        locations[location][0], locations[location][1] = best
    return fixes


def clean(source, target, *, report=None, dedupe=True, max_speed_kmh=MAX_SPEED_KMH,
          repair_coords=True, min_user_checkins=0, min_loc_checkins=0, buckets=BUCKETS):
    """执行清洗，返回计数与剔除原因。"""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise ValueError("Input and output must differ")
    if buckets < 1:
        raise ValueError("buckets must be >= 1")
    target.parent.mkdir(parents=True, exist_ok=True)

    stats = Counter(input=0)
    started = perf_counter()

    with TemporaryDirectory(dir=target.parent, prefix=".clean-") as workspace:
        workspace = Path(workspace)

        # ---------- Pass 1：L1–L4 校验、去重，并按 user_id 分桶落盘 ----------
        handles = [open(workspace / f"b{index:02d}.tsv", "w", encoding="utf-8", newline="\n")
                   for index in range(buckets)]
        seen = set()
        locations = {}
        conflicts = {}
        try:
            with source.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    if not line.strip():
                        stats["blank_line"] += 1
                        continue
                    stats["input"] += 1
                    values = line.rstrip("\r\n").split("\t")
                    if len(values) != len(FIELDS):
                        stats["invalid_field_count"] += 1
                        continue
                    try:
                        user, iso, lat, lon, location, epoch = _validate(values)
                    except ValueError as error:
                        stats[str(error)] += 1
                        continue
                    if abs(lat) < NULL_ISLAND_EPS and abs(lon) < NULL_ISLAND_EPS:
                        stats["null_island"] += 1
                        continue
                    stats["valid"] += 1
                    if dedupe:
                        key = hash((user, epoch, location))
                        if key in seen:
                            stats["duplicate"] += 1
                            continue
                        seen.add(key)
                    entry = locations.get(location)
                    if entry is None:
                        locations[location] = [lat, lon, 1]
                    elif abs(entry[0] - lat) < 1e-9 and abs(entry[1] - lon) < 1e-9:
                        entry[2] += 1
                    else:
                        counts = conflicts.setdefault(location, {(entry[0], entry[1]): entry[2]})
                        counts[(lat, lon)] = counts.get((lat, lon), 0) + 1
                    handles[zlib.crc32(user.encode("utf-8")) % buckets].write(
                        f"{user}\t{epoch}\t{lat!r}\t{lon!r}\t{location}\t{iso}\n"
                    )
        finally:
            for handle in handles:
                handle.close()
        seen = None
        stats["coord_conflict_locations"] = len(conflicts)

        # ---------- L5：POI 坐标众数校正 ----------
        fixes = _majority_coordinates(locations, conflicts) if (repair_coords and conflicts) else {}
        locations = None
        conflicts = None

        # ---------- Pass 2：L6 时空一致性校验并写出 CSV ----------
        staged = workspace / "clean.csv"
        users = Counter()
        places = Counter()
        with staged.open("w", encoding="utf-8", newline="") as dst:
            writer = csv.writer(dst)
            writer.writerow(FIELDS)
            for index in range(buckets):
                rows = []
                with (workspace / f"b{index:02d}.tsv").open(encoding="utf-8") as stream:
                    for line in stream:
                        parts = line.rstrip("\n").split("\t")
                        rows.append((parts[0], int(parts[1]), float(parts[2]),
                                     float(parts[3]), parts[4], parts[5]))
                rows.sort(key=lambda row: (row[0], row[1]))
                previous = None
                for user, epoch, lat, lon, location, iso in rows:
                    fix = fixes.get(location)
                    if fix is not None:
                        lat, lon = fix
                        stats["coord_repair"] += 1
                    if previous is not None and previous[0] == user:
                        if epoch == previous[1]:
                            stats["conflict_same_time"] += 1
                            continue
                        if max_speed_kmh > 0:
                            hours = (epoch - previous[1]) / 3600.0
                            if hours > 0 and haversine_km(previous[2], previous[3], lat, lon) / hours > max_speed_kmh:
                                stats["speed_anomaly"] += 1
                                continue
                    writer.writerow((user, iso, round(lat, COORD_DECIMALS), round(lon, COORD_DECIMALS), location))
                    users[user] += 1
                    places[location] += 1
                    previous = (user, epoch, lat, lon)
        stats["output"] = sum(users.values())

        # ---------- L7：可选的稀疏过滤（生成分析集） ----------
        result_path = staged
        if min_user_checkins or min_loc_checkins:
            filtered = workspace / "core.csv"
            kept = 0
            with staged.open(encoding="utf-8", newline="") as src, filtered.open("w", encoding="utf-8", newline="") as dst:
                reader = csv.reader(src)
                writer = csv.writer(dst)
                writer.writerow(next(reader))
                for row in reader:
                    if users[row[0]] >= min_user_checkins and places[row[4]] >= min_loc_checkins:
                        writer.writerow(row)
                        kept += 1
            stats["filtered_output"] = kept
            stats["filtered_users"] = sum(1 for count in users.values() if count >= min_user_checkins)
            result_path = filtered
        result_path.replace(target)

    stats["users"] = len(users)
    stats["locations"] = len(places)
    stats["elapsed_seconds"] = round(perf_counter() - started, 1)
    result = dict(stats)
    result["config"] = {
        "dedupe": dedupe,
        "max_speed_kmh": max_speed_kmh,
        "repair_coords": repair_coords,
        "min_user_checkins": min_user_checkins,
        "min_loc_checkins": min_loc_checkins,
    }
    if report is not None:
        report_path = Path(report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="清洗 Gowalla 签到 TSV 为带表头的 CSV")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--report", type=Path, help="把清洗计数与剔除原因写入 JSON 文件")
    parser.add_argument("--max-speed-kmh", type=float, default=MAX_SPEED_KMH,
                        help="瞬时速度上限，0 表示关闭该项校验")
    parser.add_argument("--no-dedupe", action="store_true", help="关闭去重")
    parser.add_argument("--no-coord-repair", action="store_true", help="关闭 POI 坐标众数校正")
    parser.add_argument("--min-user-checkins", type=int, default=0, help="用户最少签到次数，>0 时输出分析集")
    parser.add_argument("--min-loc-checkins", type=int, default=0, help="地点最少被签到次数，>0 时输出分析集")
    parser.add_argument("--buckets", type=int, default=BUCKETS, help="分桶数，内存不足时调大")
    args = parser.parse_args()
    print(json.dumps(clean(
        args.source, args.target, report=args.report,
        dedupe=not args.no_dedupe, max_speed_kmh=args.max_speed_kmh,
        repair_coords=not args.no_coord_repair,
        min_user_checkins=args.min_user_checkins,
        min_loc_checkins=args.min_loc_checkins, buckets=args.buckets,
    ), ensure_ascii=False))
