"""Offline integration tests using a real bare Git remote (no network)."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import publish_generated as publisher


def command(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.repo = self.root / 'calculation'
        self.other = self.root / 'other'
        self.bundle = self.root / 'bundle'
        command('git', 'init', '--bare', '--initial-branch=main', str(self.remote))
        command('git', 'clone', str(self.remote), str(self.repo))
        for repo in [self.repo]:
            self.identity(repo)
        self.write(self.repo, 'data/odds_shadow/book/latest.json', '{"version":0}\n')
        self.write(self.repo, 'data/odds_shadow/history/old.json', '{}\n')
        self.write(self.repo, 'data/bwf/source.json', '{"source":0}\n')
        self.write(self.repo, 'README.md', 'initial\n')
        self.commit(self.repo)
        command('git', '-C', str(self.repo), 'push', 'origin', 'main')
        command('git', 'clone', str(self.remote), str(self.other))
        self.identity(self.other)

    def identity(self, repo):
        command('git', '-C', str(repo), 'config', 'user.name', 'Test')
        command('git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid')

    def write(self, repo, name, content):
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def commit(self, repo):
        command('git', '-C', str(repo), 'add', '-A')
        command('git', '-C', str(repo), 'commit', '-m', 'test')

    def advance(self, name='README.md'):
        self.write(self.other, name, '{"remote":true}\n')
        self.commit(self.other)
        command('git', '-C', str(self.other), 'push', 'origin', 'main')

    def prepare(self):
        self.write(self.repo, 'data/odds_shadow/book/latest.json', '{"version":1}\n')
        self.write(self.repo, 'data/odds_shadow/history/new.json', '{"new":true}\n')
        return publisher.prepare(self.repo, self.bundle, ['data/odds_shadow'], ['data/bwf'],
                                 ['data/odds_shadow/visual_evidence'])

    def remote_file(self, name):
        return command('git', '--git-dir', str(self.remote), 'show', 'main:' + name).decode()

    def test_publish_preserves_unrelated_remote_changes_and_calculation_checkout(self):
        self.write(self.repo, 'data/odds_shadow/visual_evidence/screenshot.png', 'excluded')
        self.write(self.repo, 'unrelated.txt', 'not an output')
        self.prepare()
        self.advance()
        before = command('git', '-C', str(self.repo), 'status', '--porcelain')
        head = command('git', '-C', str(self.repo), 'rev-parse', 'HEAD')
        self.assertEqual(publisher.publish(self.repo, self.bundle), 'PUBLISHED')
        self.assertEqual(self.remote_file('README.md'), '{"remote":true}\n')
        self.assertEqual(self.remote_file('data/odds_shadow/book/latest.json'), '{"version":1}\n')
        self.assertEqual(self.remote_file('data/odds_shadow/history/old.json'), '{}\n')
        names = command('git', '--git-dir', str(self.remote), 'ls-tree', '-r', '--name-only', 'main').decode()
        self.assertNotIn('screenshot.png', names)
        self.assertNotIn('unrelated.txt', names)
        self.assertEqual(command('git', '-C', str(self.repo), 'status', '--porcelain'), before)
        self.assertEqual(command('git', '-C', str(self.repo), 'rev-parse', 'HEAD'), head)

    def test_concurrent_latest_refuses_entire_bundle(self):
        self.prepare()
        self.advance('data/odds_shadow/book/latest.json')
        with self.assertRaisesRegex(publisher.PublicationError, 'PUBLICATION_DEFERRED'):
            publisher.publish(self.repo, self.bundle)
        self.assertEqual(self.remote_file('data/odds_shadow/book/latest.json'), '{"remote":true}\n')
        self.assertTrue((self.bundle / 'files/data/odds_shadow/history/new.json').exists())
        self.assertFalse(publisher.git(self.repo, 'ls-files', '-u').stdout)

    def test_changed_bwf_input_defers_model_publication(self):
        self.prepare()
        self.advance('data/bwf/source.json')
        with self.assertRaisesRegex(publisher.PublicationError, 'PUBLICATION_DEFERRED'):
            publisher.publish(self.repo, self.bundle)
        self.assertEqual(self.remote_file('data/odds_shadow/book/latest.json'), '{"version":0}\n')

    def test_push_race_retries_on_new_head_without_rebase(self):
        self.prepare()
        original = publisher.git
        pushes = []
        def race(repo, *args, **kwargs):
            if args[0] == 'push':
                pushes.append(1)
                if len(pushes) == 1:
                    self.advance()
            return original(repo, *args, **kwargs)
        with patch.object(publisher, 'git', side_effect=race):
            self.assertEqual(publisher.publish(self.repo, self.bundle), 'PUBLISHED')
        self.assertEqual(len(pushes), 2)
        self.assertEqual(self.remote_file('README.md'), '{"remote":true}\n')

    def test_relevant_push_race_is_deferred(self):
        self.prepare()
        original = publisher.git
        raced = False
        def race(repo, *args, **kwargs):
            nonlocal raced
            if args[0] == 'push' and not raced:
                raced = True
                self.advance('data/odds_shadow/book/latest.json')
            return original(repo, *args, **kwargs)
        with patch.object(publisher, 'git', side_effect=race):
            with self.assertRaisesRegex(publisher.PublicationError, 'PUBLICATION_DEFERRED'):
                publisher.publish(self.repo, self.bundle)
        self.assertEqual(self.remote_file('data/odds_shadow/book/latest.json'), '{"remote":true}\n')

    def test_rejected_push_stays_failure_and_bundle_survives(self):
        self.prepare()
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(publisher.PublicationError, 'Push rejected'):
            publisher.publish(self.repo, self.bundle)
        self.assertTrue((self.bundle / 'manifest.json').exists())
        self.assertEqual(self.remote_file('data/odds_shadow/book/latest.json'), '{"version":0}\n')
        self.assertEqual(len(publisher.git(self.repo, 'worktree', 'list').stdout.splitlines()), 1)

    def test_delete_only_selected_file(self):
        (self.repo / 'data/odds_shadow/history/old.json').unlink()
        publisher.prepare(self.repo, self.bundle, ['data/odds_shadow'])
        self.assertEqual(publisher.publish(self.repo, self.bundle), 'PUBLISHED')
        names = command('git', '--git-dir', str(self.remote), 'ls-tree', '-r', '--name-only', 'main').decode()
        self.assertNotIn('history/old.json', names)
        self.assertIn('book/latest.json', names)

    def test_no_changes_and_corrupt_bundle(self):
        publisher.prepare(self.repo, self.bundle, ['data/odds_shadow'])
        self.assertEqual(publisher.publish(self.repo, self.bundle), 'NO_CHANGES')
        second = self.root / 'second'
        self.write(self.repo, 'data/odds_shadow/book/latest.json', '{"new":1}')
        publisher.prepare(self.repo, second, ['data/odds_shadow'])
        (second / 'files/data/odds_shadow/book/latest.json').write_text('corrupted')
        with self.assertRaisesRegex(publisher.PublicationError, 'checksum'):
            publisher.publish(self.repo, second)


if __name__ == '__main__':
    unittest.main()
