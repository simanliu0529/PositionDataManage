"""Independent upstream consensus regressions; failures remain visible.

Three defects belong to the unchanged team cleaning module. expectedFailure
keeps the desired mathematical result rather than approving the wrong result.
An upstream fix will produce unexpectedSuccess and require explicit re-review.
"""
import csv
import tempfile
import unittest
from pathlib import Path

from cleaning.clean import clean


class CoordinateConsensus(unittest.TestCase):
    def cleaned(self, coordinates):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'raw.tsv'
            target = Path(temporary) / 'clean.csv'
            # Distinct users keep deduplication and trajectory rejection out of
            # these cases so that the expected coordinate mode is independent.
            source.write_text(''.join(
                f'user-{index}\t2010-01-01T00:00:00Z\t{lat}\t{lon}\tplace\n'
                for index, (lat, lon) in enumerate(coordinates)
            ), encoding='utf-8')
            stats = clean(source, target)
            with target.open(encoding='utf-8', newline='') as stream:
                result = [(float(row['latitude']), float(row['longitude']))
                          for row in csv.DictReader(stream)]
            return result, stats

    @unittest.expectedFailure
    def test_first_coordinate_remains_majority_and_minorities_are_repaired(self):
        """Upstream: a first-coordinate majority is not applied to minorities."""
        result, stats = self.cleaned([(30, 114), (30, 114), (31, 115)])
        self.assertEqual(result, [(30, 114)] * 3)
        self.assertEqual(stats['coord_conflict_locations'], 1)
        self.assertEqual(stats['coord_repair'], 1)

    @unittest.expectedFailure
    def test_later_occurrences_of_initial_coordinate_contribute_to_mode(self):
        """Upstream: later votes for the first coordinate are omitted."""
        result, stats = self.cleaned([(30, 114), (31, 115), (31, 115), (30, 114), (30, 114)])
        self.assertEqual(result, [(30, 114)] * 5)
        self.assertEqual(stats['coord_repair'], 2)

    @unittest.expectedFailure
    def test_new_majority_counts_only_records_that_change(self):
        """Upstream: coord_repair includes unchanged rows at the winning POI."""
        result, stats = self.cleaned([(30, 114), (31, 115), (31, 115)])
        self.assertEqual(result, [(31, 115)] * 3)
        self.assertEqual(stats['coord_repair'], 1)

    def test_consistent_locations_do_not_report_repairs(self):
        result, stats = self.cleaned([(30, 114)] * 3)
        self.assertEqual(result, [(30, 114)] * 3)
        self.assertEqual(stats.get('coord_repair', 0), 0)


if __name__ == '__main__':
    unittest.main()
