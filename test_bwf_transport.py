import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from bwf_sync import api_get


class TransportTests(unittest.TestCase):
    def test_timeout_recovers_once(self):
        with patch('bwf_sync.urllib.request.urlopen', side_effect=[TimeoutError(), io.StringIO('{"ok": true}')]) as get, patch('bwf_sync.time.sleep') as sleep:
            self.assertEqual(api_get('/test', {}, 15), {'ok': True})
            self.assertEqual([c.kwargs['timeout'] for c in get.call_args_list], [15, 30])
            sleep.assert_called_once_with(3)

    def test_exhausted_timeout_remains_failure(self):
        with patch('bwf_sync.urllib.request.urlopen', side_effect=TimeoutError()) as get, patch('bwf_sync.time.sleep'):
            with self.assertRaises(TimeoutError):
                api_get('/test', {}, 15)
            self.assertEqual(get.call_count, 2)

    def test_denial_and_invalid_json_are_not_retried(self):
        for response in [HTTPError('https://example.org', 403, 'denied', {}, None), io.StringIO('invalid')]:
            with self.subTest(response=response), patch('bwf_sync.urllib.request.urlopen', side_effect=[response]) as get, patch('bwf_sync.time.sleep') as sleep:
                with self.assertRaises((HTTPError, ValueError)):
                    api_get('/test', {}, 15)
                self.assertEqual(get.call_count, 1)
                sleep.assert_not_called()
