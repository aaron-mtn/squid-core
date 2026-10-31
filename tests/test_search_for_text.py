"""Test Core's literal text search, streaming budgets, and filesystem boundary.

Inference is intentionally absent; application tests own default registry and
fresh/follow-up Skill exposure. Existing exploration tools stay unchanged.
"""

import importlib
import json
import os
from pathlib import Path
import stat

import pytest

from squid_core.session import Session
from squid_core.tools.builtins import TOOLS
from squid_core.tools.executor import ToolExecutor
from test_find_files import make_directory_link


search = importlib.import_module('squid_core.tools.builtins.search_for_text')


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode('utf-8') if isinstance(content, str) else content)
    return str(path)


@pytest.mark.parametrize('ending', ['\n', '\r\n'])
@pytest.mark.parametrize('literal', ['a[]*.', 'compact_history', '\u03bb\u732b'])
def test_literal_matches_unicode_bom_and_exact_line_numbers(tmp_path, ending, literal):
    path = write(tmp_path, 'src/code.py', '\ufefffirst' + ending + literal + ending + literal)
    result = search.execute(tmp_path, literal)
    assert result == {
        'ok': True, 'tool': 'search_for_text', 'path': str(tmp_path), 'text': literal,
        'matches': [{'path': path, 'line_number': line, 'text': literal} for line in (2, 3)],
        'returned_count': 2, 'truncated': False, 'truncation_reason': None,
    }
    assert search.execute(tmp_path, 'first')['matches'][0]['text'] == 'first'


def test_case_and_filename_filter_are_consistent(tmp_path):
    write(tmp_path, 'a.py', 'Hello\nhello\nStra\u00dfe\n')
    write(tmp_path, 'b.PY', 'hello')
    write(tmp_path, 'c.md', 'hello')
    assert search.execute(tmp_path, 'hello', file_pattern='*.py')['returned_count'] == 1
    assert search.execute(tmp_path, 'HELLO', file_pattern='*.py', case_sensitive=False)['returned_count'] == 2
    assert search.execute(tmp_path, 'STRASSE', case_sensitive=False)['returned_count'] == 1
    assert search.execute(tmp_path, 'h.llo')['matches'] == []


def test_empty_files_no_matches_and_subdirectory_root(tmp_path):
    write(tmp_path, 'empty', '')
    write(tmp_path, 'other.txt', 'wanted')
    write(tmp_path, 'sub/file', 'unrelated')
    result = search.execute(tmp_path, 'wanted', str(tmp_path / 'sub'))
    assert result['ok'] and result['matches'] == [] and not result['truncated']
    assert search.execute(tmp_path, 'no match')['matches'] == []


@pytest.mark.parametrize('text', [None, '', 1, True, [], {}, 'a\nb', 'a\rb', 'a\0b', 'a' * 1025])
def test_invalid_text(tmp_path, text):
    assert not search.execute(tmp_path, text)['ok']


@pytest.mark.parametrize('kwargs', [
    {'path': ''}, {'path': 'relative'}, {'path': 1}, {'path': []}, {'path': 'bad\0path'},
    {'file_pattern': ''}, {'file_pattern': 1}, {'file_pattern': []}, {'file_pattern': '[ab]'},
    {'file_pattern': 'a/b'}, {'file_pattern': 'a\\b'}, {'file_pattern': 'a' * 257},
    {'case_sensitive': 'false'}, {'case_sensitive': 0}, {'case_sensitive': None},
])
def test_invalid_options(tmp_path, kwargs):
    assert not search.execute(tmp_path, 'text', **kwargs)['ok']


def test_invalid_missing_roots_and_registration(tmp_path):
    file = write(tmp_path, 'file', 'text')
    assert not search.execute(tmp_path, 'text', file)['ok']
    assert search.execute(tmp_path, 'text', str(tmp_path / 'absent'))['error'] == 'Search directory does not exist'
    assert TOOLS['search_for_text']['execute'] is search.execute
    schema = search.tool_definition()['function']['parameters']
    assert schema['required'] == ['text'] and schema['additionalProperties'] is False


@pytest.mark.parametrize('bad', [b'\0', b'\x01', b'\xff', b'\xc3'])
def test_binary_and_undecodable_files_discard_earlier_matches(tmp_path, bad):
    write(tmp_path, 'bad.dat', b'needle\n' + b'x' * 100 + bad)
    good = write(tmp_path, 'good.txt', 'needle')
    result = search.execute(tmp_path, 'needle')
    assert result['matches'] == [{'path': good, 'line_number': 1, 'text': 'needle'}]
    assert not result['truncated']


def test_file_and_long_line_limits_preserve_later_line_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(search, 'MAX_FILE_BYTES', 500)
    monkeypatch.setattr(search, 'MAX_LINE_BYTES', 20)
    write(tmp_path, 'big', 'needle' * 100)
    path = write(tmp_path, 'long', '\u00e9' * 30 + 'needle\nnext\nneedle\n')
    result = search.execute(tmp_path, 'needle')
    assert result['matches'] == [{'path': path, 'line_number': 3, 'text': 'needle'}]
    assert result['truncation_reason'] == 'file_size_limit, line_length_limit'


def test_bounded_excerpt_contains_hit_near_end_of_long_valid_line(tmp_path):
    write(tmp_path, 'text', '\u00df' * 600 + 'NEEDLE' + 'tail' * 200)
    result = search.execute(tmp_path, 'needle', case_sensitive=False)
    excerpt = result['matches'][0]['text']
    assert 'NEEDLE' in excerpt and len(excerpt) <= search.MAX_EXCERPT_CHARS


def test_deterministic_order_and_one_result_per_matching_line(tmp_path):
    for name in ['z.txt', 'a/deep.txt', 'a.txt']:
        write(tmp_path, name, 'hit hit\nno\nhit')
    result = search.execute(tmp_path, 'hit')
    assert [(hit['path'], hit['line_number']) for hit in result['matches']] == sorted(
        (str(tmp_path / name), line) for name in ['z.txt', 'a/deep.txt', 'a.txt'] for line in (1, 3))
    assert result == search.execute(tmp_path, 'hit')


def test_exclusions_and_special_files_are_skipped(tmp_path, monkeypatch):
    for name in search.EXCLUDED_DIRECTORIES:
        write(tmp_path, f'{name}/file', 'hit')
    write(tmp_path, 'special', 'hit')
    original = search.os.fstat
    monkeypatch.setattr(search.os, 'fstat', lambda fd: type('Info', (), {'st_mode': stat.S_IFIFO})())
    assert search.execute(tmp_path, 'hit')['matches'] == []
    monkeypatch.setattr(search.os, 'fstat', original)
    if os.name != 'nt':
        os.mkfifo(tmp_path / 'pipe')
    assert search.execute(tmp_path, 'hit')['returned_count'] == 1


def test_workspace_boundary_cannot_be_approved_away(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    write(tmp_path, 'external-secret', 'hit')
    executor = ToolExecutor(workspace, Session(workspace), allow_outside_workspace=True)
    for root in [tmp_path, workspace / '..']:
        result = executor.execute({'tool': 'search_for_text', 'args': {'text': 'hit', 'path': str(root)},
                                   'outside_workspace_approved': True})
        assert not result['ok'] and 'outside' in result['error']
        assert 'external-secret' not in json.dumps(result)


@pytest.mark.parametrize('kind', ['junction', 'symlink'])
def test_links_junctions_and_linked_roots(tmp_path, kind):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    write(tmp_path, 'outside/sub/secret', 'hit')
    good = write(workspace, 'real/sub/good', 'hit')
    make_directory_link(workspace / 'external', tmp_path / 'outside', kind)
    make_directory_link(workspace / 'internal', workspace / 'real', kind)
    make_directory_link(workspace / 'real/cycle', workspace, kind)
    result = search.execute(workspace, 'hit')
    assert result['matches'] == [{'path': good, 'line_number': 1, 'text': 'hit'}]
    assert not result['truncated']
    for target in ['external', 'external/sub', 'internal', 'internal/sub']:
        assert not search.execute(workspace, 'hit', str(workspace / target))['ok']


def test_file_symlinks_are_skipped(tmp_path):
    write(tmp_path, 'real', 'hit')
    try:
        (tmp_path / 'link').symlink_to(tmp_path / 'real')
    except OSError:
        pytest.skip('File symlink privileges unavailable')
    assert search.execute(tmp_path, 'hit')['returned_count'] == 1


def test_partial_access_errors_and_inaccessible_root(tmp_path, monkeypatch):
    good = write(tmp_path, 'good', 'hit')
    write(tmp_path, 'blocked/file', 'hit')
    write(tmp_path, 'unreadable', 'hit')
    original_scan = search.os.scandir
    original_open = Path.open

    def scan(path):
        if Path(path).name == 'blocked':
            raise PermissionError('external-secret' * 1000)
        return original_scan(path)

    def open_file(path, *args, **kwargs):
        if path.name == 'unreadable':
            raise PermissionError('external-secret' * 1000)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(search.os, 'scandir', scan)
    monkeypatch.setattr(Path, 'open', open_file)
    result = search.execute(tmp_path, 'hit')
    assert result['matches'] == [{'path': good, 'line_number': 1, 'text': 'hit'}]
    assert result['truncated'] and result['truncation_reason'] == 'traversal_error'
    root_error = search.execute(tmp_path, 'hit', str(tmp_path / 'blocked'))
    assert not root_error['ok'] and 'external-secret' not in json.dumps([result, root_error])


@pytest.mark.parametrize('text', ['hit', 'no-match'])
def test_entry_directory_and_byte_budgets_even_without_matches(tmp_path, monkeypatch, text):
    for index in range(10):
        write(tmp_path, f'{index}/file', 'hit' * 100)
    monkeypatch.setattr(search, 'MAX_VISITED_ENTRIES', 3)
    assert 'entry_limit' in search.execute(tmp_path, text)['truncation_reason']
    monkeypatch.setattr(search, 'MAX_VISITED_ENTRIES', 100)
    monkeypatch.setattr(search, 'MAX_DIRECTORIES', 2)
    assert 'directory_limit' in search.execute(tmp_path, text)['truncation_reason']
    monkeypatch.setattr(search, 'MAX_DIRECTORIES', 100)
    monkeypatch.setattr(search, 'MAX_TOTAL_BYTES', 30)
    result = search.execute(tmp_path, text)
    assert result['matches'] == [] and result['truncation_reason'] == 'byte_limit'


def test_stream_reads_and_total_bytes_are_actually_bounded(tmp_path, monkeypatch):
    write(tmp_path, 'file', 'x' * 1000)
    monkeypatch.setattr(search, 'MAX_TOTAL_BYTES', 100)
    monkeypatch.setattr(search, 'MAX_LINE_BYTES', 20)
    original = Path.open
    reads = []

    class Stream:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def readline(self, limit):
            assert 0 < limit <= 21
            raw = self.file.readline(limit)
            reads.append(len(raw))
            return raw

    monkeypatch.setattr(Path, 'open', lambda path, *a, **kw: Stream(original(path, *a, **kw)))
    assert search.execute(tmp_path, 'missing')['truncation_reason'] == 'byte_limit'
    assert sum(reads) == 100


def test_match_and_serialized_response_limits(tmp_path, monkeypatch):
    write(tmp_path, 'file', ('hit ' + '\u00e9' * 200 + '\n') * 20)
    monkeypatch.setattr(search, 'MAX_RESULTS', 3)
    result = search.execute(tmp_path, 'hit')
    assert result['returned_count'] == 3 and result['truncation_reason'] == 'match_limit'
    monkeypatch.setattr(search, 'MAX_RESPONSE_BYTES', 1500)
    result = search.execute(tmp_path, 'hit')
    assert result['returned_count'] == len(result['matches']) < 3
    assert 'response_size_limit' in result['truncation_reason']
    assert len(json.dumps(result).encode('utf-8')) <= 1500


def test_descendant_containment_rechecked(tmp_path, monkeypatch):
    write(tmp_path, 'changed/secret', 'hit')
    original = search.is_inside_workspace
    monkeypatch.setattr(search, 'is_inside_workspace', lambda path, root:
                        Path(path).name != 'changed' and original(path, root))
    result = search.execute(tmp_path, 'hit')
    assert result['matches'] == [] and result['truncation_reason'] == 'traversal_error'


def test_byte_exhaustion_preserves_validated_files_only(tmp_path, monkeypatch):
    good = write(tmp_path, 'a.txt', 'hit\n')
    write(tmp_path, 'b.txt', 'hit\n' + 'x' * 100)
    monkeypatch.setattr(search, 'MAX_TOTAL_BYTES', 20)
    result = search.execute(tmp_path, 'hit')
    assert result['matches'] == [{'path': good, 'line_number': 1, 'text': 'hit'}]
    assert result['truncation_reason'] == 'byte_limit'


def test_iterator_failure_retains_searchable_directory_prefix(tmp_path, monkeypatch):
    good = write(tmp_path, 'good', 'hit')
    original = search.os.scandir

    class PartialScan:
        def __enter__(self):
            self.scan = original(tmp_path)
            return self

        def __iter__(self):
            yield next(self.scan)
            raise OSError('external-secret')

        def __exit__(self, *args):
            self.scan.close()

    monkeypatch.setattr(search.os, 'scandir', lambda _path: PartialScan())
    result = search.execute(tmp_path, 'hit')
    assert result['matches'] == [{'path': good, 'line_number': 1, 'text': 'hit'}]
    assert result['truncation_reason'] == 'traversal_error'
