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
"""Functional tests for ``aws run``.

Every line run by ``aws run`` gets its own botocore session, so stubs
registered on a single session would not apply to the lines. Instead,
the HTTP layer (``URLLib3Session.send``) is patched for the whole
process, which captures the fully signed requests of every line.
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile

import botocore.awsrequest
import pytest
from botocore.history import get_global_history_recorder

import awscli.clidriver
from awscli.customizations.run import RunCommand, _reset_logging
from awscli.testutils import (
    _LOADER,
    capture_output,
    create_clidriver,
    mock,
    unittest,
)
from tests import RawResponse

LIST_TABLES_RESPONSE = {'TableNames': ['table-a', 'table-b']}

CONFIG = """\
[default]
region = us-east-1
s3 =
  preferred_transfer_client = classic

[profile dev]
region = eu-west-1

[profile prod]
region = ap-southeast-2
"""

CREDENTIALS = """\
[default]
aws_access_key_id = DEFAULTACCESSKEY
aws_secret_access_key = default-secret

[dev]
aws_access_key_id = DEVACCESSKEY
aws_secret_access_key = dev-secret

[prod]
aws_access_key_id = PRODACCESSKEY
aws_secret_access_key = prod-secret
"""


class BaseRunTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.config_file = self._write_file('config', CONFIG)
        self.credentials_file = self._write_file('credentials', CREDENTIALS)
        self.environ = {
            'AWS_DATA_PATH': os.environ['AWS_DATA_PATH'],
            'AWS_CONFIG_FILE': self.config_file,
            'AWS_SHARED_CREDENTIALS_FILE': self.credentials_file,
            'AWS_EC2_METADATA_DISABLED': 'true',
            'AWS_CLI_HISTORY_FILE': os.path.join(self.tempdir, 'history.db'),
        }
        if os.environ.get('ComSpec'):
            self.environ['ComSpec'] = os.environ['ComSpec']
        self.environ_patch = mock.patch('os.environ', self.environ)
        self.environ_patch.start()

        self.requests = []
        self.responses = []
        self.send_patch = mock.patch(
            'botocore.httpsession.URLLib3Session.send',
            autospec=True,
            side_effect=self._send,
        )
        self.send_patch.start()

        self.history_recorder = get_global_history_recorder()
        self._history_handlers = list(self.history_recorder._handlers)
        self._history_enabled = self.history_recorder._enabled

    def tearDown(self):
        self.send_patch.stop()
        self.environ_patch.stop()
        self.history_recorder._handlers[:] = self._history_handlers
        self.history_recorder._enabled = self._history_enabled
        RunCommand._is_running = False
        # --debug on a line leaves debug logging on, like it would for a
        # process that is about to exit. Turn it off for other tests.
        _reset_logging()
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def _write_file(self, name, content):
        path = os.path.join(self.tempdir, name)
        mode = 'wb' if isinstance(content, bytes) else 'w'
        kwargs = {} if isinstance(content, bytes) else {'encoding': 'utf-8'}
        with open(path, mode, **kwargs) as f:
            f.write(content)
        return path

    def write_commands(self, content, name='commands.txt'):
        return self._write_file(name, content)

    def add_response(self, body=None, status_code=200, headers=None):
        if body is None:
            body = LIST_TABLES_RESPONSE
        if not isinstance(body, bytes):
            body = json.dumps(body).encode('utf-8')
        self.responses.append((status_code, headers or {}, body))

    def add_error_response(self, code='ResourceNotFoundException'):
        self.add_response(
            {'__type': code, 'message': 'Requested resource not found'},
            status_code=400,
        )

    def _send(self, http_session, request):
        snapshot = self._snapshot_request(request)
        snapshot['http_session'] = http_session
        self.requests.append(snapshot)
        if self.responses:
            status_code, headers, body = self.responses.pop(0)
        else:
            status_code, headers, body = (
                200,
                {},
                json.dumps(LIST_TABLES_RESPONSE).encode('utf-8'),
            )
        return botocore.awsrequest.AWSResponse(
            request.url, status_code, headers, RawResponse(body)
        )

    def _snapshot_request(self, request):
        headers = {}
        for key, value in request.headers.items():
            if isinstance(value, bytes):
                value = value.decode('utf-8')
            headers[key.lower()] = value
        body = request.body
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        return {
            'url': request.url,
            'headers': headers,
            'body': body,
            'json': self._maybe_json(body),
            'history_handlers': len(self.history_recorder._handlers),
            'botocore_log_level': (
                logging.getLogger('botocore').getEffectiveLevel()
            ),
        }

    def _maybe_json(self, body):
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            return None

    def run_cmd(self, args, expected_rc=0, driver=None):
        if driver is None:
            driver = create_clidriver()
        entry_point = awscli.clidriver.AWSCLIEntryPoint(driver)
        with capture_output() as captured:
            rc = entry_point.main(args)
        stdout = captured.stdout.getvalue()
        stderr = captured.stderr.getvalue()
        self.assertEqual(
            rc,
            expected_rc,
            f'Unexpected rc for {args}\nstdout:\n{stdout}\nstderr:\n{stderr}',
        )
        return stdout, stderr

    def run_file(self, content, expected_rc=0, global_args=None, driver=None):
        path = self.write_commands(content)
        args = list(global_args or []) + ['run', path]
        return self.run_cmd(args, expected_rc=expected_rc, driver=driver)

    def access_key(self, request):
        auth = request['headers'].get('authorization')
        if auth is None:
            return None
        return re.search(r'Credential=([^/]+)/', auth).group(1)

    def credential_scope_region(self, request):
        auth = request['headers']['authorization']
        return re.search(r'Credential=[^/]+/\d+/([^/]+)/', auth).group(1)

    def host(self, request):
        return re.match(r'https?://([^/]+)', request['url']).group(1)

    def targets(self):
        return [r['headers'].get('x-amz-target') for r in self.requests]


class TestRunBasics(BaseRunTest):
    def test_single_command(self):
        stdout, stderr = self.run_file('dynamodb list-tables\n')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(
            self.requests[0]['headers']['x-amz-target'],
            'DynamoDB_20120810.ListTables',
        )
        self.assertEqual(json.loads(stdout), LIST_TABLES_RESPONSE)
        self.assertEqual(stderr, '')

    def test_multiple_commands_run_in_order(self):
        self.run_file(
            'dynamodb list-tables\n'
            'dynamodb describe-table --table-name first\n'
            'dynamodb describe-limits\n'
        )
        self.assertEqual(
            self.targets(),
            [
                'DynamoDB_20120810.ListTables',
                'DynamoDB_20120810.DescribeTable',
                'DynamoDB_20120810.DescribeLimits',
            ],
        )
        self.assertEqual(self.requests[1]['json'], {'TableName': 'first'})

    def test_repeated_invocation_of_same_command(self):
        self.run_file(
            'dynamodb describe-table --table-name t1\n'
            'dynamodb describe-table --table-name t2\n'
            'dynamodb describe-table --table-name t3\n'
        )
        self.assertEqual(
            [r['json'] for r in self.requests],
            [{'TableName': 't1'}, {'TableName': 't2'}, {'TableName': 't3'}],
        )

    def test_different_services(self):
        self.add_response(LIST_TABLES_RESPONSE)
        self.add_response({'QueueUrls': ['https://queue']})
        stdout, _ = self.run_file('dynamodb list-tables\nsqs list-queues\n')
        self.assertEqual(
            [self.host(r) for r in self.requests],
            [
                'dynamodb.us-east-1.amazonaws.com',
                'sqs.us-east-1.amazonaws.com',
            ],
        )
        self.assertIn('table-a', stdout)
        self.assertIn('https://queue', stdout)

    def test_empty_lines_and_comments_are_skipped(self):
        self.run_file(
            '# This is a comment\n'
            '\n'
            '   \n'
            'dynamodb describe-table --table-name a\n'
            '    # indented comment\n'
            '\t\n'
            'dynamodb describe-table --table-name b\n'
            '#dynamodb describe-table --table-name c\n'
        )
        self.assertEqual(
            [r['json'] for r in self.requests],
            [{'TableName': 'a'}, {'TableName': 'b'}],
        )

    def test_file_with_only_comments(self):
        stdout, stderr = self.run_file('# nothing\n\n')
        self.assertEqual(self.requests, [])
        self.assertEqual(stdout, '')

    def test_empty_file(self):
        self.run_file('')
        self.assertEqual(self.requests, [])

    def test_utf8_bom_and_crlf(self):
        self.run_file(
            '﻿dynamodb describe-table --table-name a\r\n'
            '\r\n'
            'dynamodb describe-table --table-name b\r\n'
        )
        self.assertEqual(
            [r['json'] for r in self.requests],
            [{'TableName': 'a'}, {'TableName': 'b'}],
        )

    def test_missing_file(self):
        _, stderr = self.run_cmd(
            ['run', os.path.join(self.tempdir, 'missing.txt')],
            expected_rc=252,
        )
        self.assertIn('Unable to read file', stderr)
        self.assertEqual(self.requests, [])

    def test_missing_file_argument(self):
        self.run_cmd(['run'], expected_rc=252)

    def test_help(self):
        # The help command should be available, rendered via the renderer.
        entry_point = awscli.clidriver.AWSCLIEntryPoint(create_clidriver())
        with mock.patch('awscli.help.get_renderer') as get_renderer:
            with capture_output():
                entry_point.main(['run', 'help'])
        rendered = get_renderer.return_value.render.call_args[0][0]
        self.assertIn(b'aws run', rendered)


class TestRunQuoting(BaseRunTest):
    def assert_table_name(self, line, expected):
        self.run_file(f'dynamodb describe-table --table-name {line}\n')
        self.assertEqual(self.requests[-1]['json'], {'TableName': expected})

    def test_single_quotes(self):
        self.assert_table_name("'my table'", 'my table')

    def test_double_quotes(self):
        self.assert_table_name('"my table"', 'my table')

    def test_escaped_space(self):
        self.assert_table_name('my\\ table', 'my table')

    def test_dollar_in_single_quotes(self):
        self.assert_table_name("'cost$100'", 'cost$100')

    def test_escaped_dollar(self):
        self.assert_table_name('cost\\$100', 'cost$100')

    def test_quoted_shell_operators(self):
        self.assert_table_name("'a|b;c&d>e<f(g)'", 'a|b;c&d>e<f(g)')

    def test_inline_hash_is_literal(self):
        self.assert_table_name('a#b', 'a#b')

    def test_unicode(self):
        self.assert_table_name("'tåble ✓'", 'tåble ✓')

    def test_quoted_tilde(self):
        self.assert_table_name("'~table'", '~table')

    def test_json_value_in_single_quotes(self):
        self.add_response({})
        self.run_file(
            'dynamodb get-item --table-name t '
            '--key \'{"id": {"S": "a b"}, "n": {"N": "1"}}\'\n'
        )
        self.assertEqual(
            self.requests[0]['json'],
            {'TableName': 't', 'Key': {'id': {'S': 'a b'}, 'n': {'N': '1'}}},
        )

    def test_json_value_in_double_quotes(self):
        self.add_response({})
        self.run_file(
            'dynamodb get-item --table-name t --key "{\\"id\\": {\\"S\\": \\"x\\"}}"\n'
        )
        self.assertEqual(
            self.requests[0]['json'],
            {'TableName': 't', 'Key': {'id': {'S': 'x'}}},
        )

    def test_shorthand_value(self):
        self.add_response({})
        self.run_file('dynamodb get-item --table-name t --key id={S=abc}\n')
        self.assertEqual(
            self.requests[0]['json'],
            {'TableName': 't', 'Key': {'id': {'S': 'abc'}}},
        )

    def test_file_uri_parameter(self):
        key_file = self._write_file('key.json', '{"id": {"S": "from-file"}}')
        self.add_response({})
        self.run_file(
            f"dynamodb get-item --table-name t --key 'file://{key_file}'\n"
        )
        self.assertEqual(
            self.requests[0]['json']['Key'], {'id': {'S': 'from-file'}}
        )


class TestRunFailures(BaseRunTest):
    def assert_failed_on_line(self, stderr, lineno, rc):
        self.assertIn(
            f'aws run: the command on line {lineno} failed with exit '
            f'code {rc}. No further commands were run.',
            stderr,
        )

    def test_client_error_stops_execution(self):
        self.add_response()
        self.add_error_response()
        stdout, stderr = self.run_file(
            'dynamodb describe-table --table-name one\n'
            'dynamodb describe-table --table-name two\n'
            'dynamodb describe-table --table-name three\n',
            expected_rc=254,
        )
        self.assertEqual(
            [r['json'] for r in self.requests],
            [{'TableName': 'one'}, {'TableName': 'two'}],
        )
        self.assertIn('ResourceNotFoundException', stderr)
        self.assert_failed_on_line(stderr, 2, 254)

    def test_failure_on_first_line(self):
        self.add_error_response()
        _, stderr = self.run_file(
            'dynamodb list-tables\ndynamodb list-tables\n', expected_rc=254
        )
        self.assertEqual(len(self.requests), 1)
        self.assert_failed_on_line(stderr, 1, 254)

    def test_line_number_accounts_for_comments(self):
        self.add_error_response()
        _, stderr = self.run_file(
            '# comment\n\ndynamodb list-tables\n', expected_rc=254
        )
        self.assert_failed_on_line(stderr, 3, 254)

    def test_failure_message_does_not_include_arguments(self):
        self.add_error_response()
        _, stderr = self.run_file(
            'dynamodb describe-table --table-name super-secret-value\n',
            expected_rc=254,
        )
        note = stderr[stderr.index('aws run:') :]
        self.assertNotIn('super-secret-value', note)

    def test_unknown_service(self):
        _, stderr = self.run_file(
            'not-a-service list-things\ndynamodb list-tables\n',
            expected_rc=252,
        )
        self.assertEqual(self.requests, [])
        self.assertIn("invalid choice 'not-a-service'", stderr)
        self.assert_failed_on_line(stderr, 1, 252)

    def test_unknown_operation(self):
        _, stderr = self.run_file(
            'dynamodb list-tables\n'
            'dynamodb not-an-operation\n'
            'dynamodb list-tables\n',
            expected_rc=252,
        )
        self.assertEqual(len(self.requests), 1)
        self.assert_failed_on_line(stderr, 2, 252)

    def test_param_validation_error(self):
        _, stderr = self.run_file(
            'dynamodb describe-table\ndynamodb list-tables\n',
            expected_rc=252,
        )
        self.assertEqual(self.requests, [])
        self.assertIn('--table-name', stderr)
        self.assert_failed_on_line(stderr, 1, 252)

    def test_invalid_json_parameter(self):
        _, stderr = self.run_file(
            "dynamodb get-item --table-name t --key '{not json'\n"
            'dynamodb list-tables\n',
            expected_rc=252,
        )
        self.assertEqual(self.requests, [])
        self.assert_failed_on_line(stderr, 1, 252)

    def test_profile_not_found(self):
        _, stderr = self.run_file(
            'dynamodb list-tables\n'
            'dynamodb list-tables --profile does-not-exist\n'
            'dynamodb list-tables\n',
            expected_rc=255,
        )
        self.assertEqual(len(self.requests), 1)
        self.assertIn('does-not-exist', stderr)
        self.assert_failed_on_line(stderr, 2, 255)

    def test_failure_after_success_keeps_earlier_output(self):
        self.add_response()
        self.add_error_response()
        stdout, _ = self.run_file(
            'dynamodb list-tables\ndynamodb list-tables\n', expected_rc=254
        )
        self.assertEqual(json.loads(stdout), LIST_TABLES_RESPONSE)


class TestRunValidation(BaseRunTest):
    def assert_rejected(self, content, message, line=None):
        _, stderr = self.run_file(content, expected_rc=252)
        self.assertEqual(self.requests, [])
        self.assertIn(message, stderr)
        if line is not None:
            self.assertIn(f'line {line}:', stderr)
        return stderr

    def test_parse_error_on_later_line_runs_nothing(self):
        self.assert_rejected(
            'dynamodb list-tables\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables | head\n',
            'unsupported shell syntax',
            line=3,
        )

    def test_unterminated_quote_runs_nothing(self):
        self.assert_rejected(
            "dynamodb list-tables\ndynamodb describe-table --table-name 'a\n",
            'unable to parse command',
            line=2,
        )

    def test_shell_operators_rejected(self):
        for operator in ['|', '&', ';', '<', '>', '(', ')', '`']:
            with self.subTest(operator=operator):
                self.assert_rejected(
                    f'dynamodb describe-table --table-name a{operator}b\n',
                    f"unsupported shell syntax {operator!r}",
                    line=1,
                )

    def test_command_separators_rejected(self):
        for line in [
            'dynamodb list-tables && dynamodb list-tables',
            'dynamodb list-tables; dynamodb list-tables',
            'dynamodb list-tables > out.json',
            'dynamodb list-tables 2>&1',
            'dynamodb describe-table --table-name $(whoami)',
        ]:
            with self.subTest(line=line):
                self.assert_rejected(line + '\n', 'unsupported shell syntax')

    def test_dollar_rejected_unquoted_and_in_double_quotes(self):
        for value in ['$HOME', '${HOME}', '"$HOME"', '"a $b"']:
            with self.subTest(value=value):
                self.assert_rejected(
                    f'dynamodb describe-table --table-name {value}\n',
                    "unsupported shell syntax '$'",
                )

    def test_dollar_allowed_in_single_quotes(self):
        self.run_file("dynamodb describe-table --table-name '$HOME'\n")
        self.assertEqual(self.requests[0]['json'], {'TableName': '$HOME'})

    def test_environment_variables_are_not_expanded(self):
        self.environ['MY_TABLE'] = 'expanded'
        self.run_file("dynamodb describe-table --table-name '$MY_TABLE'\n")
        self.assertEqual(self.requests[0]['json'], {'TableName': '$MY_TABLE'})

    def test_tilde_rejected(self):
        stderr = self.assert_rejected(
            'dynamodb get-item --table-name t --key file://x\n'
            'dynamodb describe-table --table-name ~/table\n',
            "'~'",
            line=2,
        )
        self.assertIn('use a full path', stderr)

    def test_trailing_backslash_rejected(self):
        self.assert_rejected(
            'dynamodb list-tables \\\n--region us-west-2\n',
            'line continuations are not supported',
            line=1,
        )

    def test_nested_run_rejected(self):
        nested = self.write_commands('dynamodb list-tables\n', 'nested.txt')
        self.assert_rejected(
            f'dynamodb list-tables\nrun {nested}\n',
            'aws run cannot be nested',
            line=2,
        )

    def test_nested_run_after_global_options_rejected(self):
        nested = self.write_commands('dynamodb list-tables\n', 'nested.txt')
        self.assert_rejected(
            f'--region us-west-2 --debug run {nested}\n',
            'aws run cannot be nested',
        )

    def test_reentrant_run_rejected(self):
        RunCommand._is_running = True
        path = self.write_commands('dynamodb list-tables\n')
        _, stderr = self.run_cmd(['run', path], expected_rc=252)
        self.assertIn('cannot be called from a command run by aws run', stderr)
        self.assertEqual(self.requests, [])

    def test_leading_aws_rejected(self):
        self.assert_rejected(
            'dynamodb list-tables\naws dynamodb list-tables\n',
            "commands must not start with 'aws'",
            line=2,
        )

    def test_cli_auto_prompt_rejected(self):
        self.assert_rejected(
            'dynamodb list-tables --cli-auto-prompt\n',
            '--cli-auto-prompt is not supported',
            line=1,
        )

    def test_only_global_options_rejected(self):
        self.assert_rejected('--region us-west-2\n', 'no command given')

    def test_invalid_utf8_rejected(self):
        path = self._write_file('bad.txt', b'dynamodb list-tables \xff\n')
        _, stderr = self.run_cmd(['run', path], expected_rc=252)
        self.assertIn('Unable to read file', stderr)
        self.assertEqual(self.requests, [])


class TestRunGlobalOptions(BaseRunTest):
    def test_outer_global_options_rejected(self):
        for global_args in [
            ['--profile', 'dev'],
            ['--region', 'us-west-2'],
            ['--endpoint-url', 'https://example.com'],
            ['--output', 'text'],
            ['--query', 'TableNames'],
            ['--ca-bundle', '/tmp/ca.pem'],
            ['--cli-binary-format', 'raw-in-base64-out'],
            ['--no-verify-ssl'],
            ['--no-paginate'],
            ['--no-sign-request'],
            ['--color', 'on'],
            ['--cli-read-timeout', '5'],
            ['--cli-connect-timeout', '5'],
            ['--cli-error-format', 'json'],
        ]:
            with self.subTest(global_args=global_args):
                _, stderr = self.run_file(
                    'dynamodb list-tables\n',
                    expected_rc=252,
                    global_args=global_args,
                )
                self.assertIn(
                    'not supported with aws run: ' + global_args[0], stderr
                )
                self.assertEqual(self.requests, [])

    def test_multiple_outer_global_options_listed(self):
        _, stderr = self.run_file(
            'dynamodb list-tables\n',
            expected_rc=252,
            global_args=['--region', 'us-west-2', '--output', 'text'],
        )
        self.assertIn('--region, --output', stderr)

    def test_outer_global_options_rejected_before_parsing_file(self):
        _, stderr = self.run_cmd(
            [
                '--region',
                'us-west-2',
                'run',
                os.path.join(self.tempdir, 'missing.txt'),
            ],
            expected_rc=252,
        )
        self.assertIn('not supported with aws run', stderr)

    def test_outer_debug_is_forwarded_to_each_line(self):
        _, stderr = self.run_file(
            'dynamodb list-tables\ndynamodb list-tables\n',
            global_args=['--debug'],
        )
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertEqual(request['botocore_log_level'], logging.DEBUG)
        # One for the outer "aws run", and one for each line.
        self.assertGreaterEqual(stderr.count('CLI version'), 3)

    def test_debug_on_one_line_does_not_leak(self):
        self.run_file(
            '--debug dynamodb list-tables\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables --debug\n'
            'dynamodb list-tables\n'
        )
        levels = [r['botocore_log_level'] for r in self.requests]
        self.assertEqual(levels[0], logging.DEBUG)
        self.assertNotEqual(levels[1], logging.DEBUG)
        self.assertEqual(levels[2], logging.DEBUG)
        self.assertNotEqual(levels[3], logging.DEBUG)

    def test_no_cli_pager_on_line_is_allowed(self):
        self.run_file('dynamodb list-tables --no-cli-pager\n')
        self.assertEqual(len(self.requests), 1)


class TestRunIsolation(BaseRunTest):
    def test_different_regions_per_line(self):
        self.run_file(
            'dynamodb list-tables --region us-west-2\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables --region eu-central-1\n'
            'dynamodb list-tables\n'
        )
        self.assertEqual(
            [self.host(r) for r in self.requests],
            [
                'dynamodb.us-west-2.amazonaws.com',
                'dynamodb.us-east-1.amazonaws.com',
                'dynamodb.eu-central-1.amazonaws.com',
                'dynamodb.us-east-1.amazonaws.com',
            ],
        )
        self.assertEqual(
            [self.credential_scope_region(r) for r in self.requests],
            ['us-west-2', 'us-east-1', 'eu-central-1', 'us-east-1'],
        )

    def test_different_profiles_per_line(self):
        self.run_file(
            'dynamodb list-tables --profile dev\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables --profile prod\n'
            'dynamodb list-tables --profile dev\n'
            'dynamodb list-tables\n'
        )
        self.assertEqual(
            [self.access_key(r) for r in self.requests],
            [
                'DEVACCESSKEY',
                'DEFAULTACCESSKEY',
                'PRODACCESSKEY',
                'DEVACCESSKEY',
                'DEFAULTACCESSKEY',
            ],
        )
        # The profile's region is used too.
        self.assertEqual(
            [self.credential_scope_region(r) for r in self.requests],
            [
                'eu-west-1',
                'us-east-1',
                'ap-southeast-2',
                'eu-west-1',
                'us-east-1',
            ],
        )

    def test_profile_with_region_override(self):
        self.run_file(
            'dynamodb list-tables --profile dev --region us-west-2\n'
            'dynamodb list-tables --profile dev\n'
        )
        self.assertEqual(
            [self.credential_scope_region(r) for r in self.requests],
            ['us-west-2', 'eu-west-1'],
        )
        self.assertEqual(
            [self.access_key(r) for r in self.requests],
            ['DEVACCESSKEY', 'DEVACCESSKEY'],
        )

    def test_aws_profile_environment_variable_applies_to_every_line(self):
        self.environ['AWS_PROFILE'] = 'dev'
        self.run_file(
            'dynamodb list-tables\n'
            'dynamodb list-tables --profile prod\n'
            'dynamodb list-tables\n'
        )
        self.assertEqual(
            [self.access_key(r) for r in self.requests],
            ['DEVACCESSKEY', 'PRODACCESSKEY', 'DEVACCESSKEY'],
        )

    def test_different_endpoint_urls_per_line(self):
        self.run_file(
            'dynamodb list-tables --endpoint-url https://localhost:8000\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables --endpoint-url http://127.0.0.1:9000\n'
            'dynamodb list-tables\n'
        )
        self.assertEqual(
            [r['url'] for r in self.requests],
            [
                'https://localhost:8000/',
                'https://dynamodb.us-east-1.amazonaws.com/',
                'http://127.0.0.1:9000/',
                'https://dynamodb.us-east-1.amazonaws.com/',
            ],
        )

    def test_no_sign_request_does_not_leak(self):
        self.run_file(
            'dynamodb list-tables\n'
            'dynamodb list-tables --no-sign-request\n'
            'dynamodb list-tables\n'
        )
        self.assertEqual(
            [self.access_key(r) for r in self.requests],
            ['DEFAULTACCESSKEY', None, 'DEFAULTACCESSKEY'],
        )

    def test_output_and_query_per_line(self):
        stdout, _ = self.run_file(
            'dynamodb list-tables --output text\n'
            'dynamodb list-tables --query TableNames[0]\n'
            'dynamodb list-tables\n'
            "dynamodb list-tables --output yaml --query 'TableNames[1]'\n"
        )
        expected = (
            'TABLENAMES\ttable-a\n'
            'TABLENAMES\ttable-b\n'
            '"table-a"\n' + json.dumps(LIST_TABLES_RESPONSE, indent=4) + '\n'
            '"table-b"\n'
        )
        self.assertEqual(stdout, expected)

    def test_output_from_config_applies_to_each_line(self):
        with open(self.config_file, 'a') as f:
            f.write('\n[profile textout]\noutput = text\n')
        with open(self.credentials_file, 'a') as f:
            f.write(
                '\n[textout]\naws_access_key_id = TEXTKEY\n'
                'aws_secret_access_key = s\n'
            )
        stdout, _ = self.run_file(
            'dynamodb list-tables --profile textout --region us-east-1\n'
            'dynamodb list-tables --query TableNames[0]\n'
        )
        self.assertEqual(
            stdout,
            'TABLENAMES\ttable-a\nTABLENAMES\ttable-b\n"table-a"\n',
        )

    def test_each_line_gets_a_new_session(self):
        drivers = []
        real_create_clidriver = awscli.clidriver.create_clidriver

        def record_driver(*args, **kwargs):
            driver = real_create_clidriver(*args, **kwargs)
            drivers.append(driver)
            return driver

        outer = create_clidriver()
        with mock.patch(
            'awscli.customizations.run.create_clidriver',
            side_effect=record_driver,
        ):
            self.run_file(
                'dynamodb list-tables\n'
                'dynamodb list-tables\n'
                'sqs list-queues\n',
                driver=outer,
            )
        self.assertEqual(len(drivers), 3)
        sessions = [d.session for d in drivers]
        self.assertEqual(len({id(s) for s in sessions}), 3)
        emitters = [s.get_component('event_emitter') for s in sessions]
        self.assertEqual(len({id(e) for e in emitters}), 3)


class TestRunSharedState(BaseRunTest):
    def _record_drivers(self):
        drivers = []
        real_create_clidriver = awscli.clidriver.create_clidriver

        def record_driver(*args, **kwargs):
            driver = real_create_clidriver(*args, **kwargs)
            drivers.append(driver)
            return driver

        patch = mock.patch(
            'awscli.customizations.run.create_clidriver',
            side_effect=record_driver,
        )
        return drivers, patch

    def test_data_loader_shared_with_outer_session(self):
        drivers, patch = self._record_drivers()
        outer = create_clidriver()
        with patch:
            with mock.patch(
                'awscli.clidriver.create_loader',
                wraps=awscli.clidriver.create_loader,
            ) as create_loader:
                self.run_file(
                    'dynamodb list-tables\n'
                    'dynamodb list-tables --profile dev\n'
                    'sqs list-queues --region us-west-2\n',
                    driver=outer,
                )
        self.assertEqual(len(drivers), 3)
        # The outer session's (already warm) loader is reused.
        create_loader.assert_not_called()
        for driver in drivers:
            self.assertIs(driver.session.get_component('data_loader'), _LOADER)

    def test_data_loader_shared_between_lines(self):
        drivers, patch = self._record_drivers()
        with patch:
            path = self.write_commands(
                'dynamodb list-tables\n'
                'dynamodb list-tables --region us-west-2\n'
                'sqs list-queues\n'
            )
            # Not seeded from the test loader: use a real fresh driver.
            outer = awscli.clidriver.create_clidriver(['run', path])
            drivers.clear()
            self.run_cmd(['run', path], driver=outer)
        loaders = [d.session.get_component('data_loader') for d in drivers]
        self.assertEqual(len(loaders), 3)
        self.assertIs(loaders[0], outer.session.get_component('data_loader'))
        self.assertTrue(all(loader is loaders[0] for loader in loaders))

    def test_data_loaders_dict_seeded_and_passed_to_each_line(self):
        calls = []
        real_create_clidriver = awscli.clidriver.create_clidriver

        def record(*args, **kwargs):
            calls.append(kwargs.get('data_loaders'))
            return real_create_clidriver(*args, **kwargs)

        outer = create_clidriver()
        with mock.patch(
            'awscli.customizations.run.create_clidriver', side_effect=record
        ):
            self.run_file(
                'dynamodb list-tables\ndynamodb list-tables\n', driver=outer
            )
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], calls[1])
        self.assertEqual(list(calls[0].values()), [_LOADER])

    def test_pager_is_disabled(self):
        self.environ['AWS_PAGER'] = 'less'
        with mock.patch('awscli.utils.is_a_tty', return_value=True):
            with mock.patch('awscli.utils.Popen') as popen:
                stdout, _ = self.run_file(
                    'dynamodb list-tables\ndynamodb list-tables\n'
                )
        popen.assert_not_called()
        self.assertEqual(stdout.count('table-a'), 2)

    def test_pager_from_config_is_disabled(self):
        with open(self.config_file, 'a') as f:
            f.write('\n[profile paged]\ncli_pager = less\n')
        with open(self.credentials_file, 'a') as f:
            f.write(
                '\n[paged]\naws_access_key_id = PAGEDKEY\n'
                'aws_secret_access_key = s\n'
            )
        with mock.patch('awscli.utils.is_a_tty', return_value=True):
            with mock.patch('awscli.utils.Popen') as popen:
                self.run_file(
                    'dynamodb list-tables --profile paged --region us-east-1\n'
                )
        popen.assert_not_called()
        self.assertEqual(len(self.requests), 1)

    def test_history_handlers_do_not_accumulate(self):
        with open(self.config_file, 'a') as f:
            f.write('\n[profile history]\ncli_history = enabled\n')
        with open(self.credentials_file, 'a') as f:
            f.write(
                '\n[history]\naws_access_key_id = HISTKEY\n'
                'aws_secret_access_key = s\n'
            )
        self.environ['AWS_PROFILE'] = 'history'
        self.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        before = len(self.history_recorder._handlers)
        self.run_file(
            'dynamodb list-tables\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables\n'
            'dynamodb list-tables\n'
        )
        counts = [r['history_handlers'] for r in self.requests]
        # Every line only sees the handler added for its own session.
        self.assertEqual(counts, [1, 1, 1, 1])
        # After the run, only the outer command's handler remains.
        self.assertEqual(len(self.history_recorder._handlers), before + 1)

    def test_line_with_history_disabled_is_not_recorded(self):
        # History is enabled for the run command's own profile, but not for
        # the profile of the line, so nothing from the line is recorded.
        with open(self.config_file, 'a') as f:
            f.write('\n[profile history]\ncli_history = enabled\n')
        with open(self.credentials_file, 'a') as f:
            f.write(
                '\n[history]\naws_access_key_id = HISTKEY\n'
                'aws_secret_access_key = s\n'
            )
        self.environ['AWS_PROFILE'] = 'history'
        self.run_file(
            'dynamodb describe-table --table-name SECRETNAME --profile dev\n'
        )
        self.assertEqual(self.requests[0]['history_handlers'], 0)
        self.assertEqual(self.access_key(self.requests[0]), 'DEVACCESSKEY')
        connection = sqlite3.connect(self.environ['AWS_CLI_HISTORY_FILE'])
        try:
            rows = connection.execute(
                "SELECT event_type FROM records WHERE payload LIKE ?",
                ('%SECRETNAME%',),
            ).fetchall()
            all_rows = connection.execute(
                "SELECT event_type FROM records"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [])
        # The run command itself is still recorded.
        self.assertIn(('CLI_ARGUMENTS',), all_rows)

    def test_history_handler_from_one_line_does_not_leak(self):
        with open(self.config_file, 'a') as f:
            f.write('\n[profile history]\ncli_history = enabled\n')
        with open(self.credentials_file, 'a') as f:
            f.write(
                '\n[history]\naws_access_key_id = HISTKEY\n'
                'aws_secret_access_key = s\n'
            )
        before = len(self.history_recorder._handlers)
        self.run_file(
            'dynamodb list-tables --profile history --region us-east-1\n'
            'dynamodb list-tables\n'
        )
        counts = [r['history_handlers'] for r in self.requests]
        self.assertEqual(counts, [before + 1, before])
        self.assertEqual(len(self.history_recorder._handlers), before)

    def test_colored_output_does_not_affect_later_lines(self):
        # Colored table output wraps sys.stdout with colorama, which would
        # add escape codes to the output of the following lines.
        stdout, _ = self.run_file(
            'dynamodb list-tables --output table --color on\n'
            'dynamodb list-tables\n'
        )
        json_output = stdout[stdout.index('{\n') :]
        self.assertEqual(json.loads(json_output), LIST_TABLES_RESPONSE)

    def test_lines_send_prompt_off_user_agent(self):
        self.run_file('dynamodb list-tables\n')
        user_agent = self.requests[0]['headers']['user-agent']
        self.assertIn('md/prompt#off', user_agent)
        self.assertNotIn('md/command#run', user_agent)

    def test_connections_are_shared_between_lines(self):
        self.run_file(
            'dynamodb list-tables\n'
            'dynamodb list-tables --profile dev\n'
            'dynamodb list-tables --region us-west-2\n'
            'sqs list-queues\n'
        )
        http_sessions = [r['http_session'] for r in self.requests]
        self.assertEqual(len(http_sessions), 4)
        # Signing happens before the HTTP session, so even lines with other
        # credentials or regions share connection pools.
        self.assertEqual(len({id(s) for s in http_sessions}), 1)
        self.assertEqual(
            [self.access_key(r) for r in self.requests][:2],
            ['DEFAULTACCESSKEY', 'DEVACCESSKEY'],
        )

    def test_connections_not_shared_with_other_http_settings(self):
        self.run_file(
            'dynamodb list-tables\n'
            'dynamodb list-tables --no-verify-ssl\n'
            'dynamodb list-tables --cli-read-timeout 5\n'
            'dynamodb list-tables\n'
        )
        sessions = [r['http_session'] for r in self.requests]
        self.assertIsNot(sessions[0], sessions[1])
        self.assertIsNot(sessions[0], sessions[2])
        self.assertIsNot(sessions[1], sessions[2])
        self.assertIs(sessions[0], sessions[3])
        self.assertFalse(sessions[1]._verify)

    def test_shared_connections_are_closed_after_the_run(self):
        with mock.patch(
            'botocore.httpsession.URLLib3Session.close', autospec=True
        ) as close:
            self.run_file('dynamodb list-tables\nsqs list-queues\n')
        self.assertEqual(close.call_count, 1)
        self.assertIs(close.call_args[0][0], self.requests[0]['http_session'])

    def test_session_handlers_do_not_leak(self):
        # --outfile style handlers registered by one line must not apply to
        # the next one. s3api get-object writes the body to the outfile.
        outfile = os.path.join(self.tempdir, 'out.bin')
        self.add_response(b'object-body')
        self.add_response({'TableNames': []})
        self.run_file(
            f"s3api get-object --bucket b --key k '{outfile}'\n"
            'dynamodb list-tables\n'
        )
        with open(outfile, 'rb') as f:
            self.assertEqual(f.read(), b'object-body')
        self.assertEqual(len(self.requests), 2)

    def test_configure_set_then_use(self):
        self.run_file(
            'configure set region eu-north-1 --profile newprofile\n'
            'configure set aws_access_key_id NEWKEY --profile newprofile\n'
            'configure set aws_secret_access_key s --profile newprofile\n'
            'dynamodb list-tables --profile newprofile\n'
        )
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.access_key(self.requests[0]), 'NEWKEY')
        self.assertEqual(
            self.credential_scope_region(self.requests[0]), 'eu-north-1'
        )


class TestRunAutoPrompt(BaseRunTest):
    def _assert_not_prompted(self, mode):
        self.environ['AWS_CLI_AUTO_PROMPT'] = mode
        path = self.write_commands('dynamodb list-tables\n')
        with mock.patch('awscli.autoprompt.core.AutoPromptDriver') as prompt:
            self.run_cmd(['run', path])
        prompt.assert_not_called()
        self.assertEqual(len(self.requests), 1)

    def test_auto_prompt_on_is_ignored(self):
        self._assert_not_prompted('on')

    def test_auto_prompt_on_partial_is_ignored(self):
        self._assert_not_prompted('on-partial')

    def test_auto_prompt_on_partial_not_reprompted_on_param_error(self):
        self.environ['AWS_CLI_AUTO_PROMPT'] = 'on-partial'
        path = self.write_commands('dynamodb describe-table\n')
        with mock.patch('awscli.autoprompt.core.AutoPromptDriver') as prompt:
            self.run_cmd(['run', path], expected_rc=252)
        prompt.assert_not_called()

    def test_auto_prompt_from_config_is_ignored(self):
        with open(self.config_file, 'a') as f:
            f.write('\n[profile prompt]\ncli_auto_prompt = on\n')
        with open(self.credentials_file, 'a') as f:
            f.write(
                '\n[prompt]\naws_access_key_id = PROMPTKEY\n'
                'aws_secret_access_key = s\n'
            )
        self.environ['AWS_PROFILE'] = 'prompt'
        self.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
        path = self.write_commands('dynamodb list-tables\n')
        with mock.patch('awscli.autoprompt.core.AutoPromptDriver') as prompt:
            self.run_cmd(['run', path])
        prompt.assert_not_called()
        self.assertEqual(self.access_key(self.requests[0]), 'PROMPTKEY')

    def test_auto_prompt_still_applies_to_other_commands(self):
        self.environ['AWS_CLI_AUTO_PROMPT'] = 'on'
        with mock.patch('awscli.autoprompt.core.AutoPromptDriver') as prompt:
            prompt.return_value.prompt_for_args.return_value = [
                'dynamodb',
                'list-tables',
            ]
            self.run_cmd(['dynamodb', 'list-tables'])
        prompt.assert_called_once()


@pytest.mark.parametrize(
    'content',
    [
        'dynamodb list-tables |',
        'dynamodb list-tables &',
        'dynamodb list-tables ;',
        'dynamodb list-tables <x',
        'dynamodb list-tables >x',
        'dynamodb list-tables (',
        'dynamodb list-tables )',
        'dynamodb list-tables `x`',
        'dynamodb list-tables $X',
        'dynamodb list-tables "$X"',
        'dynamodb list-tables "`x`"',
        'dynamodb list-tables ~',
        'dynamodb list-tables \\',
        'aws dynamodb list-tables',
        'run x.txt',
        'dynamodb list-tables --cli-auto-prompt',
        "dynamodb list-tables 'x",
    ],
)
def test_rejected_lines_run_nothing(content, tmp_path):
    path = tmp_path / 'commands.txt'
    path.write_text('dynamodb list-tables\n' + content + '\n')
    environ = {
        'AWS_DATA_PATH': os.environ['AWS_DATA_PATH'],
        'AWS_CONFIG_FILE': '',
        'AWS_SHARED_CREDENTIALS_FILE': '',
        'AWS_ACCESS_KEY_ID': 'access_key',
        'AWS_SECRET_ACCESS_KEY': 'secret_key',
        'AWS_DEFAULT_REGION': 'us-east-1',
        'AWS_EC2_METADATA_DISABLED': 'true',
    }
    with mock.patch('os.environ', environ):
        with mock.patch('botocore.httpsession.URLLib3Session.send') as send:
            entry_point = awscli.clidriver.AWSCLIEntryPoint(create_clidriver())
            with capture_output() as captured:
                rc = entry_point.main(['run', str(path)])
    assert rc == 252, captured.stderr.getvalue()
    assert 'line 2:' in captured.stderr.getvalue()
    send.assert_not_called()
