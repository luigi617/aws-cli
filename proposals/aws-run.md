# Running multiple commands in one process with `aws run`

Proposal         | Metadata
---------------- | -------------
**Status**       | Draft
**Created**      | 23-October-2026

## Abstract

This document describes `aws run <file>`, a built-in command that runs a
file of AWS CLI commands, one per line, sequentially inside a single AWS
CLI process. Scripts that call the AWS CLI many times pay the process
start-up and import cost on every call; `aws run` pays it once while
keeping each command's behavior the same as if it were run on its own.

## Usage

```
aws run <file>
```

Example file (also at [assets/aws-run/commands.txt](assets/aws-run/commands.txt)):

```
# Example commands file for "aws run". Each line is one AWS CLI command
# written without the leading "aws". Lines starting with # are comments.
sts get-caller-identity

ec2 describe-instances --region us-east-1
ec2 describe-vpcs --region us-east-1
ec2 describe-vpcs --region us-east-1 --query 'Vpcs[].VpcId' --output text
s3api list-buckets
```

```
$ aws run commands.txt
```

Each command's stdout and stderr are written to the process's stdout and
stderr exactly as if the command had been run on its own, in file order.

## File format

The file is **not** a shell script. The rules below are implemented in
`awscli/customizations/run.py` (`parse_commands_file`,
`parse_command_line`, `_check_shell_syntax`).

* **Encoding.** The file is read as UTF-8. A leading UTF-8 byte order mark
  is ignored (`utf-8-sig`). A file that cannot be opened or decoded is an
  error.
* **Lines.** `\n`, `\r\n` and `\r` line endings are accepted (universal
  newlines), and the file is split only on those. Other characters that
  `str.splitlines()` treats as line breaks (form feed, U+2028, ...) stay
  part of the line. Line numbers in messages are 1-based physical line
  numbers.
* **Blank lines and comments.** Leading and trailing whitespace is
  stripped. Empty lines and lines whose first non-whitespace character is
  `#` are skipped. Inline comments are not supported: an unquoted `#` at
  the start of an argument, which a shell would treat as a comment, is
  rejected. A `#` inside an argument (`s3://bucket/a#b`) or quoted is an
  ordinary character.
* **One command per line**, without the leading `aws`, e.g.
  `ec2 describe-vpcs --region us-east-1`. Line continuations are not
  supported.
* **Quoting.** The line is split into arguments with Python's
  `shlex.split(line, posix=True)`: single quotes, double quotes and
  backslash escapes behave like POSIX shell quoting. Nothing is expanded.
* **Rejected shell syntax.** Before splitting, a quote-aware scan rejects
  shell syntax that would run or redirect something, start a comment, or
  expand variables, home directories or command output:
  * `|` `&` `;` `<` `>` `(` `)` and backtick outside quotes (pipes,
    redirections, command separators, subshells, command substitution);
  * `$` outside quotes unless escaped (`\$`), and `$`, `\$`, backtick or
    `` \` `` inside double quotes (variable, arithmetic and command
    expansion; shlex would keep the backslash that a shell removes);
  * an unquoted `#` at the start of an argument (comment);
  * an unquoted `~` that a shell would expand to a home directory: at the
    start of an argument, or after the `=` of an assignment-like word
    (`name=~/f`, `name+=~/f`, `name[i]=~/f`) or after a `:` in its value
    (`path=/a:~/f`); use a full path instead;
  * a trailing unescaped backslash (line continuation).

  Any of these characters may be used literally by quoting them as a
  shell would require (`'$HOME'`, `"a|b"`, `\;`). Wildcards (`*`, `?`,
  `[...]`) and braces are **not** rejected: they are passed through
  literally, which can differ from a shell that would expand them.
* **Other rejected lines:** unbalanced quotes; a line with only global
  options and no command; a command named `aws` (the leading `aws` must be
  omitted); a nested `run` command; and `--cli-auto-prompt`.
* **Validated up front.** Every line is parsed and validated before any
  command runs. A problem on any line is reported as
  `<file>, line N: <reason>` (with the column for rejected shell syntax)
  and exits with 252 without running anything.

## Exit codes and fail-fast

* `0` if every command succeeded (a command that returns no explicit code
  counts as success, like `sys.exit(None)`).
* `252` if the file cannot be read, any line fails validation, or an
  unsupported global option was given to `aws run` itself. No commands are
  run.
* Otherwise, commands run in order and the first command that returns a
  non-zero exit code stops the run. `aws run` exits with **that command's
  exit code** and writes to stderr:

  ```
  aws run: the command on line N failed with exit code RC. No further commands were run.
  ```

  Only the line number is shown, because arguments may contain secrets.
* `255` if stdout or stderr is closed (broken pipe) between commands.
* Exceptions escaping a command (including Ctrl-C, exit code 130) are
  handled by the same entry-point error handlers as a normal invocation
  and then stop the run like any other failure.

## Global options

* Global options on a line (`--profile`, `--region`, `--endpoint-url`,
  `--output`, `--query`, `--debug`, `--no-sign-request`, ...) apply **only
  to that line**.
* Global options given to `aws run` itself are not inherited by the
  commands, so they are rejected with 252 rather than silently ignored:
  `--profile`, `--region`, `--endpoint-url`, `--output`, `--query`,
  `--ca-bundle`, `--cli-binary-format`, `--cli-error-format`,
  `--no-verify-ssl`, `--no-paginate`, `--no-sign-request`, `--color`,
  `--cli-read-timeout`, `--cli-connect-timeout`. An option given with its
  default value (e.g. `--color auto`) cannot be told apart from an omitted
  one and is accepted. `--debug` is forwarded to every line
  (`aws --debug run file`). `--no-cli-pager`, `--cli-auto-prompt` and
  `--no-cli-auto-prompt` are accepted and have no effect, because the pager
  is always off for lines and auto-prompt is always off for `aws run`.
* Environment variables (`AWS_PROFILE`, `AWS_REGION`, `AWS_CONFIG_FILE`,
  ...) and the config and credentials files apply to every line, exactly
  as they would if each command were run separately.
* The output pager is disabled for every line, and lines are never
  auto-prompted. `help` lines still use the help pager
  (`MANPAGER`/`PAGER`, by default `less -R`).
  Auto-prompt is also forced off for `aws run` itself (via
  `_NO_AUTO_PROMPT_COMMANDS` in `clidriver.py`), because re-prompting and
  re-running the batch could repeat commands that already modified
  resources.

## What is reused vs reinitialized

Each line is run with a brand new botocore `Session` and `CLIDriver`
created by `create_clidriver(argv, data_loaders=...)`.

| State | Per line | Reason |
| --- | --- | --- |
| Python process, imported modules | Reused | This is the start-up cost `aws run` exists to avoid. |
| Model `Loader` and its cached JSON (service models, `endpoints.json`, partitions, `cli.json`, ...) | Reused, keyed by the resolved `data_path` | Loaders only read and cache files; sharing them is safe as long as the search path is the same. The cache is cleared after a successful line whose top-level command is `configure` (before alias expansion), since `configure add-model` can add models. |
| botocore `Session` and config store | New | Holds the profile, region and other resolved settings; reuse leaked one line's settings into the next. |
| Credential resolver and resolved credentials | New | Reuse signed later lines with an earlier profile's credentials. |
| `CLIDriver`, command and argument tables | New | Built from the session (and its plugins/aliases). |
| Event emitter and registered handlers | New | Commands register per-invocation handlers (e.g. `--outfile`, `--no-sign-request`). |
| User agent | New | Set on each new session (`_set_user_agent_for_session`), with `md/prompt#off` and the line's own `md/command#...`. |
| Error handler chain | New | Bound to the session for structured error output. |
| Default client config | New | Derived from per-line options (timeouts, SSL, ...). |
| Service clients | New | See below; clients capture handlers and config at creation time. |
| HTTP connection pools (`URLLib3Session`) | Reused, keyed by HTTP settings | Shared by endpoints created with exactly the same TLS verification/CA bundle, proxies, proxy config, timeouts, pool size, socket options and client certificate (`SharedHTTPSessions` in `run.py`, through the new `http_session_cls` session component). Requests are signed before they reach the HTTP session, so credentials are never shared. Closed when the run ends. |
| Logging configuration | Reset | Logging is process wide; `--debug` on one line must not affect the next. |
| History recorder handlers | Detached and restored | The run command's own handlers are detached while a line runs, so each line is recorded only if its own profile enables history; they are restored afterwards. |
| `sys.stdout` / `sys.stderr` | Restored | Colored output wraps them (colorama); the wrapper must not affect later lines. |
| Telemetry `ClientContext` | New | Each line is reported as its own invocation. |

## Why client caching and session reuse were deferred

Reusing a session (or caching clients across lines) was prototyped and
rejected for this version because the isolation failures were real and
silent:

* A reused session signed a later `--profile other` line with the
  **earlier profile's access key**, because credentials had already been
  resolved and cached on the session.
* `--region` and `--no-sign-request` from one line stuck to the following
  lines.
* Per-line session handlers, such as the after-call handler that
  implements `--outfile`, were missed by cached clients, because
  `client.meta.events` is a copy of the session's emitter taken when the
  client is created.
* The gain is small: once the process and loaders are warm,
  `create_client` costs roughly 1-3 ms.

A fresh session per line makes isolation correct by construction, and
the shared loader keeps most of the warm-process benefit.

## Credential behavior

* Providers that cache on disk keep working across lines exactly as they
  do across separate processes: assume-role and web identity
  (`~/.aws/cli/cache`), IAM Identity Center/SSO (token cache in
  `~/.aws/sso/cache`, role credentials in `~/.aws/cli/cache`), and
  `aws login` (login token cache).
* Providers without a disk cache are re-resolved on every line:
  `credential_process` is re-executed, and IMDS and container credentials
  are re-fetched.

## Performance

Measured with `scripts/performance/benchmark_run.py` against a loopback
stub server (100 commands, median of 5 runs; full method and raw numbers
in [assets/aws-run/benchmark-results.md](assets/aws-run/benchmark-results.md)):

| Mode | Total (s) | Marginal per command (ms) | Sessions | Clients | TCP connections | Peak RSS (MiB) |
| --- | --- | --- | --- | --- | --- | --- |
| 100 separate `python -m awscli` processes | 39.35 | 394 | 100 | 100 | 100 | 96 (max per process) |
| `aws run` | 1.68 | 13.3 | 101 | 100 | 100 | 119 |
| boto3, one session and reused clients | 0.41 | 1.7 | 1 | 3 | 3 | 90 |

To make per-line cost low, `CLIOperationCaller` now creates its
`OutputStreamFactory` (which copies `os.environ`) on first use instead of
once per operation whenever a command table is built. This took `aws run`
from 3.80 s to 1.68 s for the same benchmark and also helps single
commands.

A second round added connection pool sharing between lines, which
brought the TCP connections for 100 lines from 100 to 1 and, with an
emulated 20 ms network round trip over HTTPS, cut 100 commands from about
10.5 s to about 5.1 s (boto3: about 3.5 s). Two CPU micro-optimizations were also measured and
not kept. See "Round 2" in the results file.

## Known limitations

* Connections are only reused between lines with identical HTTP
  settings; a line with e.g. `--no-verify-ssl`, `--ca-bundle` or
  `--cli-read-timeout` gets its own connection pool.
* `credential_process`, IMDS and container credentials are fetched again
  for every line.
* Global options on `aws run` are not inherited by lines (they are
  rejected, except `--debug`).
* The output pager is disabled and auto-prompt is not available.
* `help` lines run (and use the help pager), but `aws run` is not intended
  for interactive use.
* A line containing only global options, such as `--version`, is rejected
  ("no command given").
* A user-defined alias named `run` overrides the built-in command, as for
  any other command.
* Once a line uses the CRT S3 transfer client, the CRT S3 cross-process
  lock is held by the process for the rest of the run, so concurrent AWS
  CLI processes fall back to the classic transfer client in the meantime.
* stdin is shared by all lines: a line that reads stdin (e.g. `s3 cp -`)
  consumes it for every later line.
* It is not a shell: no variables, pipes, redirection, conditionals,
  globbing or command substitution. Use a shell script for those.
* Commands run strictly sequentially.

## Future work

* **CPU micro-optimizations that were measured and not kept:** caching
  the "handler accepts `**kwargs`" check (about 0.7 ms per line) and
  reusing client method objects between clients of the same model (about
  1 ms per line). Both change core botocore code paths for a small gain,
  and the first had subtle edge cases (`partialmethod`, partials with a
  `__signature__`).
* **Building only the command table that is used.** Each line builds
  the main and service command tables and parsers, although it uses one
  operation. Measured at up to about 2 ms per line in total; deferred
  because customizations modify these tables through
  `building-command-table.*` events.
* **In-memory credential cache** keyed by profile and the relevant
  environment, so that `credential_process` and IMDS/container credentials
  are not re-fetched on every line.
* Parallel execution, variables and control flow are explicitly out of
  scope.
