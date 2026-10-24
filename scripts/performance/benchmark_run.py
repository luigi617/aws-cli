#!/usr/bin/env python
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
"""Benchmark ``aws run`` against separate CLI processes and boto3.

All requests go to a stub HTTP server started by this script, bound to
127.0.0.1 on an ephemeral port. No real AWS endpoint is contacted and only
fake credentials are used.

Modes compared, all making the same N calls (round-robin over sts
GetCallerIdentity, ec2 DescribeInstances, ec2 DescribeVpcs and s3
ListBuckets):

* ``separate``: N processes of ``python -m awscli <command>``.
* ``aws-run``: one ``python -m awscli run commands.txt`` process.
* ``aws-run-unpooled``: the same, with connection pool sharing between
  lines turned off by a benchmark-only patch (see
  ``benchmark_run_cli_wrapper.py``), to measure what pooling saves.
* ``boto3``: one process, one boto3 Session, one client per service
  (needs a separate venv with boto3, see ``--boto3-python``).
* ``botocore``: like ``boto3`` but with the AWS CLI's vendored botocore,
  as a same-version reference.

``--tls`` serves HTTPS with a throwaway self-signed certificate for
127.0.0.1, trusted through ``AWS_CA_BUNDLE``. ``--rtt-ms`` emulates network
latency: the stub waits 2 x RTT before each new connection (TCP and TLS
handshakes) and 1 x RTT before each response. This is an emulation, not a
measurement of a real network.

Example::

    /tmp/awsrun-bench-venv/bin/pip install boto3   # once, separate venv
    .venv/bin/python scripts/performance/benchmark_run.py \\
        --commands 100 --reps 5
"""

import argparse
import http.server
import json
import os
import platform
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLI_WRAPPER = os.path.join(HERE, 'benchmark_run_cli_wrapper.py')
SDK_DRIVER = os.path.join(HERE, 'benchmark_run_sdk.py')
COUNTS_ENV_VAR = 'AWSRUN_BENCH_COUNTS_FILE'
UNPOOLED_ENV_VAR = 'AWSRUN_BENCH_UNPOOLED'
DEFAULT_BOTO3_PYTHON = '/tmp/awsrun-bench-venv/bin/python'
REGION = 'us-east-1'
ACCOUNT = '123456789012'

# (CLI args, SDK service, SDK method)
OPERATIONS = [
    (['sts', 'get-caller-identity'], 'sts', 'get_caller_identity'),
    (['ec2', 'describe-instances'], 'ec2', 'describe_instances'),
    (['ec2', 'describe-vpcs'], 'ec2', 'describe_vpcs'),
    (['s3api', 'list-buckets'], 's3', 'list_buckets'),
]

STS_RESPONSE = f"""<GetCallerIdentityResponse \
xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:iam::{ACCOUNT}:user/bench</Arn>
    <UserId>AIDAEXAMPLEBENCH</UserId>
    <Account>{ACCOUNT}</Account>
  </GetCallerIdentityResult>
  <ResponseMetadata><RequestId>bench-1</RequestId></ResponseMetadata>
</GetCallerIdentityResponse>"""

EC2_INSTANCES_RESPONSE = f"""<DescribeInstancesResponse \
xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <requestId>bench-2</requestId>
  <reservationSet>
    <item>
      <reservationId>r-0123456789abcdef0</reservationId>
      <ownerId>{ACCOUNT}</ownerId>
      <instancesSet>
        <item>
          <instanceId>i-0123456789abcdef0</instanceId>
          <imageId>ami-0123456789abcdef0</imageId>
          <instanceState><code>16</code><name>running</name></instanceState>
          <instanceType>t3.micro</instanceType>
          <placement><availabilityZone>us-east-1a</availabilityZone>
          </placement>
        </item>
      </instancesSet>
    </item>
  </reservationSet>
</DescribeInstancesResponse>"""

EC2_VPCS_RESPONSE = f"""<DescribeVpcsResponse \
xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
  <requestId>bench-3</requestId>
  <vpcSet>
    <item>
      <vpcId>vpc-0123456789abcdef0</vpcId>
      <state>available</state>
      <cidrBlock>10.0.0.0/16</cidrBlock>
      <ownerId>{ACCOUNT}</ownerId>
      <isDefault>false</isDefault>
    </item>
  </vpcSet>
</DescribeVpcsResponse>"""

S3_LIST_BUCKETS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Owner><ID>bench-owner-id</ID></Owner>
  <Buckets>
    <Bucket>
      <Name>bench-bucket</Name>
      <CreationDate>2026-01-01T00:00:00.000Z</CreationDate>
    </Bucket>
  </Buckets>
</ListAllMyBucketsResult>"""

QUERY_RESPONSES = {
    ('sts', 'GetCallerIdentity'): STS_RESPONSE,
    ('ec2', 'DescribeInstances'): EC2_INSTANCES_RESPONSE,
    ('ec2', 'DescribeVpcs'): EC2_VPCS_RESPONSE,
}


# --------------------------------------------------------------------------
# Stub server
# --------------------------------------------------------------------------


class StubStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.requests = 0
            self.connections = 0
            self.server_seconds = 0.0
            self.unexpected = []

    def add_connection(self):
        with self._lock:
            self.connections += 1

    def add_request(self, seconds, unexpected=None):
        with self._lock:
            self.requests += 1
            self.server_seconds += seconds
            if unexpected:
                self.unexpected.append(unexpected)

    def snapshot(self):
        with self._lock:
            return {
                'requests': self.requests,
                'connections': self.connections,
                'server_seconds': self.server_seconds,
                'unexpected': list(self.unexpected),
            }


class StubHandler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 so that clients can keep connections alive.
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        pass

    def _service(self):
        # SigV4: "Credential=AKID/date/region/service/aws4_request".
        auth = self.headers.get('Authorization', '')
        for part in auth.split(','):
            part = part.strip()
            if 'Credential=' in part:
                scope = part.split('Credential=', 1)[1].split('/')
                if len(scope) >= 4:
                    return scope[3]
        return None

    def _handle(self):
        if self.server.rtt:
            time.sleep(self.server.rtt)
        start = time.perf_counter()
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length).decode('utf-8') if length else ''
        service = self._service()
        unexpected = None
        if service == 's3' and self.command == 'GET':
            payload = S3_LIST_BUCKETS_RESPONSE
        else:
            params = dict(p.split('=', 1) for p in body.split('&') if '=' in p)
            payload = QUERY_RESPONSES.get((service, params.get('Action')))
        if payload is None:
            unexpected = f'{self.command} {self.path} {service} {body[:80]}'
            status, payload = 400, '<Error><Code>BenchStub</Code></Error>'
        else:
            status = 200
        data = payload.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/xml')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('x-amzn-RequestId', 'bench-request')
        self.end_headers()
        self.wfile.write(data)
        self.server.stats.add_request(time.perf_counter() - start, unexpected)

    do_GET = _handle
    do_POST = _handle


class StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ssl_context=None, rtt=0.0):
        self.stats = StubStats()
        self.ssl_context = ssl_context
        self.rtt = rtt
        # Loopback only, ephemeral port.
        super().__init__(('127.0.0.1', 0), StubHandler)

    def get_request(self):
        sock, address = super().get_request()
        self.stats.add_connection()
        if self.rtt:
            # Emulated TCP and TLS handshake round trips.
            time.sleep(2 * self.rtt)
        if self.ssl_context is not None:
            sock = self.ssl_context.wrap_socket(sock, server_side=True)
        return sock, address


def make_certificate(tmpdir):
    """Create a throwaway self-signed certificate for 127.0.0.1."""
    cert = os.path.join(tmpdir, 'stub-cert.pem')
    key = os.path.join(tmpdir, 'stub-key.pem')
    subprocess.run(
        [
            'openssl',
            'req',
            '-x509',
            '-newkey',
            'rsa:2048',
            '-nodes',
            '-keyout',
            key,
            '-out',
            cert,
            '-days',
            '1',
            '-subj',
            '/CN=127.0.0.1',
            '-addext',
            'subjectAltName=IP:127.0.0.1',
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return cert, context


def check_listener_is_loopback(port):
    """Use lsof to confirm the stub server listens on 127.0.0.1 only."""
    lsof = shutil.which('lsof') or '/usr/sbin/lsof'
    output = subprocess.run(
        [lsof, '-nP', '-iTCP', '-sTCP:LISTEN', '-a', '-p', str(os.getpid())],
        capture_output=True,
        text=True,
    ).stdout
    lines = [line for line in output.splitlines() if f':{port} ' in line]
    if not lines:
        raise RuntimeError(f'lsof did not show the stub listener:\n{output}')
    for line in lines:
        if f'127.0.0.1:{port} (LISTEN)' not in line:
            raise RuntimeError(f'Stub server is not loopback only: {line}')
    return lines


# --------------------------------------------------------------------------
# Running the modes
# --------------------------------------------------------------------------


def make_env(tmpdir, counts_file=None, ca_bundle=None, unpooled=False):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith('AWS_') and not k.startswith('BOTO')
    }
    config_file = os.path.join(tmpdir, 'config')
    credentials_file = os.path.join(tmpdir, 'credentials')
    env.update(
        {
            'AWS_ACCESS_KEY_ID': 'AKIABENCHFAKEFAKE000',
            'AWS_SECRET_ACCESS_KEY': 'benchFakeSecretKeyNotRealAtAll000000000',
            'AWS_SESSION_TOKEN': 'benchFakeSessionToken',
            'AWS_DEFAULT_REGION': REGION,
            'AWS_REGION': REGION,
            'AWS_CONFIG_FILE': config_file,
            'AWS_SHARED_CREDENTIALS_FILE': credentials_file,
            'AWS_PAGER': '',
            'AWS_EC2_METADATA_DISABLED': 'true',
            'AWS_CLI_AUTO_PROMPT': 'off',
        }
    )
    if counts_file:
        env[COUNTS_ENV_VAR] = counts_file
    if ca_bundle:
        env['AWS_CA_BUNDLE'] = ca_bundle
    if unpooled:
        env[UNPOOLED_ENV_VAR] = '1'
    return env


def run_child(cmd, env, cwd):
    """Run one child; return (seconds, ru_maxrss, rc, stderr)."""
    stderr = tempfile.TemporaryFile()
    start = time.perf_counter()
    proc = subprocess.Popen(
        cmd, env=env, stdout=subprocess.DEVNULL, stderr=stderr, cwd=cwd
    )
    _, status, rusage = os.wait4(proc.pid, 0)
    elapsed = time.perf_counter() - start
    proc.returncode = os.waitstatus_to_exitcode(status)
    stderr.seek(0)
    err = stderr.read().decode('utf-8', 'replace')
    stderr.close()
    return elapsed, rusage.ru_maxrss, proc.returncode, err


def read_counts(counts_file):
    totals = {'sessions': 0, 'clients': 0}
    if not os.path.exists(counts_file):
        return None
    with open(counts_file) as f:
        for line in f:
            counts = json.loads(line)
            for key in totals:
                totals[key] += counts[key]
    os.remove(counts_file)
    return totals


class Benchmark:
    def __init__(self, args, server, tmpdir, ca_bundle=None):
        self.args = args
        self.server = server
        self.tmpdir = tmpdir
        self.ca_bundle = ca_bundle
        scheme = 'https' if ca_bundle else 'http'
        self.endpoint = f'{scheme}://127.0.0.1:{server.server_address[1]}'
        self.counts_file = os.path.join(tmpdir, 'counts.jsonl')
        for name in ('config', 'credentials'):
            with open(os.path.join(tmpdir, name), 'w') as f:
                if name == 'config':
                    f.write(f'[default]\nregion = {REGION}\n')

    def operations(self, n):
        return [OPERATIONS[i % len(OPERATIONS)] for i in range(n)]

    def cli_args(self, op):
        return op[0] + ['--endpoint-url', self.endpoint, '--region', REGION]

    def write_inputs(self, n):
        commands_file = os.path.join(self.tmpdir, f'commands-{n}.txt')
        calls_file = os.path.join(self.tmpdir, f'calls-{n}.json')
        with open(commands_file, 'w') as f:
            for op in self.operations(n):
                f.write(' '.join(self.cli_args(op)) + '\n')
        with open(calls_file, 'w') as f:
            json.dump([[op[1], op[2]] for op in self.operations(n)], f)
        return commands_file, calls_file

    def modes(self):
        modes = ['separate', 'aws-run', 'aws-run-unpooled']
        if not self.args.skip_boto3:
            modes.append('boto3')
        modes.append('botocore')
        return modes

    def run_mode(self, mode, n, instrumented):
        """Run one mode once with n calls and return a result dict."""
        commands_file, calls_file = self.write_inputs(n)
        env = make_env(
            self.tmpdir,
            self.counts_file if instrumented else None,
            ca_bundle=self.ca_bundle,
            unpooled=mode == 'aws-run-unpooled',
        )
        cli = self.args.cli_python
        if mode == 'separate':
            runner = CLI_WRAPPER if instrumented else None
            cmds = []
            for op in self.operations(n):
                prefix = [cli, runner] if runner else [cli, '-m', 'awscli']
                cmds.append(prefix + self.cli_args(op))
        elif mode == 'aws-run':
            prefix = (
                [cli, CLI_WRAPPER] if instrumented else [cli, '-m', 'awscli']
            )
            cmds = [prefix + ['run', commands_file]]
        elif mode == 'aws-run-unpooled':
            # The wrapper applies the benchmark-only patch.
            cmds = [[cli, CLI_WRAPPER, 'run', commands_file]]
        else:
            python = self.args.boto3_python if mode == 'boto3' else cli
            cmds = [
                [
                    python,
                    SDK_DRIVER,
                    '--mode',
                    mode,
                    '--calls',
                    calls_file,
                    '--endpoint-url',
                    self.endpoint,
                    '--region',
                    REGION,
                ]
            ]
        self.server.stats.reset()
        total = 0.0
        rss = []
        for cmd in cmds:
            seconds, maxrss, rc, err = run_child(cmd, env, self.tmpdir)
            total += seconds
            rss.append(maxrss)
            if rc != 0:
                raise RuntimeError(
                    f'{mode}: {" ".join(cmd)} exited {rc}:\n{err}'
                )
        stats = self.server.stats.snapshot()
        if stats['unexpected']:
            raise RuntimeError(f'{mode}: unexpected requests {stats}')
        if stats['requests'] != n:
            raise RuntimeError(
                f'{mode}: expected {n} requests, stub saw {stats["requests"]}'
            )
        result = {
            'mode': mode,
            'n': n,
            'seconds': total,
            'max_rss': max(rss),
            'mean_rss': statistics.mean(rss),
            'processes': len(cmds),
            **stats,
        }
        if instrumented:
            result['counts'] = read_counts(self.counts_file)
        return result

    def measure(self, n, reps):
        """Run all modes reps times, interleaved; return per-mode lists."""
        results = {mode: [] for mode in self.modes()}
        for rep in range(reps):
            for mode in self.modes():
                res = self.run_mode(mode, n, instrumented=False)
                results[mode].append(res)
                log(
                    f'  n={n} rep {rep + 1}/{reps} {mode}: '
                    f'{res["seconds"]:.3f}s'
                )
        return results


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def rss_mib(value):
    # ru_maxrss is in bytes on macOS and in KiB on Linux.
    if sys.platform == 'darwin':
        return value / (1024 * 1024)
    return value / 1024


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def summarize(results, counts, startup):
    rows = []
    boto3_median = None
    for mode, runs in results.items():
        times = [r['seconds'] for r in runs]
        median = statistics.median(times)
        if mode == 'boto3':
            boto3_median = median
        rows.append((mode, runs, times, median))
    reference = boto3_median
    if reference is None:
        reference = statistics.median(
            r['seconds'] for r in results['botocore']
        )
    lines = [
        '| Mode | Processes | Median total (s) | Min total (s) '
        '| Median per command (ms) | Per command vs '
        f'{"boto3" if boto3_median is not None else "botocore"} (ms) '
        '| Marginal per command (ms) | Stub server time (ms total) '
        '| Requests | TCP connections | Sessions | Clients '
        '| Peak RSS max (MiB) | Peak RSS mean (MiB) |',
        '|' + '---|' * 14,
    ]
    for mode, runs, times, median in rows:
        n = runs[0]['n']
        count = counts.get(mode) or {}
        marginal = ''
        if mode in startup:
            start = startup[mode]
            marginal = f'{(median - start) / (n - 1) * 1000:.2f}'
        lines.append(
            f'| {mode} | {runs[0]["processes"]} | {median:.3f} '
            f'| {min(times):.3f} | {median / n * 1000:.2f} '
            f'| {(median - reference) / n * 1000:+.2f} '
            f'| {marginal} '
            f'| {statistics.median(r["server_seconds"] for r in runs) * 1000:.1f} '
            f'| {runs[0]["requests"]} | {runs[0]["connections"]} '
            f'| {count.get("sessions", "")} | {count.get("clients", "")} '
            f'| {rss_mib(max(r["max_rss"] for r in runs)):.1f} '
            f'| {rss_mib(statistics.mean(r["mean_rss"] for r in runs)):.1f} |'
        )
    return '\n'.join(lines)


def startup_table(single):
    lines = [
        '| Mode | Median total for 1 command (s) | Min (s) |',
        '|---|---|---|',
    ]
    for mode, runs in single.items():
        times = [r['seconds'] for r in runs]
        lines.append(
            f'| {mode} | {statistics.median(times):.3f} | {min(times):.3f} |'
        )
    return '\n'.join(lines)


def raw_table(results):
    lines = ['| Mode | Rep | Total (s) |', '|---|---|---|']
    for mode, runs in results.items():
        for i, r in enumerate(runs, start=1):
            lines.append(f'| {mode} | {i} | {r["seconds"]:.3f} |')
    return '\n'.join(lines)


def versions(args):
    info = {}
    out = subprocess.run(
        [
            args.cli_python,
            '-c',
            'import awscli, botocore; print(awscli.__version__, '
            'botocore.__version__)',
        ],
        capture_output=True,
        text=True,
        cwd=HERE,
    ).stdout.split()
    if len(out) == 2:
        info['awscli'], info['vendored botocore'] = out
    if not args.skip_boto3:
        out = subprocess.run(
            [
                args.boto3_python,
                '-c',
                'import boto3, botocore; print(boto3.__version__, '
                'botocore.__version__)',
            ],
            capture_output=True,
            text=True,
        ).stdout.split()
        if len(out) == 2:
            info['boto3'], info['boto3 botocore'] = out
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--commands',
        type=int,
        default=100,
        help='Number of commands/calls per run (>= 2).',
    )
    parser.add_argument(
        '--reps', type=int, default=5, help='Repetitions of each mode.'
    )
    parser.add_argument(
        '--skip-boto3', action='store_true', help='Do not run the boto3 mode.'
    )
    parser.add_argument(
        '--boto3-python',
        default=DEFAULT_BOTO3_PYTHON,
        help='Python interpreter of a venv with boto3.',
    )
    parser.add_argument(
        '--cli-python',
        default=sys.executable,
        help='Python interpreter of the AWS CLI venv.',
    )
    parser.add_argument(
        '--skip-startup',
        action='store_true',
        help='Do not measure the 1-command startup runs.',
    )
    parser.add_argument(
        '--tls',
        action='store_true',
        help='Serve HTTPS with a throwaway self-signed certificate.',
    )
    parser.add_argument(
        '--rtt-ms',
        type=float,
        default=0.0,
        help='Emulated network round trip time in milliseconds.',
    )
    args = parser.parse_args()
    if args.commands < 2:
        parser.error('--commands must be at least 2')
    if not args.skip_boto3 and not os.path.exists(args.boto3_python):
        parser.error(
            f'{args.boto3_python} does not exist. Create it with: '
            'python3 -m venv /tmp/awsrun-bench-venv && '
            '/tmp/awsrun-bench-venv/bin/pip install boto3 '
            '(or pass --skip-boto3)'
        )

    tmpdir = tempfile.mkdtemp(prefix='awsrun-bench-')
    ca_bundle = ssl_context = None
    if args.tls:
        ca_bundle, ssl_context = make_certificate(tmpdir)
    server = StubServer(ssl_context=ssl_context, rtt=args.rtt_ms / 1000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        lsof_lines = check_listener_is_loopback(port)
        log(f'Stub server listening (lsof): {lsof_lines[0]}')
        bench = Benchmark(args, server, tmpdir, ca_bundle=ca_bundle)

        log('Warm-up and instrumented runs (counts, not timed)...')
        counts = {}
        for mode in bench.modes():
            res = bench.run_mode(mode, args.commands, instrumented=True)
            counts[mode] = res['counts']
            log(f'  {mode}: {res["counts"]}')

        log(f'Timed runs, {args.commands} commands...')
        results = bench.measure(args.commands, args.reps)

        startup = {}
        single = {}
        if not args.skip_startup:
            log('Timed runs, 1 command...')
            single = bench.measure(1, args.reps)
            startup = {
                mode: statistics.median(r['seconds'] for r in runs)
                for mode, runs in single.items()
            }
        lsof_lines = check_listener_is_loopback(port)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print('## Environment\n')
    print(f'- uname: `{" ".join(platform.uname())}`')
    print(f'- python: `{sys.version.split()[0]}` ({args.cli_python})')
    for key, value in versions(args).items():
        print(f'- {key}: `{value}`')
    print(f'- stub listener (lsof): `{" ".join(lsof_lines[0].split())}`')
    print(f'- commands: {args.commands}, reps: {args.reps}')
    print(
        f'- transport: {"HTTPS (self-signed, AWS_CA_BUNDLE)" if args.tls else "HTTP"}'
        f', emulated RTT: {args.rtt_ms:g} ms\n'
    )
    print('## Summary\n')
    print(summarize(results, counts, startup))
    if single:
        print('\n## Startup (1 command)\n')
        print(startup_table(single))
    print('\n## Raw timings\n')
    print(raw_table(results))
    return 0


if __name__ == '__main__':
    sys.exit(main())
