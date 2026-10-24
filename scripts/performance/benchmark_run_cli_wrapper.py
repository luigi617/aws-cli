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
"""Runs the AWS CLI like ``python -m awscli``, counting botocore objects.

Usage: python benchmark_run_cli_wrapper.py <aws cli args...>

With ``AWSRUN_BENCH_UNPOOLED=1``, connection pool sharing between the
lines of ``aws run`` is turned off, to measure what it saves.

Importing ``awscli`` installs the import alias that makes ``botocore``
resolve to the vendored ``awscli.botocore``, so ``botocore.session`` below
is the same module the CLI uses.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import awscli  # noqa: E402,F401,I001  (installs the botocore alias)
import awscli.clidriver  # noqa: E402
import botocore.session  # noqa: E402
from benchmark_run_counters import install  # noqa: E402


UNPOOLED_ENV_VAR = 'AWSRUN_BENCH_UNPOOLED'


def disable_connection_pool_sharing():
    """Benchmark-only: give every endpoint its own HTTP session again."""
    from awscli.customizations import run

    def new_session(self, **kwargs):
        return self._http_session_cls(**kwargs)

    run.SharedHTTPSessions.__call__ = new_session


def main():
    install(botocore.session.Session)
    if os.environ.get(UNPOOLED_ENV_VAR):
        disable_connection_pool_sharing()
    sys.argv = ['aws'] + sys.argv[1:]
    return awscli.clidriver.main()


if __name__ == '__main__':
    sys.exit(main())
