"""Pure format-adapter boundary tests; no project data or servers are touched."""
import csv
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.control import APIError
from backend.ingestion import canonicalize, import_config, json_records, preview, records, resolve_mapping
from cleaning.clean import FIELDS


def record(user='u1', **extra):
    return dict(zip(FIELDS, (user, '2010-10-19T23:55:27Z', '30.2359', '-97.7951', 'P1')), **extra)


class BoundedText(io.StringIO):
    def __init__(self, text, chunk=8192):
        super().__init__(text)
        self.chunk = chunk
        self.reads = []

    def read(self, size=-1):
        if size < 0 or size > 8192:
            raise AssertionError('JSON decoder requested an unbounded source read')
        self.reads.append(size)
        return super().read(min(size, self.chunk))


class IngestionFormats(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.serial = 0

    def tearDown(self):
        self.temp.cleanup()

    def adapt(self, content, filename='fixture.json', mapping=None):
        self.serial += 1
        source = self.root / (str(self.serial) + '-' + filename)
        target = self.root / (str(self.serial) + '-canonical.tsv')
        source.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        report = {}
        output = canonicalize(source, target, import_config(filename, mapping), report, lambda: None)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        return output.read_text(encoding='utf-8'), report

    def test_large_json_array_uses_bounded_streaming_reads(self):
        count = 12000
        stream = BoundedText(json.dumps([record(str(i)) for i in range(count)]))
        decoded = list(json_records(stream))
        self.assertEqual(len(decoded), count)
        self.assertEqual(decoded[0]['user_id'], '0')
        self.assertEqual(decoded[-1]['user_id'], str(count - 1))
        self.assertGreater(len(stream.reads), 100)
        self.assertTrue(all(size == 8192 for size in stream.reads))

    def test_json_record_crosses_8192_boundary_and_small_chunks(self):
        objects = [record('含引号"与反斜杠\\', padding='空格 ' * 3000), record('next')]
        serialized = json.dumps(objects, ensure_ascii=False)
        self.assertGreater(len(serialized), 8192)
        self.assertEqual(list(json_records(BoundedText(serialized))), objects)
        self.assertEqual(list(json_records(BoundedText(serialized, chunk=7))), objects)
        self.assertEqual(list(json_records(BoundedText(json.dumps(objects[1]), chunk=1))), [objects[1]])

    def test_empty_trailing_truncated_or_malformed_json_is_rejected(self):
        for content in ('', '  \n', '[]', '[ ]'):
            with self.subTest(empty=content), self.assertRaises(ValueError):
                self.adapt(content)
        valid = json.dumps(record())
        for content in ('[' + valid, '[' + valid + ',]', '[' + valid + ' ' + valid + ']',
                        '[' + valid + '] trailing', valid + valid, '[', '[null', '{"x":', '[{}]null'):
            with self.subTest(malformed=content[:80]), self.assertRaises(ValueError):
                self.adapt(content)

    def test_json_invalid_records_keep_exact_record_accounting(self):
        objects = [record(), None, [1, 2], False, 123, 'text', record('u2', unused='ignored')]
        text, report = self.adapt(json.dumps(objects))
        lines = text.splitlines()
        self.assertEqual(len(lines), len(objects))
        self.assertEqual(lines[1:6], ['INVALID_RECORD'] * 5)
        self.assertEqual(report['parsed_records'], len(objects))
        self.assertEqual(report['input_format'], 'json')
        self.assertIn('数据记录序号', report['record_basis'])
        self.assertEqual(report['coordinate_system'], 'WGS84')

    def test_json_nested_control_and_missing_fields_are_not_silently_dropped(self):
        nested = record('u1')
        nested['user_id'] = {'nested': 'value'}
        boolean = record('u2')
        boolean['latitude'] = True
        tabbed = record('u\t3')
        missing = record('u4')
        del missing['location_id']
        text, report = self.adapt(json.dumps([record(), nested, boolean, tabbed, missing]))
        lines = text.splitlines()
        self.assertEqual(lines[1:4], ['INVALID_RECORD'] * 3)
        self.assertTrue(lines[4].endswith('\t'))
        self.assertEqual(report['parsed_records'], 5)

    def test_duplicate_csv_and_json_fields_are_rejected(self):
        duplicated_header = ','.join(FIELDS) + ',user_id\n' + ','.join(record().values()) + ',u2\n'
        with self.assertRaises(ValueError):
            self.adapt(duplicated_header, 'fixture.csv')
        obj = json.dumps(record())
        duplicated_object = obj[:-1] + ',"user_id":"overwritten"}'
        for suffix, content in (('json', '[' + duplicated_object + ']'), ('jsonl', duplicated_object + '\n')):
            with self.subTest(format=suffix), self.assertRaises(ValueError):
                self.adapt(content, 'fixture.' + suffix)

    def test_csv_quotes_commas_multiline_and_wrong_width(self):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(FIELDS)
        writer.writerow(record('u,"quoted"').values())
        writer.writerow(record('u\nmultiline').values())
        writer.writerow(['too', 'few'])
        text, report = self.adapt(stream.getvalue(), 'fixture.csv')
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith('u,"quoted"\t'))
        self.assertEqual(lines[1:], ['INVALID_RECORD', 'INVALID_RECORD'])
        self.assertEqual(report['parsed_records'], 3)
        with self.assertRaises(csv.Error):
            self.adapt(','.join(FIELDS) + '\n"unterminated', 'fixture.csv')

    def test_jsonl_line_limit_and_invalid_utf8_are_rejected(self):
        with patch.dict('os.environ', {'CHECKIN_MAX_LINE_BYTES': '256'}):
            with self.assertRaises(ValueError):
                self.adapt(json.dumps(record(padding='x' * 300)) + '\n', 'fixture.jsonl')
        for suffix in ('json', 'jsonl', 'csv'):
            with self.subTest(format=suffix), self.assertRaises(UnicodeError):
                self.adapt(b'\xff\xfeinvalid UTF8\n', 'fixture.' + suffix)

    def test_multibyte_source_limits_are_bytes_not_character_counts(self):
        obj = record(padding='测' * 50)
        serialized = json.dumps(obj, ensure_ascii=False)
        limit = len(serialized) + 1
        self.assertGreater(len(serialized.encode('utf-8')), limit)
        for suffix, content in (('json', '[' + serialized + ']'), ('jsonl', serialized + '\n')):
            with self.subTest(format=suffix), patch.dict('os.environ', {'CHECKIN_MAX_LINE_BYTES': str(limit)}), self.assertRaises(ValueError):
                self.adapt(content, 'fixture.' + suffix)

    def test_mapping_validation_and_crs_rejection(self):
        valid = {field: 'source_' + field for field in FIELDS}
        self.assertEqual(import_config('x.csv', valid)['field_mapping'], valid)
        self.assertEqual(import_config('x.csv', json.dumps(valid))['field_mapping'], valid)
        invalid = [[], False, 0, '[]', 'false', '0', {'user_id': 'uid'},
                   {field: 'same' for field in FIELDS}, {**valid, 'unknown': 'x'}, {**valid, 'user_id': ''}]
        for mapping in invalid:
            with self.subTest(mapping=mapping), self.assertRaises(APIError):
                import_config('x.csv', mapping)
        with self.assertRaises(APIError):
            import_config('x.tsv', valid)
        for crs in ('GCJ02', 'BD09', 'EPSG:3857', '', None):
            with self.subTest(crs=crs), self.assertRaises(APIError):
                import_config('x.csv', coordinate_system=crs)
        with self.assertRaises(ValueError):
            resolve_mapping(['one', 'two'], valid)

    def test_auto_aliases_explicit_mapping_and_missing_source_fields(self):
        columns = ['UID', 'timestamp', 'LAT', 'Lng', 'venue_id']
        expected = dict(zip(FIELDS, columns))
        self.assertEqual(resolve_mapping(columns, {}), expected)
        values = dict(zip(columns, record().values()))
        text, report = self.adapt(json.dumps([values]), 'fixture.json')
        self.assertEqual(text.rstrip('\n').split('\t'), list(record().values()))
        self.assertEqual(report['field_mapping'], expected)
        chosen = dict(zip(FIELDS, ['person', 'at', 'y', 'x', 'place']))
        payload = ','.join(chosen.values()) + '\n' + ','.join(record().values()) + '\n'
        text, report = self.adapt(payload, 'fixture.csv', chosen)
        self.assertEqual(report['field_mapping'], chosen)
        self.assertTrue(text.startswith('u1\t'))
        with self.assertRaises(ValueError):
            self.adapt('user_id,latitude\nu1,30\n', 'fixture.csv')

    def test_preview_is_bounded_sample_and_tsv_keeps_original_source(self):
        objects = [record(str(i)) for i in range(8)]
        # A slice need not contain the closing bracket when five records exist.
        content = json.dumps(objects).encode()
        result = preview(content[:-10], import_config('fixture.json'))
        self.assertTrue(result['sample_only'])
        self.assertEqual(len(result['rows']), 5)
        self.assertEqual(result['rows'][0]['user_id'], '0')
        self.assertEqual(result['mapping_error'], None)
        raw = '\t'.join(record().values()) + '\n\n'
        text, report = self.adapt(raw, 'fixture.tsv')
        self.assertEqual(text, raw)
        self.assertEqual(report['input_format'], 'tsv')
        self.assertIn('物理行', report['record_basis'])


if __name__ == '__main__':
    unittest.main()
