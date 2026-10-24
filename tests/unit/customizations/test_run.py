# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import json
import os

import pytest

from awscli.customizations.exceptions import ParamValidationError
from awscli.customizations.run import (
    RunCommand,
    SharedHTTPSessions,
    _check_shell_syntax,
    parse_command_line,
    parse_commands_file,
)

CLI_JSON = os.path.join(
    os.path.dirname(os.path.abspath(__import__('awscli').__file__)),
    'data',
    'cli.json',
)


@pytest.fixture(scope='module')
def cli_options():
    with open(CLI_JSON) as f:
        return json.load(f)['options']


class TestParseCommandLine:
    @pytest.mark.parametrize(
        'line',
        ['', '   ', '\t', '# a comment', '   # indented comment', '#'],
    )
    def test_empty_and_comment_lines(self, line, cli_options):
        assert parse_command_line(line, cli_options) is None

    @pytest.mark.parametrize(
        'line, expected',
        [
            ('s3 ls', ['s3', 'ls']),
            ('  s3   ls  ', ['s3', 'ls']),
            (
                's3 ls\t--region us-west-2',
                ['s3', 'ls', '--region', 'us-west-2'],
            ),
            ("ddb get --key 'a b'", ['ddb', 'get', '--key', 'a b']),
            ('ddb get --key "a b"', ['ddb', 'get', '--key', 'a b']),
            (r'ddb get --key a\ b', ['ddb', 'get', '--key', 'a b']),
            (
                """ddb get --key '{"id": {"S": "a b"}}'""",
                ['ddb', 'get', '--key', '{"id": {"S": "a b"}}'],
            ),
            (
                r'ddb get --key "{\"id\": 1}"',
                ['ddb', 'get', '--key', '{"id": 1}'],
            ),
            ("x y --v 'a$b'", ['x', 'y', '--v', 'a$b']),
            (
                "x y --v 'a|b;c&d<e>f(g)h`i'",
                ['x', 'y', '--v', 'a|b;c&d<e>f(g)h`i'],
            ),
            (
                'x y --v "a|b;c&d<e>f(g)h"',
                ['x', 'y', '--v', 'a|b;c&d<e>f(g)h'],
            ),
            (r'x y --v a\|b', ['x', 'y', '--v', 'a|b']),
            (r'x y --v \$HOME', ['x', 'y', '--v', '$HOME']),
            ('x y --v a~b', ['x', 'y', '--v', 'a~b']),
            ("x y --v '~/f'", ['x', 'y', '--v', '~/f']),
            ('x y --v "~/f"', ['x', 'y', '--v', '~/f']),
            # Inline '#' is not a comment.
            ('x y --v a#b', ['x', 'y', '--v', 'a#b']),
            ("x y --v '#b'", ['x', 'y', '--v', '#b']),
            ('x y --v ""', ['x', 'y', '--v', '']),
            # Wildcards are passed through literally.
            ('s3 rm s3://b/*', ['s3', 'rm', 's3://b/*']),
            ('x y ünïcode', ['x', 'y', 'ünïcode']),
        ],
    )
    def test_splits_arguments(self, line, expected, cli_options):
        assert parse_command_line(line, cli_options) == expected

    @pytest.mark.parametrize(
        'line',
        [
            'aws s3 ls',
            '--debug aws s3 ls',
            '--region us-east-1 aws s3 ls',
        ],
    )
    def test_rejects_leading_aws(self, line, cli_options):
        with pytest.raises(ValueError, match="must not start with 'aws'"):
            parse_command_line(line, cli_options)

    @pytest.mark.parametrize(
        'line',
        [
            'run other.txt',
            '--debug run other.txt',
            '--profile dev run other.txt',
            '--region us-east-1 --output json run other.txt',
        ],
    )
    def test_rejects_nested_run(self, line, cli_options):
        with pytest.raises(ValueError, match='cannot be nested'):
            parse_command_line(line, cli_options)

    def test_run_as_argument_value_is_allowed(self, cli_options):
        # Only the top level command is checked.
        assert parse_command_line('ddb get --key run', cli_options) == [
            'ddb',
            'get',
            '--key',
            'run',
        ]

    @pytest.mark.parametrize(
        'line',
        ['--debug', '--region us-east-1', '--profile'],
    )
    def test_rejects_no_command(self, line, cli_options):
        with pytest.raises(ValueError, match='no command given'):
            parse_command_line(line, cli_options)

    def test_rejects_cli_auto_prompt(self, cli_options):
        with pytest.raises(ValueError, match='--cli-auto-prompt'):
            parse_command_line('s3 ls --cli-auto-prompt', cli_options)

    def test_allows_no_cli_auto_prompt(self, cli_options):
        assert parse_command_line('s3 ls --no-cli-auto-prompt', cli_options)

    @pytest.mark.parametrize('line', ["s3 ls 'unterminated", 's3 ls "a'])
    def test_rejects_unterminated_quotes(self, line, cli_options):
        with pytest.raises(ValueError):
            parse_command_line(line, cli_options)


class TestCheckShellSyntax:
    @pytest.mark.parametrize('char', list('|&;<>()`'))
    def test_rejects_unquoted_operators(self, char):
        with pytest.raises(ValueError, match='unsupported shell syntax'):
            _check_shell_syntax(f's3 ls a{char}b')

    @pytest.mark.parametrize(
        'line',
        [
            's3 ls | grep x',
            's3 ls && s3 ls',
            's3 ls; s3 ls',
            's3 ls > out.txt',
            's3 ls < in.txt',
            's3 ls 2>&1',
            's3 ls &',
            '(s3 ls)',
            's3 ls `whoami`',
            's3 ls $(whoami)',
        ],
    )
    def test_rejects_shell_constructs(self, line):
        with pytest.raises(ValueError, match='unsupported shell syntax'):
            _check_shell_syntax(line)

    @pytest.mark.parametrize('char', list('|&;<>()'))
    def test_allows_operators_in_quotes(self, char):
        _check_shell_syntax(f"s3 ls 'a{char}b'")
        _check_shell_syntax(f's3 ls "a{char}b"')
        _check_shell_syntax(f's3 ls a\\{char}b')

    def test_backtick_allowed_only_in_single_quotes(self):
        _check_shell_syntax("s3 ls 'a`b'")
        _check_shell_syntax('s3 ls a\\`b')
        with pytest.raises(ValueError, match='unsupported shell syntax'):
            _check_shell_syntax('s3 ls "a`b"')

    @pytest.mark.parametrize(
        'line',
        [
            's3 ls $HOME',
            's3 ls a$b',
            's3 ls ${X}',
            's3 ls "$HOME"',
            's3 ls "a $b"',
        ],
    )
    def test_rejects_dollar_outside_single_quotes(self, line):
        with pytest.raises(ValueError, match="'\\$'"):
            _check_shell_syntax(line)

    def test_allows_dollar_in_single_quotes_or_escaped(self):
        _check_shell_syntax("s3 ls '$HOME'")
        _check_shell_syntax("s3 ls 'a \"$b\"'")
        _check_shell_syntax('s3 ls \\$HOME')

    def test_reports_column(self):
        with pytest.raises(ValueError, match='column 7'):
            _check_shell_syntax('s3 ls |')

    @pytest.mark.parametrize(
        'line', ['s3 cp ~/f s3://b', 's3 cp ~ s3://b', 's3 cp ~user/f .']
    )
    def test_rejects_word_initial_tilde(self, line):
        with pytest.raises(ValueError, match='use a full path'):
            _check_shell_syntax(line)

    def test_rejects_tilde_at_line_start(self):
        with pytest.raises(ValueError, match="'~'"):
            _check_shell_syntax('~/bin/x')

    @pytest.mark.parametrize(
        'line',
        [
            's3 cp a~b s3://b',
            "s3 cp '~/f' s3://b",
            's3 cp "~/f" s3://b',
            's3 cp \\~/f s3://b',
            's3 cp --x=~/f s3://b',
            's3 cp 1a=~/f s3://b',
            's3 cp a=b=~ s3://b',
            's3 cp a:~ s3://b',
            's3 cp Name=x,Values=~ s3://b',
        ],
    )
    def test_allows_non_initial_or_quoted_tilde(self, line):
        _check_shell_syntax(line)

    @pytest.mark.parametrize(
        'line', ['s3 cp a=~/f .', 's3 cp _a1=~ .', 's3 cp a=x:~/f .']
    )
    def test_rejects_tilde_in_assignment_like_word(self, line):
        # A shell expands these, e.g. "echo a=~/f" prints a=$HOME/f.
        with pytest.raises(ValueError, match='use a full path'):
            _check_shell_syntax(line)

    @pytest.mark.parametrize('char', ['$', '`'])
    def test_rejects_escaped_dollar_or_backtick_in_double_quotes(self, char):
        # A shell removes the backslash here, but shlex would keep it.
        with pytest.raises(ValueError, match='unsupported shell syntax'):
            _check_shell_syntax(f's3 ls "a\\{char}b"')

    @pytest.mark.parametrize(
        'line', ['x a+=~/x .', 'x b[0]=~/x .', 'x b[x]=a:~/y .']
    )
    def test_rejects_tilde_in_other_assignment_forms(self, line):
        with pytest.raises(ValueError, match='use a full path'):
            _check_shell_syntax(line)

    def test_rejects_inline_comment(self):
        with pytest.raises(ValueError, match='inline comment at column 7'):
            _check_shell_syntax('s3 ls #s3://b')

    @pytest.mark.parametrize(
        'line',
        [
            's3 ls a#b',
            "s3 ls '#b'",
            's3 ls "#b"',
            's3 ls \\#b',
            's3 ls s3://b/a#b',
        ],
    )
    def test_allows_non_comment_hash(self, line):
        _check_shell_syntax(line)

    def test_column_accounts_for_indentation(self):
        with pytest.raises(ValueError, match='column 11'):
            parse_command_line('    s3 ls | head', {})

    def test_rejects_trailing_backslash(self):
        with pytest.raises(ValueError, match='line continuations'):
            _check_shell_syntax('s3 ls \\')

    def test_escaped_trailing_backslash_is_allowed(self):
        _check_shell_syntax('s3 ls \\\\')

    def test_quote_state_resets_after_closing_quote(self):
        with pytest.raises(ValueError):
            _check_shell_syntax("s3 ls 'a' | b")
        with pytest.raises(ValueError):
            _check_shell_syntax('s3 ls "a" ; b')


class TestParseCommandsFile:
    def _write(self, tmp_path, content, name='commands.txt'):
        path = tmp_path / name
        if isinstance(content, str):
            content = content.encode('utf-8')
        path.write_bytes(content)
        return str(path)

    def test_returns_line_numbers(self, tmp_path, cli_options):
        path = self._write(
            tmp_path,
            '# comment\n\ns3 ls\n   \nec2 describe-vpcs --region us-east-1\n',
        )
        assert parse_commands_file(path, cli_options) == [
            (3, ['s3', 'ls']),
            (5, ['ec2', 'describe-vpcs', '--region', 'us-east-1']),
        ]

    def test_empty_file(self, tmp_path, cli_options):
        path = self._write(tmp_path, '')
        assert parse_commands_file(path, cli_options) == []

    def test_only_comments(self, tmp_path, cli_options):
        path = self._write(tmp_path, '# a\n\n# b\n')
        assert parse_commands_file(path, cli_options) == []

    def test_no_trailing_newline(self, tmp_path, cli_options):
        path = self._write(tmp_path, 's3 ls')
        assert parse_commands_file(path, cli_options) == [(1, ['s3', 'ls'])]

    def test_crlf_line_endings(self, tmp_path, cli_options):
        path = self._write(
            tmp_path, 's3 ls\r\n\r\nsts get-caller-identity\r\n'
        )
        assert parse_commands_file(path, cli_options) == [
            (1, ['s3', 'ls']),
            (3, ['sts', 'get-caller-identity']),
        ]

    def test_only_newlines_split_lines(self, tmp_path, cli_options):
        # str.splitlines() would also split on these characters.
        path = self._write(tmp_path, 'ddb get --key "a\u2028b"\x0c\ns3 ls\n')
        assert parse_commands_file(path, cli_options) == [
            (1, ['ddb', 'get', '--key', 'a\u2028b']),
            (2, ['s3', 'ls']),
        ]

    def test_utf8_bom_is_stripped(self, tmp_path, cli_options):
        path = self._write(tmp_path, b'\xef\xbb\xbfs3 ls\n')
        assert parse_commands_file(path, cli_options) == [(1, ['s3', 'ls'])]

    def test_utf8_content(self, tmp_path, cli_options):
        path = self._write(tmp_path, 'ddb get --key "ünïcødé ✓"\n')
        assert parse_commands_file(path, cli_options) == [
            (1, ['ddb', 'get', '--key', 'ünïcødé ✓'])
        ]

    def test_invalid_utf8(self, tmp_path, cli_options):
        path = self._write(tmp_path, b's3 ls \xff\xfe\n')
        with pytest.raises(ParamValidationError, match='Unable to read file'):
            parse_commands_file(path, cli_options)

    def test_missing_file(self, tmp_path, cli_options):
        path = str(tmp_path / 'does-not-exist.txt')
        with pytest.raises(ParamValidationError, match='Unable to read file'):
            parse_commands_file(path, cli_options)

    def test_directory(self, tmp_path, cli_options):
        with pytest.raises(ParamValidationError, match='Unable to read file'):
            parse_commands_file(str(tmp_path), cli_options)

    def test_error_reports_path_and_line(self, tmp_path, cli_options):
        path = self._write(tmp_path, 's3 ls\n\ns3 ls | head\n')
        with pytest.raises(ParamValidationError) as e:
            parse_commands_file(path, cli_options)
        assert f'{path}, line 3:' in str(e.value)

    def test_error_on_last_line_rejects_whole_file(
        self, tmp_path, cli_options
    ):
        path = self._write(tmp_path, 's3 ls\ns3 ls\naws s3 ls\n')
        with pytest.raises(ParamValidationError, match='line 3'):
            parse_commands_file(path, cli_options)


class TestRunCommandArgs:
    def test_has_positional_file_argument(self):
        assert RunCommand.NAME == 'run'
        assert RunCommand.ARG_TABLE[0]['name'] == 'file'
        assert RunCommand.ARG_TABLE[0]['positional_arg'] is True


class TestSharedHTTPSessions:
    def test_same_settings_share_a_session(self):
        created = []

        def factory(**kwargs):
            created.append(kwargs)
            return object()

        sessions = SharedHTTPSessions(factory)
        kwargs = {
            'timeout': (60, 60),
            'proxies': {'https': 'http://proxy:8080'},
            'verify': True,
            'max_pool_connections': 10,
            'socket_options': [(6, 1, 1)],
            'client_cert': None,
            'proxies_config': {'proxy_use_forwarding_for_https': False},
        }
        first = sessions(**kwargs)
        assert sessions(**dict(kwargs)) is first
        assert len(created) == 1

    @pytest.mark.parametrize(
        'changes',
        [
            {'verify': False},
            {'verify': '/tmp/ca.pem'},
            {'timeout': (5, 60)},
            {'proxies': {'https': 'http://other:8080'}},
            {'proxies_config': {'proxy_ca_bundle': '/tmp/p.pem'}},
            {'max_pool_connections': 20},
            {'socket_options': []},
            {'client_cert': ('/tmp/c.pem', '/tmp/k.pem')},
        ],
    )
    def test_different_settings_get_different_sessions(self, changes):
        sessions = SharedHTTPSessions(lambda **kwargs: object())
        kwargs = {
            'timeout': (60, 60),
            'proxies': {},
            'verify': True,
            'max_pool_connections': 10,
            'socket_options': [(6, 1, 1)],
            'client_cert': None,
            'proxies_config': None,
        }
        assert sessions(**kwargs) is not sessions(**{**kwargs, **changes})

    def test_unhashable_settings_are_never_shared(self):
        sessions = SharedHTTPSessions(lambda **kwargs: object())
        unhashable = {'verify': True, 'proxies_config': {'x': {1, 2}}}
        assert sessions(**unhashable) is not sessions(**unhashable)

    def test_close_closes_and_forgets_sessions(self):
        closed = []

        class FakeSession:
            def __init__(self, **kwargs):
                pass

            def close(self):
                closed.append(self)

        sessions = SharedHTTPSessions(FakeSession)
        first = sessions(verify=True)
        sessions.close()
        assert closed == [first]
        assert sessions(verify=True) is not first

    def test_close_continues_after_a_failure(self):
        closed = []

        class FakeSession:
            def __init__(self, fail, **kwargs):
                self.fail = fail

            def close(self):
                closed.append(self)
                if self.fail:
                    raise RuntimeError('boom')

        sessions = SharedHTTPSessions(FakeSession)
        sessions(fail=True)
        sessions(fail=False)
        sessions.close()
        assert len(closed) == 2
