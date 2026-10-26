"""Publish captured generated files without rebasing or changing the calculation checkout.

prepare: capture only changed allowlisted files in an external bundle.
publish: create one commit on current main, refusing changed inputs/outputs.
Upload the bundle before publish so rejected publications remain inspectable.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile


class PublicationError(RuntimeError):
    pass


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def paths_from(raw):
    return [p.decode('utf-8') for p in raw.split(b'\0') if p]


def safe_path(value):
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] != 'data':
        raise PublicationError(f'Unsafe generated path: {value}')
    return value


def prepare(repo, bundle, paths, guards=(), excludes=()):
    repo, bundle = Path(repo).resolve(), Path(bundle).resolve()
    if bundle == repo or repo in bundle.parents:
        raise PublicationError('Bundle must be outside the calculation checkout')
    for p in (*paths, *guards, *excludes):
        safe_path(p)
    if git(repo, 'ls-files', '-u').stdout:
        raise PublicationError('Calculation checkout has unresolved files')
    # No local commits are made by the workflow before preparing this bundle.
    base = git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()
    changed = set(paths_from(git(repo, 'diff', '--name-only', '-z', base, '--', *paths).stdout))
    changed.update(paths_from(git(repo, 'ls-files', '--others', '--exclude-standard', '-z', '--', *paths).stdout))
    bundle.mkdir(parents=True, exist_ok=False)
    records = []
    for name in sorted(changed):
        safe_path(name)
        if any(name == e or name.startswith(e.rstrip('/') + '/') for e in excludes):
            continue
        source = repo / name
        if source.is_symlink() or any(p.is_symlink() for p in source.parents if p != repo):
            raise PublicationError(f'Symlink not permitted: {name}')
        record = {'path': name, 'deleted': not source.exists()}
        if source.exists():
            content = source.read_bytes()
            dest = bundle / 'files' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            record['sha256'] = hashlib.sha256(content).hexdigest()
        records.append(record)
    manifest = {'base': base, 'guards': sorted(set(paths) | set(guards)), 'files': records,
                'run_id': os.environ.get('GITHUB_RUN_ID', 'local'),
                'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT', '1')}
    (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def publish(repo, bundle, attempts=3):
    repo, bundle = Path(repo).resolve(), Path(bundle).resolve()
    manifest = json.loads((bundle / 'manifest.json').read_text())
    records = manifest['files']
    for p in manifest['guards']:
        safe_path(p)
    for record in records:
        name = safe_path(record['path'])
        if not record['deleted']:
            content = (bundle / 'files' / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != record['sha256']:
                raise PublicationError(f'Bundle checksum mismatch: {name}')
    if not records:
        return 'NO_CHANGES'
    for attempt in range(attempts):
        git(repo, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
        head = git(repo, 'rev-parse', 'refs/remotes/origin/main').stdout.decode().strip()
        # Refuse the whole bundle: never mix old model, new prices and old summaries.
        advanced = git(repo, 'diff', '--name-only', manifest['base'], head,
                       '--', *manifest['guards']).stdout.decode().strip()
        if advanced:
            raise PublicationError('PUBLICATION_DEFERRED: inputs/outputs changed on main; '
                                   'start a fresh run. Paths:\n' + advanced)
        with tempfile.TemporaryDirectory(prefix='generated-publish-') as tmp:
            work = Path(tmp) / 'worktree'
            git(repo, 'worktree', 'add', '--detach', str(work), head)
            try:
                git(work, 'config', 'user.name', 'github-actions[bot]')
                git(work, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
                for record in records:
                    target = work / record['path']
                    if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != work):
                        raise PublicationError('Symlink in publication tree')
                    if record['deleted']:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes((bundle / 'files' / record['path']).read_bytes())
                    git(work, 'add', '-A', '--', record['path'])
                if git(work, 'diff', '--cached', '--quiet', check=False).returncode == 0:
                    return 'NO_CHANGES'
                git(work, 'commit', '-m', 'Publish generated research outputs '
                    f"({manifest['run_id']}/{manifest['run_attempt']})")
                pushed = git(work, 'push', 'origin', 'HEAD:refs/heads/main', check=False)
                if pushed.returncode == 0:
                    return 'PUBLISHED'
                # Retry only when main actually advanced. Auth/protection failures
                # must remain failures, never be mistaken for a successful publish.
                git(repo, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
                now = git(repo, 'rev-parse', 'refs/remotes/origin/main').stdout.decode().strip()
                if now == head:
                    raise PublicationError('Push rejected: ' + pushed.stderr.decode(errors='replace'))
            finally:
                git(repo, 'worktree', 'remove', '--force', str(work))
    raise PublicationError(f'PUBLICATION_FAILED: main advanced during {attempts} attempts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'publish'])
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--paths', nargs='+', default=[])
    parser.add_argument('--guard', nargs='*', default=[])
    parser.add_argument('--exclude', nargs='*', default=[])
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            if not args.paths:
                parser.error('prepare requires --paths')
            manifest = prepare(Path.cwd(), args.bundle, args.paths, args.guard, args.exclude)
            print(f"Captured {len(manifest['files'])} generated files")
        else:
            print(publish(Path.cwd(), args.bundle))
    except (PublicationError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode(errors='replace') if exc.stderr else str(exc)
        else:
            detail = str(exc)
        print('PUBLICATION_FAILED: ' + detail)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as output:
                output.write('\n### Publication failed\nResults remain in the run artifact.\n')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
