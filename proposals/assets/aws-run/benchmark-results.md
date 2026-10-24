# `aws run` benchmark results

All numbers below were measured on 2026-10-23 with
`scripts/performance/benchmark_run.py`. Nothing here is estimated or
extrapolated. All requests went to a stub HTTP server started by the
script on `127.0.0.1` (ephemeral port). No real AWS endpoint was contacted
and only fake credentials were used.

## Environment

- Machine: Apple M4 Pro, 14 cores, 48 GiB RAM
- uname: `Darwin 842f57813fdd 25.6.0 Darwin Kernel Version 25.6.0: Tue Aug 18 17:48:46 PDT 2026; root:xnu-12377.161.15.700.19~2/RELEASE_ARM64_T6041 arm64 arm`
- Python: 3.14.5 (Homebrew), AWS CLI venv `.venv/bin/python`
- awscli: 2.38.5 (source checkout, branch `explore-aws-run`), vendored botocore `2.0.0dev155`
- boto3 1.43.118 / botocore 1.43.118, in a separate venv `/tmp/awsrun-bench-venv`
- Stub listener during the run (`lsof -nP -iTCP -sTCP:LISTEN`, run
  externally while the benchmark ran):
  `Python 27057 luigiliu 3u IPv4 ... TCP 127.0.0.1:56960 (LISTEN)`
  (the script also asserts this itself via `lsof -p <pid>` at start and end)

## How to reproduce

```bash
python3 -m venv /tmp/awsrun-bench-venv
/tmp/awsrun-bench-venv/bin/pip install boto3
.venv/bin/python scripts/performance/benchmark_run.py --commands 100 --reps 5
```

Options: `--commands N` (calls per run), `--reps R`, `--skip-boto3`,
`--boto3-python PATH` (default `/tmp/awsrun-bench-venv/bin/python`),
`--cli-python PATH`, `--skip-startup`.

## What is compared

N = 100 calls, round-robin over `sts get-caller-identity`,
`ec2 describe-instances`, `ec2 describe-vpcs`, `s3api list-buckets`
(25 each), each with `--endpoint-url http://127.0.0.1:PORT --region
us-east-1`.

| Mode | What runs |
|---|---|
| separate | 100 processes of `.venv/bin/python -m awscli <command>` |
| aws-run | 1 process of `.venv/bin/python -m awscli run commands.txt` |
| boto3 | 1 process, one `boto3.Session`, one client per service (3 clients), reused for all calls |
| botocore | Same as boto3 but with the CLI's vendored botocore, as a same-version reference |

Environment for every child: fake `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN`, temp `AWS_CONFIG_FILE` and
`AWS_SHARED_CREDENTIALS_FILE`, `AWS_PAGER=''`, `AWS_DEFAULT_REGION=us-east-1`,
`AWS_EC2_METADATA_DISABLED=true`; all other `AWS_*` variables from the
parent removed. CLI stdout goes to `/dev/null`.

Notes on method:

- Wall time is measured by the parent around each child (`Popen` +
  `os.wait4`); for `separate` it is the sum over the 100 processes.
  Modes are interleaved within each repetition.
- One untimed, instrumented run of each mode happens first (warm-up and
  counting). Session/client counts come from that run, through a wrapper
  (`benchmark_run_cli_wrapper.py`) that patches
  `botocore.session.Session.__init__` and `Session.create_client` and then
  calls `awscli.clidriver.main()`. Timed runs use plain `python -m awscli`.
  In the CLI venv `botocore` is the vendored `awscli.botocore` (importing
  `awscli` installs that alias), so the patch hits the CLI's class.
- Peak RSS is each child's `ru_maxrss` from `os.wait4` (macOS reports
  bytes; converted to MiB). For `separate`, "max" is the largest of the 100
  processes and "mean" the average.
- "Per command vs boto3" = (median total - boto3 median total) / N.
- "Marginal per command" = (median total for N=100 - median total for N=1)
  / 99, which removes process startup and shows the cost of each extra
  command.
- The stub validates that it received exactly N expected requests per run.
- `python -m awscli` from a source checkout is used, not a frozen
  (PyInstaller) `aws` binary. A frozen binary has different startup cost,
  so the absolute `separate` numbers would differ; the per-line cost
  inside `aws run` should be much less affected.

## Results (N = 100, 5 repetitions)

| Mode | Processes | Median total (s) | Min total (s) | Median per command (ms) | Per command vs boto3 (ms) | Marginal per command (ms) | Stub server time (ms total) | Requests | TCP connections | Sessions | Clients | Peak RSS max (MiB) | Peak RSS mean (MiB) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| separate | 100 | 39.348 | 39.117 | 393.48 | +389.36 | 393.89 | 7.6 | 100 | 100 | 100 | 100 | 95.5 | 82.8 |
| aws-run | 1 | 1.684 | 1.679 | 16.84 | +12.72 | 13.26 | 4.1 | 100 | 100 | 101 | 100 | 119.1 | 118.8 |
| boto3 | 1 | 0.412 | 0.405 | 4.12 | +0.00 | 1.74 | 2.7 | 100 | 3 | 1 | 3 | 89.7 | 89.7 |
| botocore | 1 | 0.352 | 0.335 | 3.52 | -0.60 | 1.39 | 2.7 | 100 | 3 | 1 | 3 | 88.4 | 86.5 |

### Startup (1 command, 5 repetitions)

| Mode | Median total for 1 command (s) | Min (s) |
|---|---|---|
| separate | 0.354 | 0.353 |
| aws-run | 0.372 | 0.353 |
| boto3 | 0.240 | 0.228 |
| botocore | 0.215 | 0.213 |

### Raw timings (N = 100)

| Mode | Rep | Total (s) |
|---|---|---|
| separate | 1 | 40.254 |
| separate | 2 | 39.348 |
| separate | 3 | 40.937 |
| separate | 4 | 39.248 |
| separate | 5 | 39.117 |
| aws-run | 1 | 1.683 |
| aws-run | 2 | 1.689 |
| aws-run | 3 | 1.779 |
| aws-run | 4 | 1.684 |
| aws-run | 5 | 1.679 |
| boto3 | 1 | 0.410 |
| boto3 | 2 | 0.405 |
| boto3 | 3 | 0.412 |
| boto3 | 4 | 0.429 |
| boto3 | 5 | 0.421 |
| botocore | 1 | 0.350 |
| botocore | 2 | 0.335 |
| botocore | 3 | 0.397 |
| botocore | 4 | 0.352 |
| botocore | 5 | 0.392 |

## Interpretation

- **`aws run` vs separate processes:** 1.68 s vs 39.35 s median for 100
  commands, about 23x faster. Each extra command costs about 13 ms in
  `aws run` vs about 394 ms as a new process. Almost all of the
  separate-process cost is interpreter and CLI startup.
- **`aws run` with a single command** costs about the same as one plain
  command (0.372 s vs 0.354 s median, both 0.353 s minimum).
- **`aws run` vs boto3:** boto3 is about 4.1x faster for this workload
  (0.41 s vs 1.68 s): about 13 ms vs 1.7 ms marginal cost per call. That
  gap is the price of the isolation design: every line builds a new
  botocore Session, CLIDriver, command tables, argument parser and client,
  and formats output. boto3 builds one session and three clients and
  reuses them.
- **Same-version reference:** the vendored botocore with one session and
  reused clients (1.39 ms marginal per call) is as fast as boto3
  (1.74 ms), so the gap is in the CLI layer and per-line setup, not in
  the botocore version.
- **Sessions/clients:** `aws run` creates 1 + N sessions (the run command's
  own plus one per line) and N clients, as designed. `separate` also creates
  N sessions and N clients (one of each per process). boto3/botocore create
  1 session and 3 clients.
- **Connection reuse:** boto3/botocore used 3 TCP connections for 100
  requests (one per client, kept alive). `aws run` opened 100 connections,
  one per line, because each line has its own client and connection pool.
  On loopback this costs little; against a real endpoint each new
  connection also pays a TCP + TLS handshake, which this benchmark does not
  measure.
- **Stub server time** was 3 to 8 ms in total for 100 requests (under
  0.1 ms per request), so the totals are essentially all client-side cost.
- **Memory:** `aws run` peaked at about 119 MiB RSS, vs about 83 MiB mean
  (96 MiB max) per separate process and about 90 MiB for boto3.

## Effect of the lazy `OutputStreamFactory` change

An earlier run of the same benchmark (same machine, same day, N = 100,
5 repetitions) measured `aws run` at 3.801 s median (33.9 ms marginal per
command, 257 MiB peak RSS); `separate` was 45.257 s and boto3 0.454 s in
that run. Profiling it showed most of the per-line time in
`ServiceCommand._create_command_table`: it creates one
`CLIOperationCaller` per operation of the service (43,625 for 100
lines), and each one created an `OutputStreamFactory`, which copies
`os.environ`. That copy took 5.9 s of a 9.3 s profile.

`CLIOperationCaller` now creates its `OutputStreamFactory` on first use
(`awscli/clidriver.py`), so building a command table no longer copies the
environment once per operation. With that change `aws run` went from
3.80 s to 1.68 s (33.9 to 13.3 ms marginal per command) and peak RSS from
257 to 119 MiB. A single `aws ec2 ...` command also builds the EC2 command
table, so it benefits too, once per process.

The earlier run also checked that sessions do not accumulate: over 25, 100
and 200 lines, a weakref to every `Session` showed 0 still alive at exit
after `gc.collect()`.

## Round 2: connection pool sharing

`aws run` now shares HTTP connection pools between lines:

- `botocore/session.py`, `client.py` and `args.py`: a new
  `http_session_cls` session component chooses the HTTP session for new
  endpoints. The default is `URLLib3Session`, and `create_endpoint` is
  called exactly as before unless another callable is registered.
- `aws run` registers `SharedHTTPSessions` (`awscli/customizations/run.py`)
  for every line. It returns one shared `URLLib3Session` per distinct set of
  HTTP settings (TLS verification/CA bundle, proxies, proxy config,
  timeouts, pool size, socket options, client certificate), and closes them
  all when the run ends. Requests are signed before they reach the HTTP
  session, so lines never share credentials.

`aws-run-unpooled` below is the same code with pool sharing turned off by a
benchmark-only patch (`AWSRUN_BENCH_UNPOOLED=1`, see
`benchmark_run_cli_wrapper.py`), measured in the same run. Compare modes
within a table: overall machine speed varied between rounds (`separate`
took 39 to 44 s for the same work).

The HTTP and HTTPS runs used the source checkout; the emulated-latency run
used an identical copy of the same commit (`61f627c8f`) through
`PYTHONPATH`, with the same virtualenv.

### Plain HTTP (`--commands 100 --reps 5`)

| Mode | Median total (s) | Marginal per command (ms) | TCP connections | Sessions | Clients | Peak RSS (MiB) |
|---|---|---|---|---|---|---|
| separate | 40.829 | 408.70 | 100 | 100 | 100 | 95.5 (max per process) |
| aws-run | 1.776 | 13.88 | 1 | 101 | 100 | 120.3 |
| aws-run-unpooled | 1.985 | 16.02 | 100 | 101 | 100 | 119.6 |
| boto3 | 0.452 | 2.14 | 3 | 1 | 3 | 92.4 |
| botocore | 0.379 | 1.54 | 3 | 1 | 3 | 86.2 |

### HTTPS, self-signed certificate (`--tls`)

| Mode | Median total (s) | Marginal per command (ms) | TCP connections | Peak RSS (MiB) |
|---|---|---|---|---|
| separate | 41.746 | 417.39 | 100 | 96.3 (max per process) |
| aws-run | 1.858 | 14.99 | 1 | 121.8 |
| aws-run-unpooled | 1.952 | 15.97 | 100 | 121.1 |
| boto3 | 0.415 | 1.77 | 3 | 91.0 |
| botocore | 0.367 | 1.38 | 3 | 87.2 |

### HTTPS with an emulated 20 ms round trip (`--tls --rtt-ms 20`)

The stub waits 40 ms before each new connection (TCP and TLS handshakes)
and 20 ms before each response. This emulates latency; it is not a
measurement of a real network or of real AWS endpoints.

| Mode | Median total (s) | Marginal per command (ms) | TCP connections | Peak RSS (MiB) |
|---|---|---|---|---|
| separate | 49.763 | 497.38 | 100 | 96.3 (max per process) |
| aws-run | 5.096 | 46.31 | 1 | 120.9 |
| aws-run-unpooled | 10.469 | 100.72 | 100 | 119.7 |
| boto3 | 3.479 | 31.52 | 3 | 90.6 |
| botocore | 3.393 | 30.98 | 3 | 87.2 |

Startup for a single command was unchanged (HTTP: separate 0.367 s,
aws-run 0.402 s median).

### Interpretation

- With pooling, `aws run` uses **one** TCP connection for all 100 lines
  (every service goes to the same stub host and every line has the same
  HTTP settings). boto3 uses 3, one per client.
- On loopback, pooling saves 1 to 2.6 ms per line across runs (2.1 ms over
  HTTP and 1.0 ms over HTTPS in this round, 1.3 and 2.6 ms in an earlier
  round), which is close to run-to-run noise.
- With the emulated 20 ms round trip, pooling halves the time: 10.47 s to
  5.10 s for 100 commands, about 54 ms per line. `aws run` is then about
  10x faster than separate processes and about 1.5x slower than boto3.
- `separate` cannot benefit from pooling: each process has its own
  connections.

### Measured and not kept

Two CPU optimizations were implemented, measured in-process (mean of four
lines with `URLLib3Session.send` stubbed) and then reverted, because they
change core botocore code paths for a small gain:

| Change | Mean per line |
|---|---|
| Baseline at the time | 9.6 to 9.7 ms |
| Cache the handler "accepts `**kwargs`" check per function code | 8.6 to 9.0 ms |
| Also reuse client method objects for clients of the same loaded model | 7.7 to 8.0 ms |

An independent review also found two edge cases in the first one
(`functools.partialmethod` handlers, and partials with their own
`__signature__`). Building only the command table of the operation being
run was measured at up to about 2 ms per line and not implemented, because
customizations modify these tables through `building-command-table.*`
events.
