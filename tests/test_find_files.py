"""Verify bounded filename search and containment without model inference.

Core owns traversal and its failure contract; application suites separately
check default registry exposure and Chat Squid's fresh/follow-up permissions.
"""

import importlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from squid_core.session import Session
from squid_core.tools.builtins import TOOLS
from squid_core.tools.executor import ToolExecutor


search = importlib.import_module("squid_core.tools.builtins.find_files")


def touch(root, name):
    """Create nested fixture paths without making their ordering significant."""
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    return str(target)


@pytest.mark.parametrize(('pattern', 'names'), [
    ('README.md', ['README.md', 'docs/README.md']),
    ('*.py', ['src/app.py', 'src/deep/test.py']),
    ('*session*', ['src/session.json', 'session.txt']),
    ('app.??', ['src/app.py']),
    ('readme.md', []),
    ('*.PY', ['src/UPPER.PY']),
    ('missing*', []),
])
def test_exact_wildcard_recursive_case_sensitive_matching(tmp_path, pattern, names):
    for name in ['README.md', 'docs/README.md', 'src/app.py', 'src/deep/test.py',
                 'src/session.json', 'session.txt', 'src/UPPER.PY']:
        touch(tmp_path, name)
    result = search.execute(tmp_path, pattern)
    assert result == {
        'ok': True, 'tool': 'find_files', 'path': str(tmp_path), 'pattern': pattern,
        'files': sorted(str(tmp_path / name) for name in names),
        'returned_count': len(names), 'truncated': False, 'truncation_reason': None,
    }


def test_filename_only_and_selected_subdirectory(tmp_path):
    touch(tmp_path, 'needle-dir/ordinary.txt')
    target = touch(tmp_path, 'sub/needle.txt')
    touch(tmp_path, 'needle.txt')
    result = search.execute(tmp_path, '*needle*', str(tmp_path / 'sub'))
    assert result['files'] == [target]
    assert search.execute(tmp_path, '*needle*')['returned_count'] == 2
    assert search.execute(tmp_path, '*', str(tmp_path / 'needle-dir'))['files'] == [
        str(tmp_path / 'needle-dir/ordinary.txt')]


@pytest.mark.parametrize('pattern', [None, '', 1, True, [], {}, 'a/b', 'a\\b',
                                    'bad\0name', '[ab]*', 'x]', '.', '..', 'x' * 257])
def test_invalid_patterns(tmp_path, pattern):
    result = search.execute(tmp_path, pattern)
    assert not result['ok'] and 'pattern' in result['error']


@pytest.mark.parametrize('path', ['', 'relative', 1, True, [], {}, 'bad\0path'])
def test_invalid_roots(tmp_path, path):
    assert not search.execute(tmp_path, '*', path)['ok']


def test_missing_file_and_empty_roots(tmp_path):
    assert search.execute(tmp_path, '*', str(tmp_path / 'absent'))['error'] == 'Search directory does not exist'
    file = touch(tmp_path, 'file')
    assert search.execute(tmp_path, '*', file)['error'] == 'Requested path is not a directory'
    (tmp_path / 'empty').mkdir()
    result = search.execute(tmp_path, '*', str(tmp_path / 'empty'))
    assert result['files'] == [] and not result['truncated']


def test_workspace_boundary_cannot_be_overridden(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    touch(tmp_path, 'external-secret.txt')
    executor = ToolExecutor(workspace, Session(workspace), allow_outside_workspace=True)
    for target in [tmp_path, workspace / '..']:
        result = executor.execute({'tool': 'find_files', 'args': {'pattern': '*', 'path': str(target)},
                                   'outside_workspace_approved': True})
        assert not result['ok'] and 'outside' in result['error']
        assert 'external-secret' not in json.dumps(result)
    assert TOOLS['find_files']['execute'] is search.execute
    assert search.tool_definition()['function']['parameters']['required'] == ['pattern']


def make_directory_link(link, target, kind):
    """Use unprivileged junctions on Windows, with separate symlink coverage."""
    if kind == 'junction':
        if os.name != 'nt':
            pytest.skip('Windows junction test')
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                       check=True, capture_output=True)
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip('Directory symlinks unavailable on this system')


@pytest.mark.parametrize('kind', ['junction', 'symlink'])
def test_links_and_linked_roots_are_not_followed(tmp_path, kind):
    workspace = tmp_path / 'workspace'
    outside = tmp_path / 'outside'
    workspace.mkdir()
    touch(outside, 'nested/secret.txt')
    touch(workspace, 'real/sub/known.txt')
    make_directory_link(workspace / 'external', outside, kind)
    make_directory_link(workspace / 'internal', workspace / 'real', kind)
    make_directory_link(workspace / 'real' / 'cycle', workspace, kind)
    result = search.execute(workspace, '*')
    assert result['files'] == [str(workspace / 'real/sub/known.txt')]
    assert not result['truncated']
    for target in [workspace / 'external', workspace / 'external/nested',
                   workspace / 'internal', workspace / 'internal/sub']:
        assert not search.execute(workspace, '*', str(target))['ok']
    assert 'secret' not in json.dumps(result)


def test_file_symlinks_are_not_returned(tmp_path):
    touch(tmp_path, 'real.txt')
    try:
        (tmp_path / 'link.txt').symlink_to(tmp_path / 'real.txt')
        (tmp_path / 'broken.txt').symlink_to(tmp_path / 'absent.txt')
    except OSError:
        pytest.skip('File symlinks unavailable on this system')
    assert search.execute(tmp_path, '*.txt')['files'] == [str(tmp_path / 'real.txt')]


def test_excluded_directories_are_pruned_without_gitignore_interpretation(tmp_path):
    for name in search.EXCLUDED_DIRECTORIES:
        touch(tmp_path, f'{name}/nested/hidden.txt')
    target = touch(tmp_path, 'src/keep.txt')
    (tmp_path / '.gitignore').write_text('src/\n', encoding='utf-8')
    result = search.execute(tmp_path, '*.txt')
    assert result['files'] == [target] and not result['truncated']


def test_deterministic_traversal_and_result_limit(tmp_path, monkeypatch):
    for name in ['z/z.txt', 'a/c.txt', 'a/b.txt', 'root.txt']:
        touch(tmp_path, name)
    monkeypatch.setattr(search, 'MAX_RESULTS', 2)
    first = search.execute(tmp_path, '*.txt')
    assert first == search.execute(tmp_path, '*.txt')
    assert first['files'] == sorted([str(tmp_path / 'root.txt'), str(tmp_path / 'a/b.txt')])
    assert first['truncated'] and first['truncation_reason'] == 'result_limit'
    assert first['returned_count'] == 2


@pytest.mark.parametrize('pattern', ['*', 'no-match'])
def test_entry_budget_bounds_wide_zero_match_searches(tmp_path, monkeypatch, pattern):
    for index in range(10):
        touch(tmp_path, f'{index}.txt')
    monkeypatch.setattr(search, 'MAX_VISITED_ENTRIES', 3)
    original = search.os.scandir
    yielded = []

    class CountingScan:
        def __enter__(self):
            self.scan = original(tmp_path)
            return self

        def __iter__(self):
            for item in self.scan:
                yielded.append(item.name)
                yield item

        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(search.os, 'scandir', lambda _path: CountingScan())
    result = search.execute(tmp_path, pattern)
    assert len(yielded) == 4  # One lookahead detects an incomplete directory.
    assert result['returned_count'] <= 3
    assert result['truncated'] and result['truncation_reason'] == 'entry_limit'


@pytest.mark.parametrize('pattern', ['*', 'no-match'])
def test_directory_budget_bounds_deep_zero_match_searches(tmp_path, monkeypatch, pattern):
    touch(tmp_path, 'a/b/c/last.txt')
    monkeypatch.setattr(search, 'MAX_DIRECTORIES', 2)
    result = search.execute(tmp_path, pattern)
    assert result['files'] == []
    assert result['truncated'] and result['truncation_reason'] == 'directory_limit'


def test_response_limit_includes_metadata(tmp_path, monkeypatch):
    for index in range(20):
        touch(tmp_path, f'{index:02}-' + '\u00e9' * 60 + '.txt')
    monkeypatch.setattr(search, 'MAX_RESPONSE_BYTES', 1500)
    result = search.execute(tmp_path, '*')
    assert result['ok'] and result['files'] == sorted(result['files'])
    assert 0 < result['returned_count'] < 20
    assert result['returned_count'] == len(result['files'])
    assert result['truncated'] and result['truncation_reason'] == 'response_size_limit'
    assert len(json.dumps(result).encode('utf-8')) <= 1500


def test_inaccessible_root_and_partial_descendant_errors(tmp_path, monkeypatch):
    target = touch(tmp_path, 'a-good/keep.txt')
    touch(tmp_path, 'z-blocked/hidden.txt')
    original = search.os.scandir

    def scan(path):
        if Path(path).name == 'z-blocked':
            raise PermissionError('unbounded external-secret-path' * 1000)
        return original(path)

    monkeypatch.setattr(search.os, 'scandir', scan)
    result = search.execute(tmp_path, '*')
    assert result['ok'] and result['files'] == [target]
    assert result['truncated'] and result['truncation_reason'] == 'traversal_error'
    root_error = search.execute(tmp_path, '*', str(tmp_path / 'z-blocked'))
    assert not root_error['ok'] and 'error' in root_error
    assert 'external-secret' not in json.dumps([result, root_error])


def test_partial_scandir_iterator_failure_retains_collected_prefix(tmp_path, monkeypatch):
    target = touch(tmp_path, 'keep.txt')
    original = search.os.scandir

    class FailingScan:
        def __enter__(self):
            self.scan = original(tmp_path)
            return self

        def __iter__(self):
            yield next(self.scan)
            raise OSError('external-secret')

        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(search.os, 'scandir', lambda _path: FailingScan())
    result = search.execute(tmp_path, '*')
    assert result['files'] == [target] and result['truncation_reason'] == 'traversal_error'


def test_descendant_containment_is_rechecked_during_traversal(tmp_path, monkeypatch):
    target = touch(tmp_path, 'keep.txt')
    touch(tmp_path, 'changed/secret.txt')
    original = search.is_inside_workspace
    checks = []

    def contains(path, workspace):
        checks.append(Path(path))
        return Path(path).name != 'changed' and original(path, workspace)

    monkeypatch.setattr(search, 'is_inside_workspace', contains)
    result = search.execute(tmp_path, '*')
    assert tmp_path / 'changed' in checks
    assert result['files'] == [target] and result['truncation_reason'] == 'traversal_error'
