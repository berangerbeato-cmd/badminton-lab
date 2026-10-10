"""Offline tests for immutable shadow ledger. No network or bookmaker accounts."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from v32_shadow_ledger import FILES, archive


class ShadowLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'ledger'
        sample = {
            'model': {'policy': 'MODEL_ONLY_NO_BET_NO_T0_REWRITE'},
            'comparison': {'policy': 'RESEARCH_ONLY_NO_BET_NO_T0', 'generated_at_utc': '2026-10-10T13:00:00+00:00',
                           'comparison_count': 1, 'results': [{'decision': 'NO_BET'}],
                           'source_audit': {'netbet_quotes': {'status': 'FRESH_SNAPSHOT'}}},
            'quality': {'policy': 'RESEARCH_ONLY_NO_BET',
                        'comparison_generated_at_utc': '2026-10-10T13:00:00+00:00',
                        'comparison_count': 1, 'rows': [{'decision': 'NO_BET'}]},
        }
        for key in ('model', 'comparison', 'quality', 'upcoming'):
            path = self.root / FILES[key]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('a,b\n1,2\n' if key == 'upcoming' else json.dumps(sample[key]))

    def test_creates_auditable_snapshot(self):
        manifest = archive(self.root, self.out, '12345')
        self.assertEqual(manifest['policy'], 'SHADOW_ONLY_NO_BET')
        self.assertEqual(manifest['comparison_count'], 1)
        self.assertEqual(manifest['source_audit']['netbet_quotes']['status'], 'FRESH_SNAPSHOT')
        for key, entry in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.root / entry['source']).read_bytes()).hexdigest(), entry['sha256'])
        self.assertTrue((self.out / '12345' / 'manifest.json').exists())

    def test_existing_run_cannot_be_overwritten(self):
        archive(self.root, self.out, '12345')
        with self.assertRaises(FileExistsError):
            archive(self.root, self.out, '12345')

    def test_rejects_mismatched_quality_snapshot(self):
        path = self.root / FILES['quality']
        obj = json.loads(path.read_text())
        obj['comparison_generated_at_utc'] = '2026-10-09T13:00:00+00:00'
        path.write_text(json.dumps(obj))
        with self.assertRaises(ValueError):
            archive(self.root, self.out, '12345')
        self.assertFalse((self.out / '12345').exists())

    def test_rejects_any_non_no_bet_decision(self):
        path = self.root / FILES['comparison']
        obj = json.loads(path.read_text())
        obj['results'][0]['decision'] = 'BET'
        path.write_text(json.dumps(obj))
        with self.assertRaises(ValueError):
            archive(self.root, self.out, '12345')

    def test_rejects_unsafe_run_id(self):
        with self.assertRaises(ValueError):
            archive(self.root, self.out, '../123')


if __name__ == '__main__':
    unittest.main()
