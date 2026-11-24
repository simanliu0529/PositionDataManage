"""Bounded format adapters. The shared cleaner always receives canonical TSV.

JSON accepts an array of flat objects, a single object, or JSON Lines. CSV
requires a header. Coordinates are declared WGS 84; no GCJ/BD conversion occurs.
"""
import csv
import codecs
import io
import json
import os
from pathlib import Path
from cleaning.clean import FIELDS
from backend.control import APIError

ALIASES = {
    'user_id': ('user_id', 'userid', 'user', 'uid', '用户id', '用户编号'),
    'checkin_time': ('checkin_time', 'checkintime', 'timestamp', 'time', 'datetime', 'created_at', '签到时间'),
    'latitude': ('latitude', 'lat', '纬度'),
    'longitude': ('longitude', 'lon', 'lng', '经度'),
    'location_id': ('location_id', 'locationid', 'venue_id', 'poi_id', 'place_id', '地点id'),
}
FORMATS = {'.txt': 'tsv', '.tsv': 'tsv', '.csv': 'csv', '.json': 'json', '.jsonl': 'jsonl', '.ndjson': 'jsonl'}


def import_config(filename, mapping=None, coordinate_system='WGS84'):
    kind = FORMATS.get(Path(filename).suffix.lower())
    if not kind:
        raise APIError(415, 'UNSUPPORTED_FILE', '支持 TXT、TSV、带表头 CSV、JSON 数组和 JSONL。')
    if coordinate_system != 'WGS84':
        raise APIError(400, 'UNSUPPORTED_CRS', '当前只接受 WGS 84 经纬度；请先转换其他坐标系。')
    if mapping is None or mapping == '':
        mapping = {}
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (ValueError, TypeError):
            raise APIError(400, 'INVALID_MAPPING', '字段映射必须为 JSON 对象。') from None
    if (not isinstance(mapping, dict) or set(mapping) - set(FIELDS)
            or any(not isinstance(v, str) or not v or len(v) > 200 for v in mapping.values())
            or (mapping and (set(mapping) != set(FIELDS) or len(set(mapping.values())) != len(FIELDS)))):
        raise APIError(400, 'INVALID_MAPPING', '请为五个标准字段各选择一个不同的源字段。')
    if kind == 'tsv' and mapping:
        raise APIError(400, 'INVALID_MAPPING', '无表头 TSV 按固定五列读取，不使用字段映射。')
    return {'format': kind, 'field_mapping': mapping, 'coordinate_system': 'WGS84'}


def resolve_mapping(columns, selected):
    if len(columns) != len(set(columns)):
        raise ValueError('表头含重复字段，请先修正源文件。')
    if selected:
        if not set(selected.values()) <= set(columns):
            raise ValueError('映射中的字段在文件内不存在。')
        return selected
    normalized = {name.strip().lower(): name for name in columns}
    result = {}
    for field, aliases in ALIASES.items():
        candidates = [normalized[a] for a in aliases if a in normalized]
        if not candidates:
            raise ValueError('无法自动识别字段 ' + field + '，请在上传预览中指定映射。')
        result[field] = candidates[0]
    return result


def unique_object(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ValueError('JSON 记录含重复字段，已停止解析。')
        result[key] = value
    return result


def json_records(stream):
    """Incremental JSON array decoding; never read the entire source into memory."""
    decoder = json.JSONDecoder(object_pairs_hook=unique_object)
    buffer = ''
    eof = False
    maximum = int(os.environ.get('CHECKIN_MAX_LINE_BYTES', 65536))

    def read_more():
        nonlocal buffer, eof
        chunk = stream.read(8192)
        eof = not chunk
        buffer += chunk

    def whitespace():
        nonlocal buffer
        while True:
            buffer = buffer.lstrip()
            if buffer or eof:
                return
            read_more()

    def value():
        nonlocal buffer
        while True:
            try:
                obj, end = decoder.raw_decode(buffer)
                if len(buffer[:end].encode('utf-8')) > maximum:
                    raise ValueError('单条 JSON 记录超过上限。')
                buffer = buffer[end:]
                return obj
            except json.JSONDecodeError:
                if eof or len(buffer.encode('utf-8')) > maximum:
                    raise ValueError('JSON 格式无效或单条记录超过上限。') from None
                read_more()

    whitespace()
    if not buffer:
        return
    if buffer[0] != '[':
        yield value()
        whitespace()
        if buffer:
            raise ValueError('JSON 根应为数组或单个对象；多行对象请使用 .jsonl。')
        return
    buffer = buffer[1:]
    whitespace()
    if buffer.startswith(']'):
        buffer = buffer[1:]
    else:
        while True:
            whitespace()
            yield value()
            whitespace()
            if buffer.startswith(']'):
                buffer = buffer[1:]
                break
            if not buffer.startswith(','):
                raise ValueError('JSON 数组缺少逗号或结束括号。')
            buffer = buffer[1:]
    whitespace()
    if buffer:
        raise ValueError('JSON 数组后存在多余内容。')


def records(stream, kind, on_invalid=None):
    def lines():
        maximum = int(os.environ.get('CHECKIN_MAX_LINE_BYTES', 65536))
        while True:
            line = stream.readline(maximum + 1)
            if not line:
                break
            if len(line.encode('utf-8')) > maximum:
                raise ValueError('单行超过配置上限。')
            yield line
    if kind == 'csv':
        reader = csv.reader(lines(), strict=True)
        columns = next(reader, None)
        if not columns:
            raise ValueError('CSV 缺少表头。')
        if len(columns) > 100 or any(len(c) > 200 for c in columns):
            raise ValueError('CSV 表头过大。')
        for row in reader:
            valid=len(row)==len(columns)
            if not valid and on_invalid: on_invalid(row)
            yield columns, dict(zip(columns, row)) if valid else None
    else:
        iterator = json_records(stream) if kind == 'json' else (json.loads(line,object_pairs_hook=unique_object) for line in lines() if line.strip())
        columns = None
        for record in iterator:
            if isinstance(record, dict) and columns is None:
                columns = list(record)
                if len(columns) > 100 or any(len(c) > 200 for c in columns):
                    raise ValueError('JSON 字段过多或字段名过长。')
            if not isinstance(record,dict) and on_invalid: on_invalid(record)
            yield columns or [], record if isinstance(record, dict) else None


def preview(content, config):
    text = codecs.getincrementaldecoder('utf-8-sig')().decode(content, final=False)
    result = {'format': config['format'], 'coordinate_system': 'WGS84', 'sample_only': True, 'columns': [], 'rows': [], 'field_mapping': {}, 'mapping_error': None}
    if config['format'] == 'tsv':
        result['columns'] = list(FIELDS)
        result['field_mapping'] = {key: key for key in FIELDS}
        result['rows'] = [dict(zip(FIELDS, line.split('\t'))) for line in text.splitlines()[:5]]
        return result
    for columns, row in records(io.StringIO(text), config['format']):
        if not result['columns']:
            result['columns'] = columns
            try:
                result['field_mapping'] = resolve_mapping(columns, config['field_mapping'])
            except ValueError as error:
                result['mapping_error'] = str(error)
        result['rows'].append(row)
        if len(result['rows']) == 5:
            break
    return result


def canonicalize(source, target, config, report, progress):
    """One output line per source data record, so rejection accounting stays exact."""
    maximum = int(os.environ.get('CHECKIN_MAX_LINE_BYTES', 65536))
    report['input_format'] = config['format']
    report['record_basis'] = '原始物理行（含空行）' if config['format'] == 'tsv' else '数据记录序号（CSV 不含表头；JSON 按数组顺序）'
    report['coordinate_system'] = 'WGS84'
    if config['format'] == 'tsv':
        return source
    count = 0
    selected = None
    invalid_source = None
    def remember_invalid(value):
        nonlocal invalid_source
        invalid_source=value
    with source.open(encoding='utf-8-sig', newline='') as src, target.open('w', encoding='utf-8', newline='\n') as out:
        for columns, record in records(src, config['format'],remember_invalid):
            count += 1
            if selected is None and columns:
                selected = resolve_mapping(columns, config['field_mapping'])
                report['field_mapping'] = selected
            values = []
            invalid = record is None or selected is None
            if not invalid:
                for field in FIELDS:
                    value = record.get(selected[field], '')
                    if value is None:
                        value = ''
                    if isinstance(value, (dict, list, bool)):
                        invalid = True
                    value = str(value)
                    if any(c in value for c in ('\t', '\r', '\n')):
                        invalid = True
                    values.append(value)
            line = 'INVALID_RECORD' if invalid else '\t'.join(values)
            if invalid:
                samples=report.setdefault('parse_rejections',[])
                if len(samples)<5:
                    samples.append({'record_number':count,'source':json.dumps(record if record is not None else invalid_source,ensure_ascii=False)[:2000],'reason':'结构、字段类型或字段中的控制字符不符合五列文本约定'})
            if len(line.encode('utf-8')) > maximum:
                raise ValueError('单条标准化记录超过上限。')
            out.write(line + '\n')
            if count % 10000 == 0:
                report['parsed_records'] = count
                progress()
    report['parsed_records'] = count
    if not count:
        raise ValueError('文件中没有数据记录。')
    return target
