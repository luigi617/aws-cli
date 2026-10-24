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
"""Implements the ``--cli-dry-run`` global option.

When ``--cli-dry-run`` is provided, the command is parsed and validated and
the request for the AWS API operation is built as usual, but it is
intercepted at the end of the ``before-call`` event, before it is signed or
sent. The request is then printed as JSON instead of a service response,
along with the output a successful call would produce. Options such as
``--query`` and ``--output`` are treated as part of the command being dry
run, so they are applied to a placeholder response to produce that output
instead of to the printed document.

The printed request reflects the changes ``before-call`` handlers make, with
two exceptions: presigned URLs, which would be signed with the caller's
credentials, are replaced by a placeholder, and checksums that would require
reading a streaming body are not computed. Changes made after
``before-call``, such as signing and endpoint discovery, are not reflected.

Only commands that map to a single AWS API operation are supported. As a
fail-closed backstop, any other API call made while ``--cli-dry-run`` is
active is refused, except for the calls credential providers make to obtain
credentials.
"""

import copy
import io
import json
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

from botocore import xform_name
from botocore.history import get_global_history_recorder
from botocore.utils import ArgumentGenerator

from awscli.clidriver import CLIOperationCaller
from awscli.customizations.exceptions import DryRunNotSupportedError
from awscli.formatter import get_formatter, is_response_paginated
from awscli.utils import json_encoder

REDACTED = '****'
MAX_BODY_SIZE = 64 * 1024
STREAMING_BODY = '<streaming body>'
PRESIGNED_URL_OMITTED = '<presigned url omitted>'
NOT_COMPUTED = '<not computed>'

# Operations that credential and token providers call on clients created from
# the session, e.g. when a role is assumed while resolving an account-based
# endpoint. They are allowed so that resolving credentials behaves the same
# as in a real run.
_CREDENTIAL_PROVIDER_OPERATIONS = {
    ('sts', 'AssumeRole'),
    ('sts', 'AssumeRoleWithWebIdentity'),
    ('sts', 'AssumeRoleWithSAML'),
    ('sso', 'GetRoleCredentials'),
    ('sso-oidc', 'CreateToken'),
    ('signin', 'CreateOAuth2Token'),
}
# Output formats that are not data, so are shown as their lines of text.
_LINE_OUTPUT_FORMATS = ('text', 'table')
# Headers added by signing. They are not present before signing, but are
# removed in case a customization sets them earlier.
_SIGNING_HEADERS = {
    'authorization',
    'x-amz-date',
    'x-amz-security-token',
    'x-amz-content-sha256',
}
# Checksum headers the Glacier customizations compute by reading the body.
_GLACIER_CHECKSUM_HEADERS = ('x-amz-content-sha256', 'x-amz-sha256-tree-hash')
# Members not modeled as sensitive are also redacted if they hold a string
# and their lowercased name contains one of these, e.g. "MasterUserPassword"
# or "AuthToken", unless it ends with one of the excluded suffixes, e.g.
# "PasswordSecretArn" or "AuthTokenUpdateStrategy".
_SENSITIVE_NAME_PARTS = (
    'password',
    'passphrase',
    'authtoken',
    'secretaccesskey',
    'privatekey',
    'sessiontoken',
    'clientsecret',
    'customerkey',
)
_SENSITIVE_NAMES = {'mfa'}
_NON_SENSITIVE_NAME_SUFFIXES = (
    'arn',
    'id',
    'ids',
    'type',
    'version',
    'strategy',
    'status',
)


def register_dry_run(cli):
    cli.register(
        'top-level-args-parsed', enable_dry_run, unique_id='cli-dry-run'
    )


def enable_dry_run(parsed_args, session, **kwargs):
    emitter = session.get_component('event_emitter')
    if not getattr(parsed_args, 'cli_dry_run', False):
        # The session may be reused for another command, e.g. when the CLI
        # is driven programmatically, so remove what a dry run registered.
        emitter.unregister('before-call', unique_id='cli-dry-run-before-call')
        return
    # Clients created later copy the session's event emitter, so this also
    # applies to clients created by customizations and credential providers.
    emitter.register_last(
        'before-call',
        refuse_api_call,
        unique_id='cli-dry-run-before-call',
    )


def refuse_api_call(model, **kwargs):
    service_name = model.service_model.service_name
    if (service_name, model.name) in _CREDENTIAL_PROVIDER_OPERATIONS:
        return
    raise DryRunNotSupportedError(
        f'--cli-dry-run refused to call {service_name} {model.name}, which '
        f'is not the operation being dry run. The command is not supported '
        f'with --cli-dry-run.'
    )


def invoke_dry_run(
    session,
    command_name,
    operation_model,
    operation_caller,
    call_parameters,
    parsed_globals,
):
    """Print the request an operation command would send instead of sending.

    This is called by ``ServiceOperation`` in place of its operation caller,
    after the ``calling-command`` handlers have run and none of them
    overrode the call, e.g. for ``--generate-cli-skeleton``.
    """
    # Commands such as waiters reuse an operation's arguments but do not
    # make a single call to it, so only the standard caller is supported.
    # Callers that subclass it, such as the one for ``ssm start-session``,
    # are bypassed so their side effects do not happen.
    if not isinstance(operation_caller, CLIOperationCaller):
        raise DryRunNotSupportedError(
            f"--cli-dry-run is not supported for '{command_name}'. It is "
            f"only supported for commands that call a single AWS API "
            f"operation."
        )
    return DryRunCLIOperationCaller(session).invoke(
        operation_model.service_model.service_name,
        operation_model.name,
        call_parameters,
        parsed_globals,
    )


class _CapturedRequest(Exception):
    def __init__(self, request_dict, model):
        super().__init__('Request captured by --cli-dry-run')
        self.request_dict = request_dict
        self.model = model


class _RequestCapture:
    def __init__(self):
        self.api_params = None

    def record_params(self, params, **kwargs):
        # Keep a reference so in-place changes made by later
        # before-parameter-build handlers are reflected.
        self.api_params = params

    def skip_glacier_checksums(self, params, **kwargs):
        # Computing them would read the streaming body, which may be large
        # or unseekable.
        if hasattr(params['body'], 'read'):
            for header in _GLACIER_CHECKSUM_HEADERS:
                params['headers'].setdefault(header, NOT_COMPUTED)

    def capture(self, model, params, **kwargs):
        raise _CapturedRequest(params, model)


def _omit_presigned_url(*args, **kwargs):
    return PRESIGNED_URL_OMITTED


class DryRunCLIOperationCaller(CLIOperationCaller):
    """Build the request for an operation and print it instead of sending."""

    def invoke(self, service_name, operation_name, parameters, parsed_globals):
        client = self._session.create_client(
            service_name,
            region_name=parsed_globals.region,
            endpoint_url=parsed_globals.endpoint_url,
            verify=parsed_globals.verify_ssl,
        )
        # Handlers such as the EC2 and RDS cross-region presigned URL
        # injectors sign a URL with the caller's credentials, which is
        # neither needed nor wanted to show the request.
        client._request_signer.generate_presigned_url = _omit_presigned_url
        capture = _RequestCapture()
        events = client.meta.events
        service_id = client.meta.service_model.service_id.hyphenize()
        events.register_last(
            f'before-parameter-build.{service_id}.{operation_name}',
            capture.record_params,
            unique_id='cli-dry-run-record-params',
        )
        if service_id == 'glacier':
            events.register_first(
                f'before-call.{service_id}.{operation_name}',
                capture.skip_glacier_checksums,
                unique_id='cli-dry-run-skip-glacier-checksums',
            )
        # The request is captured after every other before-call handler has
        # run, so it includes the headers and body changes they make. This
        # client is the one making the call being dry run, so the refusal
        # copied from the session does not apply to it.
        events.unregister('before-call', unique_id='cli-dry-run-before-call')
        events.register_last(
            'before-call', capture.capture, unique_id='cli-dry-run-capture'
        )
        try:
            with _history_disabled():
                response = self._make_client_call(
                    client, operation_name, parameters, parsed_globals
                )
                if is_response_paginated(response):
                    # Only the first page's request is needed.
                    next(iter(response), None)
        except _CapturedRequest as captured:
            document = build_dry_run_document(
                client, captured.model, capture.api_params, captured
            )
        else:
            raise RuntimeError(
                f'--cli-dry-run did not capture a request for '
                f'{service_name} {operation_name}.'
            )
        document['ExpectedOutput'] = self._render_expected_output(
            client, operation_name, parsed_globals
        )
        # The document describes the command being dry run, so options such
        # as --query and --output apply to its ExpectedOutput instead of to
        # the document, which is always JSON.
        with self._output_stream_factory.get_output_stream() as stream:
            json.dump(
                document,
                stream,
                indent=4,
                default=json_encoder,
                ensure_ascii=False,
            )
            stream.write('\n')
        return 0

    def _render_expected_output(self, client, operation_name, parsed_globals):
        response = generate_expected_response(
            client.meta.service_model.operation_model(operation_name)
        )
        if (
            client.can_paginate(xform_name(operation_name))
            and parsed_globals.paginate
        ):
            # Pagination tokens are not included in the output when the
            # CLI paginates automatically.
            paginator_config = self._session.get_paginator_model(
                client.meta.service_model.service_name
            ).get_paginator(operation_name)
            _remove_output_tokens(paginator_config, response)
        output = parsed_globals.output
        if output is None:
            output = self._session.get_config_variable('output')
        if output == 'off':
            return None
        if output not in _LINE_OUTPUT_FORMATS:
            # JSON and YAML output are embedded as data so that they are
            # readable instead of an escaped string. They are rendered as
            # JSON to apply --query and the same value conversions.
            output = 'json'
        # Color codes would be escaped in the JSON document.
        format_args = copy.copy(parsed_globals)
        format_args.color = 'off'
        stream = io.StringIO()
        get_formatter(output, format_args)(operation_name, response, stream)
        rendered = stream.getvalue()
        if not rendered:
            # Nothing would be printed.
            return None
        if output == 'json':
            return json.loads(rendered)
        lines = rendered.splitlines()
        if output == 'text':
            # Text output is tab separated, so each line is embedded as its
            # list of columns instead of with escaped tabs.
            return [line.split('\t') for line in lines]
        return lines


@contextmanager
def _history_disabled():
    """Do not record the call in ``aws history``, as it is never sent."""
    recorder = get_global_history_recorder()
    was_enabled = getattr(recorder, '_enabled', False)
    recorder.disable()
    try:
        yield
    finally:
        if was_enabled:
            recorder.enable()


def build_dry_run_document(client, operation_model, api_params, captured):
    request_dict = captured.request_dict
    redacted_params, sensitive_members = redact_parameters(
        api_params or {}, operation_model.input_shape
    )
    url = request_dict['url']
    body = render_body(request_dict.get('body'))
    if sensitive_members:
        # A sensitive value may have been serialized anywhere in the body
        # or query string, so they are redacted as a whole.
        url = _redact_url(url, sensitive_members)
        if body is not None and body != STREAMING_BODY:
            body = REDACTED
    return {
        'DryRun': True,
        'Service': operation_model.service_model.service_name,
        'Operation': operation_model.name,
        'Region': client.meta.region_name,
        'Parameters': redacted_params,
        'Request': {
            'Method': request_dict['method'],
            'Url': url,
            'Headers': redact_headers(
                request_dict['headers'], sensitive_members
            ),
            'Body': body,
        },
    }


def generate_expected_response(operation_model):
    """Generate the shape of the response a successful call returns.

    Values are placeholders, the same as the ones
    ``--generate-cli-skeleton output`` uses.
    """
    output_shape = operation_model.output_shape
    if output_shape is None:
        return {}
    response = ArgumentGenerator(use_member_names=True).generate_skeleton(
        output_shape
    )
    if operation_model.has_streaming_output:
        # The streamed payload is written to the outfile instead of being
        # included in the output.
        response.pop(output_shape.serialization['payload'], None)
    return response


def _remove_output_tokens(paginator_config, response):
    output_tokens = paginator_config['output_token']
    if not isinstance(output_tokens, list):
        output_tokens = [output_tokens]
    for token in output_tokens:
        # Expressions such as "NextMarker || Contents[-1].Key" are not
        # removed, only plain top level member names.
        response.pop(token, None)


def redact_parameters(params, input_shape):
    """Redact values of members that are sensitive.

    A member is sensitive if it is modeled as sensitive or if its name looks
    like it holds a secret, see ``_SENSITIVE_NAME_PARTS``. Returns a
    redacted copy of ``params`` along with the list of top level input
    members whose value contained a sensitive value.
    """
    if input_shape is None:
        return _redact_value(params, None)[0], []
    redacted = {}
    sensitive_members = []
    for name, value in params.items():
        member = input_shape.members.get(name)
        redacted[name], found = _redact_value(value, member, name)
        if found:
            sensitive_members.append(member)
    return redacted, sensitive_members


def _is_sensitive_name(name):
    if name is None:
        return False
    lowered = name.lower()
    if lowered in _SENSITIVE_NAMES:
        return True
    return any(
        part in lowered for part in _SENSITIVE_NAME_PARTS
    ) and not lowered.endswith(_NON_SENSITIVE_NAME_SUFFIXES)


def _redact_value(value, shape, name=None):
    if hasattr(value, 'read'):
        return STREAMING_BODY, False
    if shape is not None and shape.metadata.get('sensitive'):
        return REDACTED, True
    if isinstance(value, str) and _is_sensitive_name(name):
        return REDACTED, True
    type_name = getattr(shape, 'type_name', None)
    found = False
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            item_name = None
            if type_name == 'structure':
                item_shape = shape.members.get(key)
                item_name = key
            elif type_name == 'map':
                item_shape = shape.value
            else:
                item_shape = None
            redacted[key], item_found = _redact_value(
                item, item_shape, item_name
            )
            found = found or item_found
        return redacted, found
    if isinstance(value, list):
        item_shape = shape.member if type_name == 'list' else None
        redacted = []
        for item in value:
            redacted_item, item_found = _redact_value(item, item_shape)
            redacted.append(redacted_item)
            found = found or item_found
        return redacted, found
    if isinstance(value, (bytes, bytearray)) and len(value) > MAX_BODY_SIZE:
        return f'<binary: {len(value)} bytes>', False
    return value, False


def redact_headers(headers, sensitive_members=()):
    sensitive_names = set()
    sensitive_prefixes = []
    for member in sensitive_members:
        location = member.serialization.get('location')
        name = member.serialization.get('name', '').lower()
        if location == 'header':
            sensitive_names.add(name)
        elif location == 'headers':
            sensitive_prefixes.append(name)
    redacted = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _SIGNING_HEADERS:
            continue
        if (
            lowered in sensitive_names
            or 'customer-key' in lowered
            or any(lowered.startswith(p) for p in sensitive_prefixes)
        ):
            value = REDACTED
        elif isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        redacted[name] = value
    return redacted


def _redact_url(url, sensitive_members):
    parts = urlsplit(url)
    path = parts.path
    if any(
        member.serialization.get('location') == 'uri'
        for member in sensitive_members
    ):
        path = f'/{REDACTED}'
    query = REDACTED if parts.query else ''
    return urlunsplit((parts.scheme, parts.netloc, path, query, ''))


def render_body(body):
    if body is None or (isinstance(body, (bytes, str, dict)) and not body):
        return None
    if isinstance(body, dict):
        # Query protocol bodies are form encoded from a dict when sent.
        return body
    if hasattr(body, 'read'):
        # Never read streaming bodies, they may be large or unseekable.
        return STREAMING_BODY
    if isinstance(body, str):
        body = body.encode('utf-8')
    if isinstance(body, (bytes, bytearray)):
        if len(body) > MAX_BODY_SIZE:
            return f'<body: {len(body)} bytes>'
        try:
            return body.decode('utf-8')
        except UnicodeDecodeError:
            return f'<binary body: {len(body)} bytes>'
    return STREAMING_BODY
