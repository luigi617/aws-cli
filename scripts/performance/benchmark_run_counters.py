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
"""Counts botocore Session and client creation for benchmark_run.py.

Used by both the AWS CLI wrapper and the SDK driver. The counts are
appended as one JSON line to the file named by the
``AWSRUN_BENCH_COUNTS_FILE`` environment variable when the process exits.
"""

import atexit
import json
import os

COUNTS_ENV_VAR = 'AWSRUN_BENCH_COUNTS_FILE'


def install(session_cls):
    """Patch ``session_cls`` to count __init__ and create_client calls."""
    counts = {'sessions': 0, 'clients': 0}
    original_init = session_cls.__init__
    original_create_client = session_cls.create_client

    def counting_init(self, *args, **kwargs):
        counts['sessions'] += 1
        return original_init(self, *args, **kwargs)

    def counting_create_client(self, *args, **kwargs):
        counts['clients'] += 1
        return original_create_client(self, *args, **kwargs)

    session_cls.__init__ = counting_init
    session_cls.create_client = counting_create_client

    counts_file = os.environ.get(COUNTS_ENV_VAR)
    if counts_file:

        def write_counts():
            with open(counts_file, 'a') as f:
                f.write(json.dumps(counts) + '\n')

        atexit.register(write_counts)
    return counts
