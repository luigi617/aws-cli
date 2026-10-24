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

from botocore.endpoint import Endpoint
from botocore.history import get_global_history_recorder

from awscli.alias import AliasLoader, register_alias_commands
from awscli.customizations.dryrun import refuse_api_call
from awscli.testutils import (
    BaseAWSCommandParamsTest,
    CapturedRenderer,
    FileCreator,
    mock,
    skip_if_windows,
)


class BaseDryRunTest(BaseAWSCommandParamsTest):
    def setUp(self):
        super().setUp()
        self.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
        self.sign_patch = mock.patch('botocore.signers.RequestSigner.sign')
        self.sign_mock = self.sign_patch.start()
        self.presign_patch = mock.patch(
            'botocore.signers.RequestSigner.generate_presigned_url'
        )
        self.presign_mock = self.presign_patch.start()
        self.files = FileCreator()

    def tearDown(self):
        super().tearDown()
        self.sign_patch.stop()
        self.presign_patch.stop()
        self.files.remove_all()

    def run_dry_run(self, cmd, expected_rc=0):
        stdout, stderr, rc = self.run_cmd(cmd, expected_rc)
        self.assert_nothing_sent()
        return stdout, stderr

    def run_dry_run_json(self, cmd):
        stdout, _ = self.run_dry_run(cmd)
        return json.loads(stdout)

    def assert_nothing_sent(self):
        self._get_make_request_mock().assert_not_called()
        self.sign_mock.assert_not_called()

    def _get_make_request_mock(self):
        # Endpoint.make_request is patched with a mock by run_cmd().
        return Endpoint.make_request


class TestDryRun(BaseDryRunTest):
    def test_query_protocol(self):
        doc = self.run_dry_run_json(
            'ec2 describe-instances --instance-ids i-123 --cli-dry-run'
        )
        self.assertEqual(
            doc,
            {
                'DryRun': True,
                'Service': 'ec2',
                'Operation': 'DescribeInstances',
                'Region': 'us-east-1',
                'Parameters': {'InstanceIds': ['i-123']},
                'Request': {
                    'Method': 'POST',
                    'Url': 'https://ec2.us-east-1.amazonaws.com/',
                    'Headers': mock.ANY,
                    'Body': {
                        'Action': 'DescribeInstances',
                        'Version': '2016-11-15',
                        'InstanceId.1': 'i-123',
                    },
                },
                'ExpectedOutput': mock.ANY,
            },
        )
        headers = {k.lower() for k in doc['Request']['Headers']}
        self.assertNotIn('authorization', headers)
        self.assertNotIn('x-amz-date', headers)

    def test_operation_dry_run_is_sent_in_request(self):
        doc = self.run_dry_run_json(
            'ec2 run-instances --image-id ami-12345 --dry-run --cli-dry-run'
        )
        self.assertIs(doc['Parameters']['DryRun'], True)
        self.assertEqual(doc['Request']['Body']['DryRun'], 'true')

    def test_operation_dry_run_abbreviation_is_not_cli_dry_run(self):
        doc = self.run_dry_run_json(
            'ec2 run-instances --image-id ami-12345 --dry --cli-dry-run'
        )
        self.assertIs(doc['Parameters']['DryRun'], True)

    def test_operation_dry_run_alone_is_unaffected(self):
        self.parsed_response = {'Reservations': []}
        stdout, _, _ = self.run_cmd(
            'ec2 run-instances --image-id ami-12345 --dry-run'
        )
        self.assertNotIn('DryRun', json.loads(stdout or '{}'))
        self.assertEqual(self.last_kwargs['DryRun'], True)
        self._get_make_request_mock().assert_called()

    def test_json_protocol(self):
        doc = self.run_dry_run_json(
            'dynamodb describe-table --table-name foo --cli-dry-run'
        )
        request = doc['Request']
        self.assertEqual(
            request['Headers']['X-Amz-Target'],
            'DynamoDB_20120810.DescribeTable',
        )
        self.assertEqual(json.loads(request['Body']), {'TableName': 'foo'})

    def test_rest_json_protocol(self):
        doc = self.run_dry_run_json(
            'lambda get-function --function-name myfunc --cli-dry-run'
        )
        self.assertEqual(doc['Request']['Method'], 'GET')
        self.assertEqual(
            doc['Request']['Url'],
            'https://lambda.us-east-1.amazonaws.com'
            '/2015-03-31/functions/myfunc',
        )
        self.assertIsNone(doc['Request']['Body'])

    def test_rest_xml_protocol(self):
        doc = self.run_dry_run_json(
            's3api list-objects-v2 --bucket mybucket --prefix foo '
            '--cli-dry-run'
        )
        self.assertEqual(doc['Service'], 's3')
        self.assertEqual(doc['Operation'], 'ListObjectsV2')
        self.assertEqual(doc['Request']['Method'], 'GET')
        self.assertIn('mybucket', doc['Request']['Url'])
        self.assertIn('list-type=2', doc['Request']['Url'])
        self.assertIn('prefix=foo', doc['Request']['Url'])

    def test_paginated_operation_captures_first_request(self):
        doc = self.run_dry_run_json(
            'iam list-users --page-size 5 --cli-dry-run'
        )
        self.assertEqual(doc['Parameters'], {'MaxItems': 5})
        self.assertEqual(doc['Request']['Body']['MaxItems'], 5)
        self.assertEqual(len(self.operations_called), 1)

    def test_max_items_is_not_sent(self):
        doc = self.run_dry_run_json(
            'iam list-users --max-items 1 --cli-dry-run'
        )
        self.assertEqual(doc['Parameters'], {})
        self.assertEqual(len(self.operations_called), 1)

    def test_no_paginate(self):
        doc = self.run_dry_run_json(
            'iam list-users --max-items 3 --no-paginate --cli-dry-run'
        )
        self.assertEqual(doc['Parameters'], {'MaxItems': 3})

    def test_cli_input_json_is_merged(self):
        doc = self.run_dry_run_json(
            [
                'ec2',
                'describe-instances',
                '--cli-input-json',
                json.dumps({'InstanceIds': ['i-123']}),
                '--cli-dry-run',
            ]
        )
        self.assertEqual(doc['Parameters'], {'InstanceIds': ['i-123']})

    def test_generate_cli_skeleton_takes_precedence(self):
        stdout, _ = self.run_dry_run(
            'ec2 describe-instances --generate-cli-skeleton --cli-dry-run'
        )
        skeleton = json.loads(stdout)
        self.assertIn('InstanceIds', skeleton)
        self.assertNotIn('Request', skeleton)

    def test_generate_cli_skeleton_output_takes_precedence(self):
        stdout, _ = self.run_dry_run(
            'ec2 describe-instances --generate-cli-skeleton output '
            '--cli-dry-run'
        )
        self.assertIn('Reservations', json.loads(stdout))

    def test_query_and_output_apply_to_expected_output(self):
        doc = self.run_dry_run_json(
            'ec2 describe-instances --cli-dry-run --output json '
            '--query Reservations[0].{ReservationId:ReservationId}'
        )
        self.assertEqual(doc['Operation'], 'DescribeInstances')
        self.assertEqual(
            doc['ExpectedOutput'], {'ReservationId': 'ReservationId'}
        )

    def test_text_output(self):
        doc = self.run_dry_run_json(
            'sts get-caller-identity --cli-dry-run --output text'
        )
        self.assertEqual(
            doc['ExpectedOutput'], [['Account', 'Arn', 'UserId']]
        )

    def test_table_output(self):
        doc = self.run_dry_run_json(
            'sts get-caller-identity --cli-dry-run --output table'
        )
        self.assertIn('|      GetCallerIdentity     |', doc['ExpectedOutput'])

    def test_table_output_has_no_color_codes(self):
        doc = self.run_dry_run_json(
            'sts get-caller-identity --cli-dry-run --output table --color on'
        )
        self.assertNotIn('\x1b', ''.join(doc['ExpectedOutput']))

    def test_yaml_output_is_embedded_as_data(self):
        doc = self.run_dry_run_json(
            'sts get-caller-identity --cli-dry-run --output yaml'
        )
        self.assertEqual(
            doc['ExpectedOutput'],
            {'Account': 'Account', 'Arn': 'Arn', 'UserId': 'UserId'},
        )

    def test_output_off(self):
        doc = self.run_dry_run_json(
            'ec2 describe-instances --cli-dry-run --output off'
        )
        self.assertEqual(doc['Operation'], 'DescribeInstances')
        self.assertIsNone(doc['ExpectedOutput'])

    def test_pagination_tokens_not_in_expected_output(self):
        doc = self.run_dry_run_json('iam list-users --cli-dry-run')
        expected_output = doc['ExpectedOutput']
        self.assertIn('Users', expected_output)
        self.assertNotIn('Marker', expected_output)

    def test_pagination_tokens_in_expected_output_without_paginate(self):
        doc = self.run_dry_run_json(
            'iam list-users --no-paginate --cli-dry-run'
        )
        self.assertIn('Marker', doc['ExpectedOutput'])

    def test_region_and_endpoint_url(self):
        doc = self.run_dry_run_json(
            'ec2 describe-instances --region eu-west-1 '
            '--endpoint-url http://localhost:1234 --cli-dry-run'
        )
        self.assertEqual(doc['Region'], 'eu-west-1')
        self.assertEqual(doc['Request']['Url'], 'http://localhost:1234/')

    def test_missing_required_argument(self):
        _, stderr = self.run_dry_run(
            'lambda get-function --cli-dry-run', expected_rc=252
        )
        self.assertIn('--function-name', stderr)

    def test_no_credentials(self):
        del self.environ['AWS_ACCESS_KEY_ID']
        del self.environ['AWS_SECRET_ACCESS_KEY']
        doc = self.run_dry_run_json('ec2 describe-instances --cli-dry-run')
        self.assertEqual(doc['Operation'], 'DescribeInstances')

    def test_no_region(self):
        del self.environ['AWS_DEFAULT_REGION']
        _, stderr = self.run_dry_run(
            'ec2 describe-instances --cli-dry-run', expected_rc=253
        )
        self.assertIn('region', stderr)

    def test_sensitive_values_are_redacted(self):
        stdout, _ = self.run_dry_run(
            'secretsmanager create-secret --name foo '
            '--secret-string hunter2 --cli-dry-run'
        )
        self.assertNotIn('hunter2', stdout)
        doc = json.loads(stdout)
        self.assertEqual(doc['Parameters']['Name'], 'foo')
        self.assertEqual(doc['Parameters']['SecretString'], '****')
        self.assertEqual(doc['Request']['Body'], '****')

    def test_secret_names_not_modeled_as_sensitive_are_redacted(self):
        for cmd in [
            'docdb create-db-cluster --db-cluster-identifier c '
            '--engine docdb --master-username u '
            '--master-user-password hunter2',
            'elasticache create-replication-group '
            '--replication-group-id r --replication-group-description d '
            '--auth-token hunter2',
            'mq create-user --broker-id b --username u --password hunter2',
        ]:
            stdout, _ = self.run_dry_run(f'{cmd} --cli-dry-run')
            self.assertNotIn('hunter2', stdout)
            self.assertIn('****', stdout)

    def test_mfa_header_is_redacted(self):
        stdout, _ = self.run_dry_run(
            [
                's3api',
                'delete-object',
                '--bucket',
                'b',
                '--key',
                'k',
                '--mfa',
                'arn:aws:iam::123:mfa/u 123456',
                '--cli-dry-run',
            ]
        )
        self.assertNotIn('123456', stdout)
        doc = json.loads(stdout)
        self.assertEqual(doc['Request']['Headers']['x-amz-mfa'], '****')

    def test_sensitive_header_is_redacted(self):
        key = 'a' * 32
        stdout, _ = self.run_dry_run(
            f's3api head-object --bucket mybucket --key foo '
            f'--sse-customer-algorithm AES256 --sse-customer-key {key} '
            f'--cli-dry-run'
        )
        self.assertNotIn(key, stdout)
        doc = json.loads(stdout)
        headers = doc['Request']['Headers']
        self.assertEqual(
            headers['x-amz-server-side-encryption-customer-key'], '****'
        )
        # The key's MD5 is masked consistently in the parameters and headers.
        self.assertEqual(doc['Parameters']['SSECustomerKeyMD5'], '****')
        self.assertEqual(
            headers['x-amz-server-side-encryption-customer-key-MD5'], '****'
        )

    def test_streaming_body_is_not_read(self):
        filename = self.files.create_file('body.txt', 'file contents')
        stdout, _ = self.run_dry_run(
            f's3api put-object --bucket mybucket --key foo '
            f'--body {filename} --cli-dry-run'
        )
        self.assertNotIn('file contents', stdout)
        doc = json.loads(stdout)
        self.assertEqual(doc['Parameters']['Body'], '<streaming body>')
        self.assertEqual(doc['Request']['Body'], '<streaming body>')

    def test_presigned_url_is_not_generated(self):
        doc = self.run_dry_run_json(
            'ec2 copy-snapshot --source-region us-west-2 '
            '--source-snapshot-id snap-123 --cli-dry-run'
        )
        body = doc['Request']['Body']
        self.assertEqual(body['PresignedUrl'], '<presigned url omitted>')
        self.assertEqual(body['DestinationRegion'], 'us-east-1')
        self.presign_mock.assert_not_called()

    def test_rds_presigned_url_is_not_generated(self):
        del self.environ['AWS_ACCESS_KEY_ID']
        del self.environ['AWS_SECRET_ACCESS_KEY']
        doc = self.run_dry_run_json(
            'rds copy-db-snapshot --source-region us-west-2 '
            '--source-db-snapshot-identifier arn:aws:rds:us-west-2:123:'
            'snapshot:s --target-db-snapshot-identifier t --cli-dry-run'
        )
        body = doc['Request']['Body']
        self.assertEqual(body['PreSignedUrl'], '<presigned url omitted>')
        self.assertNotIn('SourceRegion', body)
        self.presign_mock.assert_not_called()

    def test_before_call_headers_are_included(self):
        doc = self.run_dry_run_json('apigateway get-rest-apis --cli-dry-run')
        self.assertEqual(
            doc['Request']['Headers']['Accept'], 'application/json'
        )
        doc = self.run_dry_run_json(
            'glacier list-vaults --account-id - --cli-dry-run'
        )
        self.assertIn('x-amz-glacier-version', doc['Request']['Headers'])

    @mock.patch('botocore.utils.calculate_sha256')
    @mock.patch('botocore.utils.calculate_tree_hash')
    def test_glacier_streaming_body_is_not_read_for_checksums(
        self, tree_hash, sha256
    ):
        filename = self.files.create_file('archive', 'archive contents')
        doc = self.run_dry_run_json(
            f'glacier upload-archive --account-id - --vault-name v '
            f'--body {filename} --cli-dry-run'
        )
        tree_hash.assert_not_called()
        sha256.assert_not_called()
        self.assertEqual(
            doc['Request']['Headers']['x-amz-sha256-tree-hash'],
            '<not computed>',
        )
        self.assertEqual(doc['Request']['Body'], '<streaming body>')

    def test_not_recorded_as_api_call_in_history(self):
        recorder = get_global_history_recorder()
        handler = mock.Mock()
        was_enabled = recorder._enabled
        recorder.add_handler(handler)
        recorder.enable()
        try:
            self.run_dry_run(
                'secretsmanager create-secret --name foo '
                '--secret-string hunter2 --cli-dry-run'
            )
        finally:
            recorder._enabled = was_enabled
            recorder._handlers.remove(handler)
        event_types = [c.args[0] for c in handler.emit.call_args_list]
        self.assertNotIn('API_CALL', event_types)

    @mock.patch('subprocess.check_output')
    @mock.patch('subprocess.check_call')
    def test_ssm_start_session_does_not_launch_plugin(
        self, check_call, check_output
    ):
        doc = self.run_dry_run_json(
            'ssm start-session --target i-123 --cli-dry-run'
        )
        check_call.assert_not_called()
        check_output.assert_not_called()
        self.assertEqual(doc['Operation'], 'StartSession')

    @mock.patch('subprocess.check_output')
    @mock.patch('subprocess.check_call')
    def test_ecs_execute_command_does_not_launch_plugin(
        self, check_call, check_output
    ):
        doc = self.run_dry_run_json(
            'ecs execute-command --cluster c --task t --command ls '
            '--interactive --cli-dry-run'
        )
        check_call.assert_not_called()
        check_output.assert_not_called()
        self.assertEqual(doc['Operation'], 'ExecuteCommand')

    def test_override_from_other_handler_prevents_output(self):
        stdout, stderr = self.run_dry_run(
            'cloudsearchdomain search --search-query x --cli-dry-run',
            expected_rc=252,
        )
        self.assertEqual(stdout, '')
        self.assertIn('--endpoint-url is required', stderr)

    def test_driver_can_be_reused_after_dry_run(self):
        self.run_dry_run('ec2 describe-instances --cli-dry-run')
        self.parsed_response = {'Regions': []}
        self.run_cmd('ec2 describe-regions')
        self._get_make_request_mock().assert_called()

    def test_output_file_is_not_created(self):
        outfile = os.path.join(self.files.rootdir, 'outfile')
        doc = self.run_dry_run_json(
            f's3api get-object --bucket mybucket --key foo {outfile} '
            f'--cli-dry-run'
        )
        self.assertEqual(doc['Operation'], 'GetObject')
        self.assertFalse(os.path.exists(outfile))
        # The streamed body is written to the outfile, not the output.
        expected_output = doc['ExpectedOutput']
        self.assertNotIn('Body', expected_output)
        self.assertEqual(expected_output['ETag'], 'ETag')

    def test_expected_output(self):
        doc = self.run_dry_run_json(
            'ec2 describe-instances --instance-ids i-123 --cli-dry-run'
        )
        expected_output = doc['ExpectedOutput']
        reservation = expected_output['Reservations'][0]
        self.assertEqual(reservation['ReservationId'], 'ReservationId')
        self.assertEqual(
            reservation['Instances'][0]['InstanceId'], 'InstanceId'
        )

    def test_expected_output_without_output_shape(self):
        doc = self.run_dry_run_json(
            's3api delete-bucket --bucket b --cli-dry-run'
        )
        self.assertIsNone(doc['ExpectedOutput'])

    def test_help_still_works(self):
        renderer = CapturedRenderer()
        with mock.patch('awscli.help.get_renderer', return_value=renderer):
            self.run_dry_run(
                'ec2 describe-instances help --cli-dry-run', expected_rc=None
            )
            self.assertIn('describe-instances', renderer.rendered_contents)

    def test_custom_command_help_still_works(self):
        renderer = CapturedRenderer()
        with mock.patch('awscli.help.get_renderer', return_value=renderer):
            self.run_dry_run('s3 cp help --cli-dry-run', expected_rc=None)
            self.assertIn('--dryrun', renderer.rendered_contents)


class TestDryRunNotSupported(BaseDryRunTest):
    def test_s3_cp_is_refused(self):
        filename = self.files.create_file('foo.txt', 'contents')
        _, stderr = self.run_dry_run(
            f's3 cp {filename} s3://bucket/foo --cli-dry-run',
            expected_rc=252,
        )
        self.assertIn("--cli-dry-run is not supported for 'aws s3 cp'", stderr)
        self.assertIn('--dryrun', stderr)

    def test_configure_set_is_refused(self):
        config_file = self.environ['AWS_CONFIG_FILE']
        with open(config_file) as f:
            original = f.read()
        _, stderr = self.run_dry_run(
            'configure set region us-west-2 --cli-dry-run', expected_rc=252
        )
        self.assertIn('aws configure set', stderr)
        with open(config_file) as f:
            self.assertEqual(f.read(), original)

    def test_waiter_is_refused(self):
        with mock.patch('time.sleep') as sleep:
            _, stderr = self.run_dry_run(
                'ec2 wait instance-running --instance-ids i-123 --cli-dry-run',
                expected_rc=252,
            )
        sleep.assert_not_called()
        self.assertIn(
            "--cli-dry-run is not supported for "
            "'aws ec2 wait instance-running'",
            stderr,
        )

    def test_waiter_generate_cli_skeleton_takes_precedence(self):
        stdout, _ = self.run_dry_run(
            'ec2 wait instance-running --generate-cli-skeleton --cli-dry-run'
        )
        self.assertIn('InstanceIds', json.loads(stdout))

    def test_other_api_calls_are_refused(self):
        # A command that makes a call other than the one being dry run, here
        # simulated by a customization calling a separate client, is refused
        # before the request is sent.
        def make_other_call(parsed_globals, **kwargs):
            client = self.driver.session.create_client(
                'sts', region_name='us-east-1'
            )
            client.get_caller_identity()

        self.driver.session.register(
            'calling-command.ec2.describe-instances', make_other_call
        )
        _, stderr = self.run_dry_run(
            'ec2 describe-instances --cli-dry-run', expected_rc=252
        )
        self.assertIn('GetCallerIdentity', stderr)

    def test_credential_provider_calls_are_allowed(self):
        model = self.driver.session.get_service_model('sts').operation_model(
            'AssumeRole'
        )
        self.assertIsNone(refuse_api_call(model=model))


class TestDryRunAliases(BaseDryRunTest):
    def setUp(self):
        super().setUp()
        self.alias_file = self.files.create_file('alias', '[toplevel]\n')
        self.driver.alias_loader = AliasLoader(self.alias_file)
        event_emitter = self.driver.session.get_component('event_emitter')
        event_emitter.unregister(
            'building-command-table', unique_id='cli-alias-injector'
        )
        register_alias_commands(event_emitter, alias_filename=self.alias_file)

    def add_alias(self, alias_name, alias_value):
        with open(self.alias_file, 'a+') as f:
            f.write(f'{alias_name} = {alias_value}\n')

    def test_internal_alias(self):
        self.add_alias('instances', 'ec2 describe-instances')
        doc = self.run_dry_run_json('instances --cli-dry-run')
        self.assertEqual(doc['Operation'], 'DescribeInstances')

    def test_cli_dry_run_in_alias_value(self):
        self.add_alias('instances', 'ec2 describe-instances --cli-dry-run')
        doc = self.run_dry_run_json('instances')
        self.assertEqual(doc['Operation'], 'DescribeInstances')

    def test_alias_without_flag_keeps_cli_dry_run(self):
        # Global args in the alias value are applied to the parsed globals
        # without clearing --cli-dry-run given on the command line.
        self.add_alias('west', 'ec2 describe-instances --region us-west-2')
        doc = self.run_dry_run_json('west --cli-dry-run')
        self.assertEqual(doc['Region'], 'us-west-2')

    @skip_if_windows('Uses a POSIX shell command')
    def test_external_alias_is_refused(self):
        marker = os.path.join(self.files.rootdir, 'marker')
        self.add_alias('touchit', f'!touch {marker}')
        _, stderr = self.run_dry_run('touchit --cli-dry-run', expected_rc=252)
        self.assertIn("external alias 'touchit'", stderr)
        self.assertFalse(os.path.exists(marker))
