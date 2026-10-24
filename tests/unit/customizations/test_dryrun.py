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
import argparse
import io

import pytest
from botocore.hooks import HierarchicalEmitter
from botocore.model import ShapeResolver

from awscli.customizations.dryrun import (
    MAX_BODY_SIZE,
    REDACTED,
    _redact_url,
    enable_dry_run,
    redact_headers,
    redact_parameters,
    render_body,
)
from awscli.testutils import mock

SHAPES = {
    'Input': {
        'type': 'structure',
        'members': {
            'Name': {'shape': 'String'},
            'Secret': {'shape': 'SensitiveString'},
            'Token': {
                'shape': 'SensitiveString',
                'location': 'header',
                'locationName': 'x-amz-token',
            },
            'Path': {
                'shape': 'SensitiveString',
                'location': 'uri',
                'locationName': 'Path',
            },
            'Nested': {'shape': 'Nested'},
            'Items': {'shape': 'NestedList'},
            'Tags': {'shape': 'SensitiveMap'},
            'Credentials': {'shape': 'SensitiveStructure'},
        },
    },
    'Nested': {
        'type': 'structure',
        'members': {
            'Password': {'shape': 'SensitiveString'},
            'Child': {'shape': 'Nested'},
            'Value': {'shape': 'String'},
        },
    },
    'NestedList': {'type': 'list', 'member': {'shape': 'Nested'}},
    'SensitiveMap': {
        'type': 'map',
        'key': {'shape': 'String'},
        'value': {'shape': 'SensitiveString'},
    },
    'SensitiveStructure': {
        'type': 'structure',
        'sensitive': True,
        'members': {'Key': {'shape': 'String'}},
    },
    'String': {'type': 'string'},
    'SensitiveString': {'type': 'string', 'sensitive': True},
}


@pytest.fixture
def input_shape():
    return ShapeResolver(SHAPES).get_shape_by_name('Input')


class TestRedactParameters:
    def test_no_sensitive_values(self, input_shape):
        params = {'Name': 'foo', 'Nested': {'Value': 'bar'}}
        redacted, sensitive = redact_parameters(params, input_shape)
        assert redacted == params
        assert sensitive == []

    def test_top_level_sensitive_value(self, input_shape):
        redacted, sensitive = redact_parameters(
            {'Name': 'foo', 'Secret': 'hunter2'}, input_shape
        )
        assert redacted == {'Name': 'foo', 'Secret': REDACTED}
        assert [m.name for m in sensitive] == ['SensitiveString']

    def test_nested_structures_lists_and_maps(self, input_shape):
        params = {
            'Nested': {
                'Value': 'a',
                'Child': {'Password': 'p1', 'Value': 'b'},
            },
            'Items': [{'Password': 'p2'}, {'Value': 'c'}],
            'Tags': {'key': 'p3'},
            'Credentials': {'Key': 'p4'},
        }
        redacted, sensitive = redact_parameters(params, input_shape)
        assert redacted == {
            'Nested': {
                'Value': 'a',
                'Child': {'Password': REDACTED, 'Value': 'b'},
            },
            'Items': [{'Password': REDACTED}, {'Value': 'c'}],
            'Tags': {'key': REDACTED},
            'Credentials': REDACTED,
        }
        assert len(sensitive) == 4
        # The input is not modified.
        assert params['Nested']['Child']['Password'] == 'p1'

    def test_unmodeled_keys_are_kept(self, input_shape):
        redacted, sensitive = redact_parameters({'Other': 'x'}, input_shape)
        assert redacted == {'Other': 'x'}
        assert sensitive == []

    @pytest.mark.parametrize(
        'name',
        [
            'MasterUserPassword',
            'AuthToken',
            'HsmPartitionPassword',
            'ServiceAccountPassword',
            'SSECustomerKeyMD5',
            'MFA',
        ],
    )
    def test_secret_names_are_redacted_without_sensitive_trait(self, name):
        shape = ShapeResolver(
            {
                'Input': {
                    'type': 'structure',
                    'members': {
                        name: {'shape': 'String'},
                        'Users': {'shape': 'Users'},
                    },
                },
                'Users': {'type': 'list', 'member': {'shape': 'User'}},
                'User': {
                    'type': 'structure',
                    'members': {name: {'shape': 'String'}},
                },
                'String': {'type': 'string'},
            }
        ).get_shape_by_name('Input')
        redacted, sensitive = redact_parameters(
            {name: 'secret', 'Users': [{name: 'secret'}]}, shape
        )
        assert redacted == {name: REDACTED, 'Users': [{name: REDACTED}]}
        assert len(sensitive) == 2

    @pytest.mark.parametrize(
        'name, value',
        [
            ('MasterUserPasswordSecretArn', 'arn:aws:secret'),
            ('AuthTokenUpdateStrategy', 'ROTATE'),
            ('ManageMasterUserPassword', True),
            ('PasswordLength', 12),
        ],
    )
    def test_non_secret_names_are_kept(self, name, value):
        shape = ShapeResolver(
            {
                'Input': {
                    'type': 'structure',
                    'members': {name: {'shape': 'String'}},
                },
                'String': {'type': 'string'},
            }
        ).get_shape_by_name('Input')
        assert redact_parameters({name: value}, shape) == ({name: value}, [])

    def test_map_keys_are_not_treated_as_member_names(self, input_shape):
        shape = ShapeResolver(
            {
                'Input': {
                    'type': 'structure',
                    'members': {'Tags': {'shape': 'Map'}},
                },
                'Map': {
                    'type': 'map',
                    'key': {'shape': 'String'},
                    'value': {'shape': 'String'},
                },
                'String': {'type': 'string'},
            }
        ).get_shape_by_name('Input')
        params = {'Tags': {'Password': 'not-a-secret'}}
        assert redact_parameters(params, shape) == (params, [])

    def test_no_input_shape(self):
        assert redact_parameters({}, None) == ({}, [])

    def test_streaming_and_large_blobs(self, input_shape):
        redacted, _ = redact_parameters(
            {'Name': io.BytesIO(b'data'), 'Other': b'a' * (MAX_BODY_SIZE + 1)},
            input_shape,
        )
        assert redacted == {
            'Name': '<streaming body>',
            'Other': f'<binary: {MAX_BODY_SIZE + 1} bytes>',
        }


class TestRedactHeaders:
    def test_signing_headers_are_removed(self):
        headers = {
            'Authorization': 'AWS4-HMAC-SHA256 ...',
            'X-Amz-Date': '20260101T000000Z',
            'X-Amz-Security-Token': 'token',
            'X-Amz-Content-SHA256': 'abc',
            'Content-Type': 'application/json',
        }
        assert redact_headers(headers) == {'Content-Type': 'application/json'}

    def test_customer_key_headers_are_redacted(self):
        headers = {
            'x-amz-server-side-encryption-customer-key': 'key',
            'x-amz-copy-source-server-side-encryption-customer-key': 'key',
        }
        assert redact_headers(headers) == {
            'x-amz-server-side-encryption-customer-key': REDACTED,
            'x-amz-copy-source-server-side-encryption-customer-key': REDACTED,
        }

    def test_sensitive_header_members_are_redacted(self, input_shape):
        _, sensitive = redact_parameters({'Token': 't'}, input_shape)
        headers = {'X-Amz-Token': 't', 'User-Agent': 'aws-cli'}
        assert redact_headers(headers, sensitive) == {
            'X-Amz-Token': REDACTED,
            'User-Agent': 'aws-cli',
        }

    def test_bytes_values_are_decoded(self):
        assert redact_headers({'Foo': b'bar'}) == {'Foo': 'bar'}


class TestRenderBody:
    @pytest.mark.parametrize('body', [None, b'', '', {}])
    def test_empty(self, body):
        assert render_body(body) is None

    def test_dict(self):
        body = {'Action': 'DescribeInstances'}
        assert render_body(body) == body

    def test_bytes(self):
        assert render_body(b'{"a": 1}') == '{"a": 1}'

    def test_str(self):
        assert render_body('<xml/>') == '<xml/>'

    def test_binary(self):
        assert render_body(b'\xff\xfe') == '<binary body: 2 bytes>'

    def test_large(self):
        body = b'a' * (MAX_BODY_SIZE + 1)
        assert render_body(body) == f'<body: {MAX_BODY_SIZE + 1} bytes>'

    def test_streaming_body_is_not_read(self):
        body = mock.Mock(spec=io.BytesIO)
        assert render_body(body) == '<streaming body>'
        body.read.assert_not_called()


class TestBuildDocumentUrl:
    def test_query_string_and_uri_are_redacted_when_sensitive(
        self, input_shape
    ):
        _, sensitive = redact_parameters({'Path': 'secret'}, input_shape)
        assert (
            _redact_url('https://example.com/secret?a=secret', sensitive)
            == f'https://example.com/{REDACTED}?{REDACTED}'
        )

    def test_path_is_kept_without_sensitive_uri_member(self, input_shape):
        _, sensitive = redact_parameters({'Secret': 'secret'}, input_shape)
        assert (
            _redact_url('https://example.com/path?a=secret', sensitive)
            == f'https://example.com/path?{REDACTED}'
        )


class TestEnableDryRun:
    def _session(self, emitter):
        session = mock.Mock()
        session.get_component.return_value = emitter
        return session

    def test_nothing_registered_when_flag_off(self):
        emitter = HierarchicalEmitter()
        parsed_args = argparse.Namespace(cli_dry_run=False)
        enable_dry_run(parsed_args, self._session(emitter))
        assert emitter._unique_id_handlers == {}

    def test_nothing_registered_when_flag_missing(self):
        emitter = HierarchicalEmitter()
        enable_dry_run(argparse.Namespace(), self._session(emitter))
        assert emitter._unique_id_handlers == {}

    def test_handlers_registered_when_flag_on(self):
        emitter = HierarchicalEmitter()
        parsed_args = argparse.Namespace(cli_dry_run=True)
        enable_dry_run(parsed_args, self._session(emitter))
        assert emitter._unique_id_handlers.keys() == {
            'cli-dry-run-before-call',
        }

    def test_handlers_removed_when_session_is_reused_without_flag(self):
        emitter = HierarchicalEmitter()
        session = self._session(emitter)
        enable_dry_run(argparse.Namespace(cli_dry_run=True), session)
        enable_dry_run(argparse.Namespace(cli_dry_run=False), session)
        assert emitter._unique_id_handlers == {}
