"""Batch-pinned overview and bounded exports; registered by the authenticated app.

Exports read each node once, apply the same spatial predicates as interactive
queries, and assemble the whole result before sending any download bytes.
"""
import csv
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlencode

from flask import g, jsonify, request

from backend.control import APIError
from backend.spatial import params
from backend.worker import node_request
from db.load import shard_for


EXPORT_MAX_ROWS = 10000
CSV_FIELDS = ('batch_id', 'node', 'id', 'user_id', 'checkin_time',
              'latitude', 'longitude', 'location_id')


def _query(extra=()):
    try:
        return params(request.query_string.decode('utf-8'), extra)
    except UnicodeError:
        raise APIError(400, 'INVALID_PARAMETERS', '参数编码无效。') from None


def _number(value):
    """An absent legacy metric is unknown, rather than an invented zero."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def _quality(report):
    imported = report.get('database_import')
    imported = imported if isinstance(imported, dict) else {}
    cleaned = report.get('cleaning_stats')
    cleaned = cleaned if isinstance(cleaned, dict) else {}
    raw = _number(report.get('scanned_rows'))
    inserted = _number(imported.get('inserted'))
    if inserted is None:
        counts = report.get('node_counts')
        if isinstance(counts, list) and len(counts) == 2 and all(_number(n) is not None for n in counts):
            inserted = sum(counts)
    result = {
        'raw': raw,
        'rejected': _number(report.get('rejected_rows')),
        'duplicates': _number(report.get('duplicates')),
        'inserted': inserted,
        'coordinate_repairs': _number(cleaned.get('coord_repair',0)) if isinstance(cleaned.get('config'),dict) and 'repair_coords' in cleaned['config'] else _number(cleaned.get('coord_repair')),
        'retention_rate': round(inserted / raw * 100, 4) if raw and inserted is not None else None,
        'retention_label': '清洗留存率（不代表数据准确率）',
    }
    nodes = imported.get('nodes')
    nodes = nodes if isinstance(nodes, list) else []
    starts = [row['first_checkin'] for row in nodes if isinstance(row, dict) and isinstance(row.get('first_checkin'), str)]
    ends = [row['last_checkin'] for row in nodes if isinstance(row, dict) and isinstance(row.get('last_checkin'), str)]
    return result, {'start': min(starts) if starts else None, 'end': max(ends) if ends else None}


def _count(store, batch_id, index):
    data = node_request(store.nodes[index], '/count?' + urlencode({'batch_id': batch_id}))
    if data.get('batch_id') != batch_id:
        raise APIError(503, 'BATCH_MISMATCH', '节点返回了错误批次，已停止汇总。')
    count = data.get('count')
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise APIError(503, 'INVALID_NODE_RESPONSE', '节点返回的计数无效。')
    return count


def _spreadsheet_text(value):
    """Prefix dangerous text cells; quoting alone does not prevent formulas."""
    text = str(value)
    if text.startswith(('\t', '\r', '\n')) or text.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + text
    return text


def register_data_services(app, store):
    """Attach routes after the app's session/role guards have been installed."""

    @app.get('/api/v2/overview')
    def data_overview():
        p, _, _ = _query()
        if set(request.args) - {'batch_id'}:
            raise APIError(400, 'INVALID_PARAMETERS', '概览仅接受 batch_id 参数。')
        requested = p.get('batch_id')
        selected = requested or store.active()
        empty_quality, empty_range = _quality({})
        result = {
            'has_data': False, 'batch_id': None, 'published_at': None,
            'total_count': 0, 'entities': {'users': 0, 'locations': 0, 'months': 0},
            'monthly_trend': [], 'top_users': [], 'top_locations': [],
            'quality': empty_quality, 'time_range': empty_range,
            'nodes': [], 'nodes_consistent': None, 'catalog_consistent': None,
        }
        with store.db() as db:
            if g.user['role'] == 'admin':
                states = {row['status']: row['n'] for row in db.execute('SELECT status,count(*) n FROM import_jobs GROUP BY status')}
                result['tasks'] = {'total': sum(states.values()), 'status_counts': states}
        if selected is None:
            return jsonify(result)
        batch_id = store.batch(selected)
        with store.db() as db:
            batch = dict(db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone())
            row = db.execute('SELECT report FROM import_jobs WHERE job_id=?', (batch['job_id'],)).fetchone()
            try:
                report = json.loads(row['report']) if row else {}
            except (ValueError, TypeError):
                report = {}
            if not isinstance(report, dict):
                report = {}
            shards = {row['node_id']: row['row_count'] for row in db.execute('SELECT node_id,row_count FROM batch_shards WHERE batch_id=?', (batch_id,))}
            totals = {row['group_by']: (row['entities'], row['records']) for row in db.execute('SELECT group_by,count(*) entities,sum(count) records FROM catalog_counts WHERE batch_id=? GROUP BY group_by', (batch_id,))}
            months = [dict(row) for row in db.execute("SELECT key month,count FROM catalog_counts WHERE batch_id=? AND group_by='month' ORDER BY key", (batch_id,))]
            users = [dict(row) for row in db.execute("SELECT key user_id,count FROM catalog_counts WHERE batch_id=? AND group_by='user' ORDER BY count DESC,key ASC LIMIT 5", (batch_id,))]
            locations = [dict(row) for row in db.execute("SELECT key location_id,count FROM catalog_counts WHERE batch_id=? AND group_by='location' ORDER BY count DESC,key ASC LIMIT 5", (batch_id,))]
        with ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(lambda index: _count(store, batch_id, index), range(2)))
        total = sum(counts)
        nodes = [{'node': i, 'count': count, 'expected_count': shards.get(i),
                  'consistent': count == shards[i] if i in shards else None} for i, count in enumerate(counts)]
        quality, time_range = _quality(report)
        result.update({
            'has_data': True, 'batch_id': batch_id, 'published_at': batch['published_at'],
            'total_count': total,
            'entities': {'users': totals.get('user', (0, 0))[0],
                         'locations': totals.get('location', (0, 0))[0],
                         'months': totals.get('month', (0, 0))[0]},
            'monthly_trend': months, 'top_users': users, 'top_locations': locations,
            'quality': quality, 'time_range': time_range, 'nodes': nodes,
            'nodes_consistent': all(n['consistent'] for n in nodes) if len(shards) == 2 else None,
            'catalog_consistent': all(totals.get(group, (0, 0))[1] == total for group in ('user', 'month', 'location')),
        })
        return jsonify(result)

    @app.get('/api/v2/checkins/export')
    def export_checkins():
        p, _, _ = _query(('format',))
        output_format = p.pop('format', 'csv')
        if output_format not in ('csv', 'geojson'):
            raise APIError(400, 'INVALID_EXPORT_FORMAT', '导出格式仅支持 csv 或 geojson。')
        # Pagination belongs to the interactive page. Export always covers all
        # matching records; the hard bound is checked before creating a file.
        batch_id = store.batch(p.get('batch_id'))
        p['batch_id'] = batch_id
        indexes = [shard_for(p['user_id'])] if 'user_id' in p else [0, 1]
        query = urlencode(p)

        def fetch(index):
            data = node_request(store.nodes[index], '/export?' + query)
            if data.get('batch_id') != batch_id:
                raise APIError(503, 'BATCH_MISMATCH', '节点返回了错误批次，未生成导出文件。')
            items = data.get('items')
            if not isinstance(items, list) or len(items) > EXPORT_MAX_ROWS + 1:
                raise APIError(503, 'INVALID_NODE_RESPONSE', '节点返回的数据格式无效，未生成导出文件。')
            if any(not isinstance(row, dict) or not {'id', 'user_id', 'checkin_time', 'latitude', 'longitude', 'location_id'} <= set(row) for row in items):
                raise APIError(503, 'INVALID_NODE_RESPONSE', '节点返回的记录无效，未生成导出文件。')
            return [{**row, 'node': index} for row in items]

        with ThreadPoolExecutor(max_workers=len(indexes)) as pool:
            fragments = list(pool.map(fetch, indexes))
        rows = [row for fragment in fragments for row in fragment]
        if len(rows) > EXPORT_MAX_ROWS:
            raise APIError(422, 'EXPORT_LIMIT_EXCEEDED', '匹配记录超过 10000 条，请缩小用户、时间或空间筛选范围后导出；未生成截断文件。')
        rows.sort(key=lambda row: (row['checkin_time'], row['id'], row['node']), reverse=True)
        metadata = {'batch_id': batch_id, 'filters': {key: value for key, value in p.items() if key != 'batch_id'},
                    'exported_count': len(rows), 'coordinate_system': 'WGS84 (EPSG:4326)',
                    'scope': 'all_matching_records', 'max_rows': EXPORT_MAX_ROWS}
        if output_format == 'geojson':
            features = [{'type': 'Feature', 'id': f"{row['node']}:{row['id']}",
                         'geometry': {'type': 'Point', 'coordinates': [row['longitude'], row['latitude']]},
                         'properties': {key: value for key, value in row.items() if key not in ('longitude', 'latitude')}} for row in rows]
            body = json.dumps({'type': 'FeatureCollection', 'metadata': metadata, 'features': features}, ensure_ascii=False).encode('utf-8')
            content_type = 'application/geo+json; charset=utf-8'
        else:
            stream = io.StringIO(newline='')
            writer = csv.writer(stream)
            writer.writerow(CSV_FIELDS)
            for row in rows:
                record = {**row, 'batch_id': batch_id}
                writer.writerow([_spreadsheet_text(record[key]) if key in ('user_id', 'location_id') else record[key] for key in CSV_FIELDS])
            body = stream.getvalue().encode('utf-8-sig')
            content_type = 'text/csv; charset=utf-8'
        response = app.response_class(body, content_type=content_type)
        safe_batch = re.sub(r'[^A-Za-z0-9_-]', '', batch_id)
        response.headers['Content-Disposition'] = f'attachment; filename="checkins-{safe_batch}.{output_format}"'
        response.headers['X-Export-Metadata'] = quote(json.dumps(metadata, ensure_ascii=False, separators=(',', ':')), safe='')
        response.headers['X-Checkin-Batch'] = batch_id
        response.headers['X-Export-Count'] = str(len(rows))
        response.headers['X-Export-CRS'] = 'EPSG:4326'
        return response
