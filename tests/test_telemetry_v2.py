"""Semantic-convention acceptance checks against SDK spans and decoded OTLP."""
import json
import logging
from pathlib import Path
import sysconfig

import pytest
from jsonschema import validate as validate_json_schema
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

import otel_hook as hook
from otel_hooks.capture import CapturePolicy
from otel_hooks.streaming import plan
from otel_hooks.telemetry import usage_attributes
from otel_hooks.timing import PersistedIdGenerator, boundaries


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    hook._load_otel_modules()
    for name in ('_SESSION_DIR', '_BATCH_DIR', '_LOCK_DIR', '_LOCAL_SPANS_DIR', '_STATE_DIR'):
        monkeypatch.setattr(hook, name, str(tmp_path / name))
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', CapturePolicy())
    monkeypatch.setattr(hook, '_LOGS_INITIALIZED', False)
    monkeypatch.setenv('IDE_OTEL_MASK_PROMPTS', 'false')
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: True)
    monkeypatch.setattr(hook, '_resolve_repository_context', lambda data: {})
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=PersistedIdGenerator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer('ide-hooks', '3'), exporter
    provider.shutdown()


def test_usage_inclusive_cache_and_reasoning_with_real_zeros():
    result = usage_attributes({'input_tokens': 300, 'cache_read_input_tokens': 600,
                               'cache_creation_input_tokens': 100, 'output_tokens': 200,
                               'reasoning_tokens': 50}, 'anthropic')
    assert result['gen_ai.usage.input_tokens'] == 1000
    assert result['o11y.usage.total_tokens'] == 1200
    assert result['gen_ai.usage.reasoning.output_tokens'] == 50
    assert usage_attributes({'input_tokens': 0, 'output_tokens': 0}, 'openai')['o11y.usage.total_tokens'] == 0
    assert 'gen_ai.usage.input_tokens' not in usage_attributes({'input_tokens': True}, None)
    assert usage_attributes({'input_tokens': 1000, 'output_tokens': 200,
                             'cache_read_input_tokens': 600}, 'openai')['o11y.usage.total_tokens'] == 1200


@pytest.mark.parametrize('details', [
    {'input_tokens': 1000, 'output_tokens': 200,
     'input_tokens_details': {'cached_tokens': 600},
     'output_tokens_details': {'reasoning_tokens': 50}},
    {'prompt_tokens': 1000, 'completion_tokens': 200,
     'prompt_tokens_details': {'cached_tokens': 600},
     'completion_tokens_details': {'reasoning_tokens': 50}},
])
@pytest.mark.parametrize('nested', [False, True])
def test_default_capture_preserves_openai_usage_details_in_otlp(runtime, monkeypatch, details, nested):
    tracer, exporter = runtime
    config_path = Path(__file__).parents[1] / 'otel_config.example.json'
    if not config_path.is_file():
        config_path = Path(sysconfig.get_path('data')) / 'share/opentelemetry-hooks/otel_config.example.json'
    config = json.loads(config_path.read_text())
    policy = CapturePolicy.from_config(config)
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', policy)
    payload = {'provider': 'openai', 'model': 'gpt-4.1',
               **({'usage': details} if nested else details), 'prompt': 'private content'}
    permitted = policy.filter(payload)
    assert 'prompt' not in permitted
    data = hook.CursorEventAdapter(capture_policy=policy).parse('stop', permitted).to_lifecycle_data()
    with tracer.start_as_current_span('callback') as span:
        hook._populate_span(span, 'Stop', data, 'cursor')
    encoded = encode_spans(exporter.get_finished_spans())
    decoded = type(encoded).FromString(encoded.SerializeToString())
    attrs = {a.key: a.value for a in decoded.resource_spans[0].scope_spans[0].spans[0].attributes}
    for key, value in {'input_tokens': 1000, 'output_tokens': 200,
                       'cache_read.input_tokens': 600, 'reasoning.output_tokens': 50}.items():
        attr = attrs['gen_ai.usage.' + key]
        assert attr.WhichOneof('value') == 'int_value'
        assert attr.int_value == value
    assert attrs['o11y.usage.total_tokens'].int_value == 1200
    assert attrs['gen_ai.provider.name'].string_value == 'openai'
    assert attrs['o11y.client.name'].string_value == 'cursor'
    assert 'gen_ai.system' not in attrs
    assert 'gen_ai.usage.total_tokens' not in attrs
    assert not any(key.startswith('hook.') for key in attrs)


@pytest.mark.parametrize('write_key', ['cache_creation_input_tokens', 'cache_write_input_tokens'])
@pytest.mark.parametrize('write', [0, 100])
def test_provider_cache_write_inputs_emit_only_the_standard_attribute(write_key, write):
    permitted = CapturePolicy().filter({'usage': {
        'input_tokens': 300, 'cache_read_input_tokens': 600, write_key: write, 'output_tokens': 200,
    }})
    attrs = usage_attributes(permitted, 'anthropic')
    assert attrs['gen_ai.usage.cache_write.input_tokens'] == write
    assert 'gen_ai.usage.cache_creation.input_tokens' not in attrs
    assert attrs['gen_ai.usage.input_tokens'] == 900 + write
    assert attrs['o11y.usage.total_tokens'] == 1100 + write


@pytest.mark.parametrize('value', [True, -1, 1.5, '50'])
def test_usage_details_reject_non_integer_token_counts(value):
    attrs = usage_attributes(CapturePolicy().filter({'usage': {
        'input_tokens_details': {'cached_tokens': value},
        'output_tokens_details': {'reasoning_tokens': value},
    }}), 'openai')
    assert 'gen_ai.usage.cache_read.input_tokens' not in attrs
    assert 'gen_ai.usage.reasoning.output_tokens' not in attrs


@pytest.mark.parametrize('event,fields,legacy', [
    ('PreToolUse', {'tool_name': 'Read', 'tool_type': 'function'}, 'o11y.client.tool_name'),
    ('AfterMCPExecution', {'mcp_tool': 'search', 'tool_type': 'extension'}, 'o11y.client.mcp_tool'),
])
def test_callback_tool_identity_does_not_claim_an_execution_scope(runtime, event, fields, legacy):
    tracer, _ = runtime
    with tracer.start_as_current_span('callback') as span:
        hook._populate_span(span, event, CapturePolicy().filter(fields), 'cursor')
    assert span.attributes['gen_ai.tool.name'] == span.attributes[legacy]
    assert span.attributes['gen_ai.tool.type'] == fields['tool_type']
    assert 'gen_ai.operation.name' not in span.attributes


def test_context_occupancy_is_not_billed_usage_and_top_k_is_integer(runtime):
    tracer, _ = runtime
    with tracer.start_as_current_span('callback') as span:
        hook._populate_span(span, 'PreCompact', {'context_tokens': 1000, 'top_k': 40}, 'cursor')
    assert span.attributes['o11y.client.context_tokens'] == 1000
    assert not any(key.startswith('gen_ai.usage.') for key in span.attributes)
    assert type(span.attributes['gen_ai.request.top_k']) is int


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('failure', [False, True])
def test_emitted_operations_follow_pinned_registry_and_terminal_status(runtime, monkeypatch, streaming, failure):
    tracer, exporter = runtime
    from opentelemetry.trace import SpanKind
    from opentelemetry.trace.status import StatusCode
    registry = json.loads((Path(__file__).parent / 'fixtures/semconv/gen-ai-9ffc7e0.json').read_text())['attributes']
    content_schemas = {key: json.loads((Path(__file__).parent / 'fixtures/semconv' / filename).read_text())
                       for key, filename in {
                           'gen_ai.input.messages': 'gen-ai-input-messages.json',
                           'gen_ai.output.messages': 'gen-ai-output-messages.json',
                           'gen_ai.system_instructions': 'gen-ai-system-instructions.json',
                           'gen_ai.tool.definitions': 'gen-ai-tool-definitions.json',
                           'gen_ai.tool.call.arguments': 'gen-ai-tool-call-arguments.json',
                           'gen_ai.tool.call.result': 'gen-ai-tool-call-result.json',
                       }.items()}
    policy = CapturePolicy(mode='full')
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', policy)
    original_sampler = tracer.sampler
    created = []

    class ObservedSampler:
        def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
            created.append((name, dict(attributes or {})))
            return original_sampler.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    monkeypatch.setattr(tracer, 'sampler', ObservedSampler())
    context = hook._create_session_context('s', {'session_id': 's', 'conversation_id': 'conversation'}, 'claude')
    terminal = ('PostToolUseFailure', {'error': 'fixture failure', 'error_type': 'tool_error',
                                     'tool_output': {'should_not_be_a_result': True}}) if failure else (
                'PostToolUse', {'tool_output': {'ok': True}})
    events = [
        ('UserPromptSubmit', {'agent_name': 'assistant', 'model': 'claude-sonnet', 'provider_name': 'anthropic',
                              'prompt': 'read file', 'system_instructions': 'Use tools'}),
        ('PreToolUse', {'tool_name': 'Read', 'tool_type': 'function', 'tool_use_id': 'call',
                        'tool_input': {'file_path': '/fixture/file'}, 'model': 'claude-sonnet'}),
        (terminal[0], {'tool_name': 'Read', 'tool_use_id': 'call', **terminal[1]}),
        ('SubagentStart', {'agent_id': 'invocation-only', 'subagent_type': 'planner'}),
        ('SubagentStop', {'agent_id': 'invocation-only', 'subagent_type': 'planner'}),
        ('Stop', {'response': 'done', 'provider_name': 'anthropic', 'usage': {
            'input_tokens': 300, 'cache_read_input_tokens': 600, 'cache_creation_input_tokens': 100,
            'output_tokens': 200, 'reasoning_tokens': 50}}),
    ]
    for index, (event, payload) in enumerate(events):
        permitted = policy.filter({'session_id': 's', 'conversation_id': 'conversation', **payload})
        data = hook.ClaudeEventAdapter(capture_policy=policy).parse(event, permitted).to_lifecycle_data()
        data.update(_hook_event_time_ns=100 + index * 10, _hook_observed_time_ns=100 + index * 10)
        if streaming:
            assert hook._emit_streaming_event(tracer, 's', event, data, 'claude', 'g')
        else:
            hook._append_batch_event('g', event, data)
    if not streaming:
        assert hook._flush_generation(tracer, 'g', context, 'claude')
    hook._flush_session(tracer, 's', context, 'claude')
    spans = exporter.get_finished_spans()
    assert spans
    expected_types = {'string': lambda v: isinstance(v, str), 'int': lambda v: type(v) is int,
                      'double': lambda v: type(v) is float, 'boolean': lambda v: type(v) is bool,
                      'string[]': lambda v: isinstance(v, (tuple, list)) and all(isinstance(s, str) for s in v),
                      'any': lambda v: isinstance(v, str) and json.loads(v) is not None}
    for span in spans:
        attrs = span.attributes
        assert attrs['gen_ai.conversation.id'] == 'conversation'
        for key, value in attrs.items():
            if key.startswith('gen_ai.'):
                assert key in registry, key
                assert expected_types[registry[key]](value), (key, value)
                if key in content_schemas:
                    validate_json_schema(json.loads(value), content_schemas[key])
        assert 'gen_ai.agent.id' not in attrs  # run IDs are not hosted resources
        if attrs['o11y.span.role'] in {'callback', 'session'}:
            assert 'gen_ai.operation.name' not in attrs
        operation = attrs.get('gen_ai.operation.name')
        if operation:
            assert span.kind == SpanKind.INTERNAL
            assert span.name.startswith(operation)
            creation = next(attributes for name, attributes in created if name == span.name)
            assert creation['gen_ai.operation.name'] == operation
            if operation == 'execute_tool':
                assert creation['gen_ai.tool.name'] == attrs['gen_ai.tool.name']
                assert creation['gen_ai.request.model'] == 'claude-sonnet'
    tool, = [s for s in spans if s.attributes['o11y.span.role'] == 'tool']
    assert tool.name == 'execute_tool Read'
    assert tool.end_time == 120
    assert tool.status.status_code == (StatusCode.ERROR if failure else StatusCode.UNSET)
    assert json.loads(tool.attributes['gen_ai.tool.call.arguments']) == {'file_path': '/fixture/file'}
    if failure:
        assert tool.attributes['error.type'] == 'tool_error'
        assert 'gen_ai.tool.call.result' not in tool.attributes
    else:
        assert json.loads(tool.attributes['gen_ai.tool.call.result']) == {'ok': True}
    usage, = [s for s in spans if s.attributes.get('gen_ai.usage.input_tokens') is not None]
    assert usage.attributes['gen_ai.usage.input_tokens'] == 1000
    assert usage.attributes['gen_ai.usage.cache_write.input_tokens'] == 100


def test_standard_content_and_mapping_namespace_are_strict(runtime, monkeypatch):
    tracer, _ = runtime
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', CapturePolicy(mode='full'))
    with tracer.start_as_current_span('callback') as span:
        hook._populate_span(span, 'PostToolUse', {'tool_name': 'Read', 'tool_output': '42',
                            'tool_input': '{"file_path":"/fixture/file"}',
                            'tool_definitions': [{'type': 'function', 'function': {'name': 'Read', 'parameters': {'type': 'object'}}}],
                            'system_instructions': 'Use tools', 'temperature': True,
                            'max_tokens': '100', 'seed': 1.5}, 'cursor')
    assert 'gen_ai.tool.call.result' not in span.attributes
    assert span.attributes['o11y.client.tool_output.text'] == '42'
    assert json.loads(span.attributes['gen_ai.tool.call.arguments']) == {'file_path': '/fixture/file'}
    assert json.loads(span.attributes['gen_ai.tool.definitions']) == [{'type': 'function', 'name': 'Read'}]
    assert json.loads(span.attributes['gen_ai.system_instructions']) == [{'type': 'text', 'content': 'Use tools'}]
    assert not any(key in span.attributes for key in ('gen_ai.request.temperature', 'gen_ai.request.max_tokens', 'gen_ai.request.seed'))
    for key in ('gen_ai.system', 'gen_ai.client.prompt', 'gen_ai.future.custom_field'):
        with pytest.raises(ValueError, match='protected'):
            CapturePolicy.from_config({'capture': {'mappings': {'metadata.foo': key}}})


def test_parallel_boundaries_and_final_callback():
    batch = [{'event': event, 'timestamp_ns': ts, 'data': {'session_id': 's', 'agent_id': agent, **extra}}
             for event, ts, agent, extra in [
                 ('PreToolUse', 10, 'a', {'tool_use_id': 't'}),
                 ('Notification', 20, 'b', {}),
                 ('Notification', 30, 'a', {}),
                 ('PostToolUse', 40, 'a', {'tool_use_id': 't'}),
                 ('Stop', 50, 'b', {}),
             ]]
    assert boundaries(batch) == [(40, 'matching_callback', False), (50, 'next_callback', False),
                                  (40, 'next_callback', False), (40, 'observation', False),
                                  (50, 'observation', False)]


def test_buffered_retry_preserves_export_ids_times_and_session_parent(runtime, monkeypatch):
    tracer, exporter = runtime
    context = hook._create_session_context('s', {'session_id': 's'}, 'claude')
    for event, ts, extra in [('PreToolUse', 100, {'tool_use_id': 'call'}),
                             ('PostToolUse', 200, {'tool_use_id': 'call'}), ('Stop', 300, {})]:
        hook._append_batch_event('s-g', event, {'session_id': 's', '_hook_event_time_ns': ts,
                                              '_hook_observed_time_ns': ts + 10, **extra})
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: False)
    assert not hook._flush_generation(tracer, 's-g', context, 'claude')
    first = exporter.get_finished_spans()
    exporter.clear()
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: True)
    assert hook._flush_generation(tracer, 's-g', context, 'claude')
    second = exporter.get_finished_spans()
    assert [(s.context.span_id, s.start_time, s.end_time) for s in first] == [
        (s.context.span_id, s.start_time, s.end_time) for s in second]
    generation = next(s for s in second if s.name == 'invoke_agent')
    assert f'{generation.parent.span_id:016x}' == context['session_span_id']
    pre, post, stop = [s for s in second if s.attributes.get('o11y.client.hook.event')]
    assert post.parent.span_id == pre.context.span_id
    assert pre.start_time == 100 and pre.end_time == 200
    assert stop.start_time == stop.end_time == generation.end_time == 300
    hook._flush_session(tracer, 's', context, 'claude')
    root = exporter.get_finished_spans()[-1]
    assert root.parent is None
    assert root.context.span_id == generation.parent.span_id
    assert root.context.trace_id == generation.context.trace_id
    encoded = encode_spans(second)
    decoded = type(encoded).FromString(encoded.SerializeToString())
    spans = decoded.resource_spans[0].scope_spans[0].spans
    assert spans[0].start_time_unix_nano == 100
    assert spans[0].status.code == 0  # ordinary spans remain UNSET


def test_streaming_parallel_async_scopes_and_retry(runtime, monkeypatch):
    tracer, exporter = runtime
    hook._create_session_context('s', {'session_id': 's'}, 'claude')
    events = [('UserPromptSubmit', 10, {}), ('SubagentStart', 20, {'agent_id': 'child'}),
              ('PreToolUse', 30, {'agent_id': 'child', 'tool_use_id': 'call'}),
              ('Stop', 40, {}),
              ('PostToolUse', 60, {'agent_id': 'child', 'tool_use_id': 'call'}),
              ('SubagentStop', 70, {'agent_id': 'child'})]
    for event, ts, extra in events:
        assert hook._emit_streaming_event(tracer, 's', event, {'session_id': 's', '_hook_event_time_ns': ts,
                                          '_hook_observed_time_ns': ts, **extra}, 'claude', 'g')
    spans = exporter.get_finished_spans()
    by_role = {s.attributes['o11y.span.role']: s for s in spans}
    assert by_role['tool'].end_time == 60
    assert by_role['subagent'].end_time == 70
    assert by_role['generation'].end_time == 40
    assert by_role['tool'].parent.span_id == by_role['subagent'].context.span_id
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: False)
    assert not hook._emit_streaming_event(tracer, 's', 'Notification', {'session_id': 's',
                                         '_hook_observed_time_ns': 80}, 'claude', None)
    queued = hook._load_session_context('s')['stream_outbox']
    assert queued
    saved = queued[0]['span_id']
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: True)
    hook._emit_streaming_event(tracer, 's', 'Notification', {'session_id': 's',
                               '_hook_observed_time_ns': 90}, 'claude', None)
    assert sum(f'{s.context.span_id:016x}' == saved for s in exporter.get_finished_spans()) == 2
    assert hook._load_session_context('s')['stream_outbox'] == []


def test_decoded_otlp_log_body_type_timestamp_status_and_native_context(runtime, monkeypatch):
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
    tracer, _ = runtime
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = logging.Logger('callback-test')
    logger.addHandler(LoggingHandler(logger_provider=provider))
    monkeypatch.setattr(hook, '_get_otel_logger', lambda name: logger)
    monkeypatch.setattr(hook, '_LOGS_INITIALIZED', True)
    with tracer.start_as_current_span('failure') as span:
        hook._populate_span(span, 'PostToolUseFailure', {'_hook_log_body': {'unicode': 'é', 'zero': 0, 'null': None},
                           '_hook_event_time_ns': 1_000_000_000, '_hook_event_id': 'callback',
                           'tool_use_id': 'call', 'session_id': 's'}, 'claude')
    record = exporter.get_finished_logs()[0].log_record
    assert record.trace_id == span.context.trace_id and record.span_id == span.context.span_id
    assert record.timestamp == 1_000_000_000 and record.severity_number.value == 17
    assert record.body['zero'] == 0 and record.body['null'] is None
    encoded = encode_logs(exporter.get_finished_logs())
    decoded = type(encoded).FromString(encoded.SerializeToString())
    log = decoded.resource_logs[0].scope_logs[0].log_records[0]
    assert log.trace_id.hex() == f'{span.context.trace_id:032x}'
    assert log.span_id.hex() == f'{span.context.span_id:016x}'
    assert log.time_unix_nano == 1_000_000_000
    assert span.status.status_code.name == 'ERROR'
    provider.shutdown()


def test_policy_protected_fields_and_log_limit_validation():
    for target in ('gen_ai.usage.input_tokens', 'o11y.client.session_id', 'o11y.client.status',
                   'gen_ai.tool.call.id', 'gen_ai.tool.name', 'gen_ai.tool.type'):
        with pytest.raises(ValueError, match='protected'):
            CapturePolicy.from_config({'capture': {'mappings': {'x': target}}})
    for limit in (0, -1, True, '100'):
        with pytest.raises(ValueError):
            CapturePolicy.from_config({'logs': {'max_body_bytes': limit}})
    assert CapturePolicy.from_config({'logs': {'max_body_bytes': 100}}).fingerprint != CapturePolicy().fingerprint


def test_unicode_original_facts_and_bounded_preview(runtime, monkeypatch):
    tracer, _ = runtime
    policy = CapturePolicy(mode='full', max_string_length=10)
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', policy)
    original = 'é' * 100
    data = hook.ClaudeEventAdapter(capture_policy=policy).parse('UserPromptSubmit', {'prompt': original}).to_lifecycle_data()
    with tracer.start_as_current_span('hook') as span:
        hook._populate_span(span, 'UserPromptSubmit', data, 'claude')
    assert span.attributes['o11y.client.prompt.bytes'] == 200
    assert span.attributes['o11y.client.prompt.length'] == 100
    assert len(span.attributes['o11y.client.prompt.text']) == 10
    assert 'o11y.capture.shortened_or_dropped_fields' in span.attributes


def test_streaming_missing_end_is_explicit():
    state = {'trace_id': '1' * 32, 'session_span_id': '2' * 16}
    plan(state, 'PreToolUse', {'session_id': 's', 'tool_use_id': 't', '_hook_observed_time_ns': 10}, 'g')
    records = plan(state, 'SessionEnd', {'session_id': 's', '_hook_observed_time_ns': 30}, None)
    tool = next(r for r in records if r['name'] == 'o11y.client.tool')
    assert tool['basis'] == 'session_end' and tool['missing']
    assert tool['start'] == 10 and tool['end'] == 30
    assert json.dumps(state)


def test_oversized_log_body_omission_happens_before_persistence(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(hook, '_apply_config_env', lambda config: None)
    monkeypatch.setattr(hook, '_load_config', lambda: {})
    monkeypatch.setattr(hook, '_configure_logging', lambda: None)
    monkeypatch.setattr(hook, '_cleanup_state', lambda: None)
    monkeypatch.setattr(hook, '_load_session_context', lambda key: {})
    monkeypatch.setattr(hook, '_maybe_bind_session_to_upstream_context', lambda *args: {})
    monkeypatch.setattr(hook, '_maybe_enrich_session_context', lambda *args: {})
    monkeypatch.setattr(hook, '_batch_enabled', lambda: True)
    monkeypatch.setattr(hook, '_buffer_session_event', lambda key, event, data, ide: captured.append(data))
    monkeypatch.setattr(hook, '_detect_ide', lambda data: 'claude')
    monkeypatch.setattr(hook, '_emit_stdout_response', lambda *args: None)
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', CapturePolicy(mode='full', max_log_body_bytes=40, max_string_length=8))
    monkeypatch.setenv('IDE_OTEL_ENABLE_LOGS', 'true')
    monkeypatch.setattr(hook, '_load_input', lambda: {'hook_event_name': 'PreToolUse', 'session_id': 's',
                        'tool_use_id': 'call', 'tool_input': {'command': 'é' * 1000}})
    assert hook.main() == 0
    record, = captured
    assert record['_hook_log_body']['o11y.body.omitted_reason'] == 'max_body_bytes'
    assert record['_hook_log_body']['o11y.body.bytes'] > 2000
    assert 'é' * 20 not in json.dumps(record, ensure_ascii=False)
    assert record['_payload_facts']['tool_input']['bytes'] > 2000


def test_log_bridge_preserves_original_nanosecond_times(runtime, monkeypatch):
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
    from otel_hooks.serialization import CallbackLogTimeMixin
    class Handler(CallbackLogTimeMixin, LoggingHandler):
        pass
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = logging.Logger('precise-time')
    logger.addHandler(Handler(logger_provider=provider))
    monkeypatch.setattr(hook, '_LOGS_INITIALIZED', True)
    monkeypatch.setattr(hook, '_get_otel_logger', lambda name: logger)
    hook._emit_event_log('Stop', {'_hook_log_body': {}, '_hook_event_time_ns': 1_771_234_567_891_234_567,
                                '_hook_observed_time_ns': 1_771_234_568_000_000_001})
    record = exporter.get_finished_logs()[0].log_record
    assert record.timestamp == 1_771_234_567_891_234_567
    assert record.observed_timestamp == 1_771_234_568_000_000_001
    provider.shutdown()


def test_small_preview_limit_never_shortens_identity_or_fingerprint(runtime, monkeypatch):
    from otel_hooks.capture import bound_runtime_payload
    tracer, _ = runtime
    policy = CapturePolicy(mode='full', max_string_length=3)
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', policy)
    data = hook.ClaudeEventAdapter(capture_policy=policy).parse('PreToolUse', {
        'session_id': 'session-long', 'tool_use_id': 'invocation-long', 'tool_input': {'command': 'abcdef'},
    }).to_lifecycle_data()
    data['_hook_event_id'] = 'callback-long'
    bounded = bound_runtime_payload(data, policy)
    assert bounded['_hook_event_id'] == 'callback-long'
    assert bounded['session_id'] == 'session-long'
    with tracer.start_as_current_span('hook') as span:
        hook._populate_span(span, 'PreToolUse', bounded, 'claude')
    assert span.attributes['o11y.client.hook.event_id'] == 'callback-long'
    assert span.attributes['gen_ai.tool.call.id'] == 'invocation-long'
    assert len(span.attributes['o11y.client.tool_input.sha256']) == 64


def test_retry_budget_quarantines_records_with_original_identity(runtime, monkeypatch):
    tracer, exporter = runtime
    context = hook._create_session_context('s', {'session_id': 's'}, 'claude')
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', CapturePolicy(max_export_attempts=2))
    hook._append_batch_event('g', 'Stop', {'session_id': 's', '_hook_observed_time_ns': 100})
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: False)
    assert not hook._flush_generation(tracer, 'g', context, 'claude')
    first_ids = [s.context.span_id for s in exporter.get_finished_spans()]
    assert not hook._flush_generation(tracer, 'g', context, 'claude')
    from pathlib import Path
    quarantined, = Path(hook._BATCH_DIR).glob('*.quarantine.json')
    archive = json.loads(quarantined.read_text())
    assert archive['reason'] == 'export_attempts_exhausted'
    assert int(archive['identity']['span_id'], 16) in first_ids
    assert archive['events'][0]['timestamp_ns'] == 100
    assert not hook._load_batch_events('g')


def test_streaming_timeout_closes_scopes_without_invented_callback_logs(runtime):
    tracer, exporter = runtime
    hook._create_session_context('s', {'session_id': 's'}, 'claude')
    hook._emit_streaming_event(tracer, 's', 'PreToolUse', {'session_id': 's', 'tool_use_id': 'call',
                              '_hook_observed_time_ns': 100}, 'claude', 'g')
    assert hook._finalize_session(tracer, 's', None, 'claude')
    spans = exporter.get_finished_spans()
    tool = next(s for s in spans if s.name == 'o11y.client.tool')
    assert tool.attributes['o11y.timing.basis'] == 'timeout'
    assert tool.attributes['o11y.timing.missing_end'] is True
    assert not any(s.name == 'o11y.client.hook.SessionEnd' for s in spans)


def test_exporter_initialization_failure_preserves_passive_response(runtime, monkeypatch):
    replies = []
    monkeypatch.setattr(hook, '_load_config', lambda: {})
    monkeypatch.setattr(hook, '_apply_config_env', lambda config: None)
    monkeypatch.setattr(hook, '_configure_logging', lambda: None)
    monkeypatch.setattr(hook, '_cleanup_state', lambda: None)
    monkeypatch.setattr(hook, '_load_input', lambda: {'hook_event_name': 'SessionStart', 'session_id': 's'})
    monkeypatch.setattr(hook, '_detect_ide', lambda data: 'claude')
    monkeypatch.setattr(hook, '_batch_enabled', lambda: False)
    monkeypatch.setattr(hook, '_init_tracing', lambda *a, **kw: (_ for _ in ()).throw(ValueError('bad exporter')))
    monkeypatch.setattr(hook, '_emit_stdout_response', lambda *args: replies.append(args))
    assert hook.main() == 0
    assert len(replies) == 1 and replies[0][0] == 'SessionStart'


def test_original_unicode_payload_facts_survive_metadata_only_capture(runtime, monkeypatch):
    tracer, _ = runtime
    original = {'tool_input': {'command': 'é'}}
    observed = hook.ClaudeEventAdapter().parse('PreToolUse', original).to_lifecycle_data()
    filtered = hook.ClaudeEventAdapter().parse('PreToolUse', CapturePolicy().filter(original)).to_lifecycle_data()
    filtered['_payload_facts'] = observed['_payload_facts']
    assert 'tool_input' not in filtered
    with tracer.start_as_current_span('metadata') as span:
        hook._populate_span(span, 'PreToolUse', filtered, 'claude')
    assert span.attributes['o11y.client.tool_input.bytes'] == len('{"command":"é"}'.encode())
    assert 'o11y.client.tool.input' not in span.attributes
    assert len(span.attributes['o11y.client.tool.input.sha256']) == 64


def test_identical_mcp_payloads_use_observation_identity_not_content():
    from otel_hooks.lifecycle import SessionEventDeduplicator
    state = {}
    dedup = SessionEventDeduplicator(state, 'g', 10, ttl_seconds=60)
    first = {'mcp_server': 'server', 'mcp_tool': 'search', '_hook_event_id': 'observation-1',
             '_hook_observed_time_ns': 10}
    second = {**first, '_hook_event_id': 'observation-2'}
    assert not dedup.is_duplicate('AfterMCPExecution', first, None)
    assert not dedup.is_duplicate('AfterMCPExecution', second, None)
    assert dedup.is_duplicate('AfterMCPExecution', first, None)


def test_structured_message_attribute_is_omitted_instead_of_cut_into_invalid_json(runtime, monkeypatch):
    tracer, _ = runtime
    monkeypatch.setattr(hook, '_CAPTURE_POLICY', CapturePolicy(mode='full', max_string_length=40))
    with tracer.start_as_current_span('hook') as span:
        hook._populate_span(span, 'UserPromptSubmit', {'prompt': 'x' * 1000}, 'claude')
    assert 'gen_ai.input.messages' not in span.attributes
    assert len(span.attributes['o11y.client.prompt.text']) == 40


def test_streaming_state_limit_emits_explicit_inferred_scope_closure():
    state = {'trace_id': '1' * 32, 'session_span_id': '2' * 16}
    for i in range(129):
        records = plan(state, 'PreToolUse', {'session_id': 's', 'tool_use_id': str(i),
                       '_hook_observed_time_ns': 100 + i}, 'g')
        state['stream_outbox'] = []  # emulate successful delivery
    assert len(state['stream_scopes']) == 128
    closed = next(r for r in records if r['name'] == 'o11y.client.tool')
    assert closed['basis'] == 'state_limit' and closed['missing']


def test_correlated_mcp_alias_callbacks_do_not_create_extra_invocation_scopes():
    state = {'trace_id': '1' * 32, 'session_span_id': '2' * 16}
    records = []
    for index, (event, invocation) in enumerate([
        ('PreToolUse', 'call'), ('BeforeMCPExecution', 'call'),
        ('BeforeMCPExecution', None), ('AfterMCPExecution', 'call'), ('PostToolUse', 'call'),
    ]):
        data = {'session_id': 's', '_hook_observed_time_ns': 100 + index}
        if invocation:
            data['tool_use_id'] = invocation
        records = plan(state, event, data, 'g')
    assert sum(record['name'] == 'o11y.client.tool' for record in records) == 1
    assert sum(record['name'].startswith('o11y.client.hook.') for record in records) == 5


def test_effective_privacy_and_log_settings_partition_pending_queues(runtime, monkeypatch):
    fingerprints = []
    for settings in ({}, {'capture': {'mask': True}}, {'capture': {'user_identity': True}}, {'logs': {'enabled': True}}):
        for key in ('IDE_OTEL_MASK_PROMPTS', 'IDE_OTEL_CAPTURE_USER_IDENTITY', 'IDE_OTEL_ENABLE_LOGS'):
            monkeypatch.delenv(key, raising=False)
        hook._apply_config_env(settings)
        fingerprints.append(hook._CAPTURE_POLICY.fingerprint)
    assert len(set(fingerprints)) == 4
