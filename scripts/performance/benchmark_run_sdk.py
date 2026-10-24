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
"""SDK side of benchmark_run.py: makes the same calls with one session.

Usage:
    python benchmark_run_sdk.py --mode boto3 --calls calls.json \
        --endpoint-url http://127.0.0.1:PORT

``--mode boto3`` needs boto3 installed (a separate venv). ``--mode
botocore`` uses the AWS CLI's vendored botocore (run it with the AWS CLI
venv), as a same-version reference. Both create one session and one
client per service, and reuse them for every call.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['boto3', 'botocore'], required=True)
    parser.add_argument('--calls', required=True)
    parser.add_argument('--endpoint-url', required=True)
    parser.add_argument('--region', default='us-east-1')
    args = parser.parse_args()

    from benchmark_run_counters import install

    if args.mode == 'boto3':
        import boto3
        import botocore.session

        install(botocore.session.Session)
        session = boto3.Session(region_name=args.region)
        create_client = session.client
    else:
        import awscli  # noqa: F401,I001  (installs the botocore alias)
        import botocore.session

        install(botocore.session.Session)
        session = botocore.session.Session()
        session.set_config_variable('region', args.region)
        create_client = session.create_client

    with open(args.calls) as f:
        calls = json.load(f)
    clients = {}
    for service, method in calls:
        if service not in clients:
            clients[service] = create_client(
                service,
                region_name=args.region,
                endpoint_url=args.endpoint_url,
            )
        getattr(clients[service], method)()
    return 0


if __name__ == '__main__':
    sys.exit(main())
