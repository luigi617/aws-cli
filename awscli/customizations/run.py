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
"""Implements ``aws run <file>``.

Each line of the file is executed as its own AWS CLI command, in order,
inside the current process. Every line gets a fresh botocore session and
CLI driver, so profiles, regions, endpoints, credentials and other
per-command state never carry over from one line to the next. The only
state shared between lines is the warm Python process and the model
loaders, which only read and cache model files.
"""

import logging
import re
import shlex
import sys

from botocore.configprovider import ConstantProvider
from botocore.context import ClientContext, start_as_current_context
from botocore.endpoint import DEFAULT_TIMEOUT
from botocore.history import get_global_history_recorder
from botocore.httpsession import URLLib3Session

from awscli.clidriver import create_clidriver, get_command_name
from awscli.compat import get_stderr_text_writer, get_stdout_text_writer
from awscli.customizations.commands import BasicCommand
from awscli.customizations.exceptions import ParamValidationError
from awscli.errorhandler import construct_entry_point_handlers_chain
from awscli.logger import (
    disable_crt_logging,
    remove_stream_logger,
    set_stream_logger,
)
from awscli.utils import add_metadata_component_to_user_agent_extra

LOG = logging.getLogger(__name__)
HISTORY_RECORDER = get_global_history_recorder()

# Characters that a shell would treat as operators when not quoted.
_SHELL_OPERATOR_CHARS = '|&;<>()`'
# Like a shell, treat "~" after the "=" of an assignment such as "name=",
# "name+=" or "name[i]=", or after a ":" in its value, as a home directory.
# For example "path=~/f" or "path=/a:~/f".
_ASSIGNMENT_TILDE_PREFIX = re.compile(
    r'[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=(.*:)?$'
)
_LOGGERS = ['botocore', 'awscli', 's3transfer', 'urllib3']

DESCRIPTION = """\
Runs AWS CLI commands from a file, one command per line, in a single
AWS CLI process. This avoids starting the AWS CLI again for every
command.

Each non-empty line is one AWS CLI command written without the leading
``aws``, for example ``ec2 describe-vpcs --region us-east-1``. Lines
whose first non-whitespace character is ``#`` are comments. Commands run
sequentially in file order. If a command fails, no further commands are
run and ``aws run`` exits with that command's exit code.

The file is **not** a shell script. Each line is split into arguments
with POSIX shell quoting rules (single quotes, double quotes and
backslash escapes), but nothing is expanded or executed by a shell:

* Pipes, redirections, command separators, subshells and backticks
  (``| & ; < > ( )`` and `````) are rejected unless quoted.
* ``$`` is rejected unless it is inside single quotes or escaped with a
  backslash outside of quotes, because a shell would expand it.
  Environment variables are never expanded.
* An unquoted ``~`` that a shell would expand to a home directory, such
  as ``~/file`` or ``name=~/file``, is rejected. Use a full path instead.
* An unquoted ``#`` at the start of an argument is rejected, because a
  shell would treat the rest of the line as a comment.
* Wildcards and braces are not expanded, but passed on literally. Line
  continuations are not supported.

Every line is checked before any command runs, so a syntax error
anywhere in the file means nothing is run.

Each command is fully isolated from the others: global options such as
``--profile``, ``--region``, ``--endpoint-url`` and ``--output`` only
apply to the line they appear on. Global options given to ``aws run``
itself are not passed to the commands. Those that would change how a
command runs are rejected, and ``--debug`` is passed to every command. Environment variables
and configuration files apply to every command, just as they would when
running each command separately. The output pager is disabled for the
commands (``help`` still uses its pager), and they are never
auto-prompted. Commands may not call
``aws run`` themselves.
"""

EXAMPLES = """\
**To run several commands in one AWS CLI process**

Given a file ``commands.txt`` with the following content::

    # Who am I?
    sts get-caller-identity

    ec2 describe-instances --region us-east-1
    ec2 describe-vpcs --region us-east-1 --query 'Vpcs[].VpcId'
    s3api list-buckets --profile dev

The following ``run`` command runs each of the commands in order::

    aws run commands.txt
"""


def register_run_command(event_handlers):
    event_handlers.register(
        'building-command-table.main', RunCommand.add_command
    )


class RunCommand(BasicCommand):
    NAME = 'run'
    DESCRIPTION = DESCRIPTION
    SYNOPSIS = 'aws run <file>'
    EXAMPLES = EXAMPLES
    ARG_TABLE = [
        {
            'name': 'file',
            'positional_arg': True,
            'synopsis': '<file>',
            'help_text': (
                'The path to a file containing one AWS CLI command per '
                'line, written without the leading <code>aws</code>.'
            ),
        },
    ]

    _is_running = False

    def _run_main(self, parsed_args, parsed_globals):
        if RunCommand._is_running:
            raise ParamValidationError(
                'aws run cannot be called from a command run by aws run.'
            )
        self._validate_globals(parsed_globals)
        cli_options = self._session.get_data('cli').get('options', {})
        commands = parse_commands_file(parsed_args.file, cli_options)
        RunCommand._is_running = True
        try:
            return self._run_commands(commands, cli_options, parsed_globals)
        finally:
            RunCommand._is_running = False

    def _validate_globals(self, parsed_globals):
        # The commands are isolated from the run command itself, so global
        # options given to "aws run" would silently not apply to them.
        # Reject them rather than run commands with an unexpected profile,
        # region or endpoint.
        unsupported = []
        for option, value, default in [
            ('--profile', parsed_globals.profile, None),
            ('--region', parsed_globals.region, None),
            ('--endpoint-url', parsed_globals.endpoint_url, None),
            ('--output', parsed_globals.output, None),
            ('--query', parsed_globals.query, None),
            ('--ca-bundle', parsed_globals.ca_bundle, None),
            ('--cli-binary-format', parsed_globals.cli_binary_format, None),
            ('--cli-error-format', parsed_globals.cli_error_format, None),
            ('--no-verify-ssl', parsed_globals.verify_ssl is False, False),
            ('--no-paginate', parsed_globals.paginate, True),
            ('--no-sign-request', parsed_globals.sign_request, True),
            ('--color', parsed_globals.color, 'auto'),
            (
                '--cli-read-timeout',
                parsed_globals.read_timeout,
                DEFAULT_TIMEOUT,
            ),
            (
                '--cli-connect-timeout',
                parsed_globals.connect_timeout,
                DEFAULT_TIMEOUT,
            ),
        ]:
            if value != default:
                unsupported.append(option)
        if unsupported:
            raise ParamValidationError(
                'The following global options are not supported with aws run: '
                f'{", ".join(unsupported)}. Specify them on each line of the '
                'file instead.'
            )

    def _run_commands(self, commands, cli_options, parsed_globals):
        # Model loaders are shared between all commands, keyed by data_path.
        # Seed them with this session's loader, which is already warm.
        data_loaders = {
            self._session.get_config_variable('data_path'): (
                self._session.get_component('data_loader')
            )
        }
        http_sessions = SharedHTTPSessions()
        try:
            for lineno, argv in commands:
                if parsed_globals.debug:
                    argv = ['--debug'] + argv
                rc = _run_one_command(argv, data_loaders, http_sessions)
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                except BrokenPipeError:
                    return 255
                if rc != 0:
                    # Only the line number is shown, as arguments may be
                    # secret.
                    sys.stderr.write(
                        f'aws run: the command on line {lineno} failed with '
                        f'exit code {rc}. No further commands were run.\n'
                    )
                    return rc
                if get_command_name(argv, cli_options) == 'configure':
                    # "configure add-model" can add models, which cached
                    # loaders would not see.
                    data_loaders.clear()
            return 0
        finally:
            http_sessions.close()


class SharedHTTPSessions:
    """Shares HTTP sessions, and so connection pools, between clients.

    Registered as each command's ``http_session_cls`` session component, so
    clients created by different commands reuse connections. An HTTP
    session is only shared between endpoints created with exactly the same
    HTTP settings (TLS verification, proxies, timeouts, pool size, socket
    options and client certificate). Requests are signed before they reach
    the HTTP session, so sharing it never shares credentials.
    """

    def __init__(self, http_session_cls=URLLib3Session):
        self._http_session_cls = http_session_cls
        self._sessions = {}

    def __call__(self, **kwargs):
        try:
            key = _freeze(kwargs)
            hash(key)
        except TypeError:
            # Settings that cannot be compared safely are never shared.
            return self._http_session_cls(**kwargs)
        if key not in self._sessions:
            self._sessions[key] = self._http_session_cls(**kwargs)
        return self._sessions[key]

    def close(self):
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for http_session in sessions:
            try:
                http_session.close()
            except Exception:
                LOG.debug('Unable to close HTTP session', exc_info=True)


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__,) + tuple(_freeze(v) for v in value)
    return value


def _run_one_command(argv, data_loaders, http_sessions):
    _reset_logging()
    # History is recorded by handlers attached for each command's own
    # session, depending on its profile. Detach the run command's own
    # handlers while the command runs, and restore them afterwards.
    history_handlers = list(HISTORY_RECORDER._handlers)
    history_enabled = HISTORY_RECORDER._enabled
    HISTORY_RECORDER._handlers[:] = []
    HISTORY_RECORDER.disable()
    # Some formatters wrap sys.stdout and sys.stderr (e.g. colorama for
    # colored output), which must not affect the following commands.
    stdout, stderr = sys.stdout, sys.stderr
    try:
        with start_as_current_context(ClientContext()):
            try:
                driver = create_clidriver(argv, data_loaders=data_loaders)
                driver.session.register_component(
                    'http_session_cls', http_sessions
                )
                # Each command would otherwise start its own pager.
                driver.session.get_component(
                    'config_store'
                ).set_config_provider('pager', ConstantProvider(None))
                # Commands run by aws run are never auto-prompted.
                add_metadata_component_to_user_agent_extra(
                    driver.session, 'prompt', 'off'
                )
                rc = driver.main(argv)
            except BaseException as e:
                LOG.debug('Exception caught in aws run', exc_info=True)
                rc = construct_entry_point_handlers_chain().handle_exception(
                    e,
                    stdout=get_stdout_text_writer(),
                    stderr=get_stderr_text_writer(),
                )
            # A command returns None, like a process exiting with
            # sys.exit(None), when it ends without an explicit code.
            if rc is None:
                rc = 0
            HISTORY_RECORDER.record('CLI_RC', rc, 'CLI')
    finally:
        HISTORY_RECORDER._handlers[:] = history_handlers
        HISTORY_RECORDER._enabled = history_enabled
        sys.stdout, sys.stderr = stdout, stderr
    return rc


def _reset_logging():
    # Logging is process wide, so --debug on one line must not affect the
    # following lines. This mirrors CLIDriver._set_logging(False), and also
    # resets the logger levels.
    for logger_name in _LOGGERS:
        remove_stream_logger(logger_name)
        logging.getLogger(logger_name).setLevel(logging.NOTSET)
    disable_crt_logging()
    set_stream_logger(logger_name='awscli', log_level=logging.ERROR)


def parse_commands_file(path, cli_options):
    """Read a commands file and return a list of ``(lineno, argv)``.

    All lines are validated before returning, so that a syntax error
    anywhere in the file is reported before any command runs.
    """
    try:
        with open(path, encoding='utf-8-sig') as f:
            # Universal newlines already turned '\r\n' and '\r' into '\n'.
            # Only split on '\n', so line numbers match what editors show.
            lines = f.read().split('\n')
    except (OSError, UnicodeDecodeError) as e:
        raise ParamValidationError(f'Unable to read file {path}: {e}')
    commands = []
    for lineno, line in enumerate(lines, start=1):
        try:
            argv = parse_command_line(line, cli_options)
        except ValueError as e:
            raise ParamValidationError(f'{path}, line {lineno}: {e}')
        if argv is not None:
            commands.append((lineno, argv))
    return commands


def parse_command_line(line, cli_options):
    """Split one line of a commands file into CLI arguments.

    Returns None for empty and comment lines. Raises ValueError for lines
    that cannot be run.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return None
    _check_shell_syntax(line)
    try:
        argv = shlex.split(stripped, comments=False, posix=True)
    except ValueError as e:
        raise ValueError(f'unable to parse command: {e}')
    command_name = get_command_name(argv, cli_options)
    if command_name is None:
        raise ValueError('no command given')
    if command_name == 'aws':
        raise ValueError("commands must not start with 'aws'")
    if command_name == 'run':
        raise ValueError('aws run cannot be nested')
    if '--cli-auto-prompt' in argv:
        raise ValueError('--cli-auto-prompt is not supported with aws run')
    return argv


def _check_shell_syntax(line):
    # Reject shell syntax that would run or redirect something, start a
    # comment, or expand variables, home directories or command output.
    # Wildcards and braces are passed on literally.
    quote = None
    escaped = False
    word_start = True
    word_begin = 0
    for i, char in enumerate(line):
        if escaped:
            escaped = False
            word_start = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == '\\':
            if quote == '"' and line[i + 1 : i + 2] in ('$', '`'):
                # A shell removes this backslash, but shlex keeps it.
                raise ValueError(
                    f"unsupported shell syntax '\\{line[i + 1]}' at column "
                    f"{i + 1}"
                )
            escaped = True
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char in '$`':
                raise ValueError(
                    f"unsupported shell syntax {char!r} at column {i + 1}"
                )
            continue
        if char in '\'"':
            quote = char
        elif char in _SHELL_OPERATOR_CHARS or char == '$':
            raise ValueError(
                f"unsupported shell syntax {char!r} at column {i + 1}"
            )
        elif char == '#' and word_start:
            raise ValueError(
                f"unsupported inline comment at column {i + 1}; quote '#' "
                "to use it literally"
            )
        elif char == '~' and (
            word_start or _ASSIGNMENT_TILDE_PREFIX.match(line[word_begin:i])
        ):
            raise ValueError(
                f"unsupported shell syntax '~' at column {i + 1}; "
                "use a full path instead"
            )
        if char.isspace():
            word_start = True
            word_begin = i + 1
        else:
            word_start = False
    if escaped:
        raise ValueError('line continuations are not supported')
