"""Schema-v2 acceptance checks against SDK spans and decoded OTLP protobuf."""
import json
import logging

import pytest
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
    monkeypatch.setattr(hook, '_force_flush_provider', lambda **kwargs: True)
    monkeypatch.setattr(hook, '_resolve_repository_context', lambda data: {})
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=PersistedIdGenerator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer('ide-hooks', '2'), exporter
    provider.shutdown()


def test_usage_inclusive_cache_and_reasoning_with_real_zeros():
    result = usage_attributes({'input_tokens': 300, 'cache_read_input_tokens': 600,
                               'cache_creation_input_tokens': 100, 'output_tokens': 200,
                               'reasoning_tokens': 50}, 'anthropic')
    assert result['gen_ai.usage.input_tokens'] == 1000
    assert result['gen_ai.usage.total_tokens'] == 1200
    assert result['gen_ai.usage.reasoning.output_tokens'] == 50
    assert usage_attributes({'input_tokens': 0, 'output_tokens': 0}, 'openai')['gen_ai.usage.total_tokens'] == 0
    assert 'gen_ai.usage.input_tokens' not in usage_attributes({'input_tokens': True}, None)
    assert usage_attributes({'input_tokens': 1000, 'output_tokens': 200,
                             'cache_read_input_tokens': 600}, 'openai')['gen_ai.usage.total_tokens'] == 1200


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
    generation = next(s for s in second if s.name == 'gen_ai.client.generation')
    assert f'{generation.parent.span_id:016x}' == context['session_span_id']
    pre, post, stop = [s for s in second if '.hook.' in s.name]
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
    by_name = {s.name: s for s in spans}
    assert by_name['gen_ai.client.tool'].end_time == 60
    assert by_name['gen_ai.client.subagent'].end_time == 70
    assert by_name['gen_ai.client.generation'].end_time == 40
    assert by_name['gen_ai.client.tool'].parent.span_id == by_name['gen_ai.client.subagent'].context.span_id
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
    for target in ('gen_ai.usage.input_tokens', 'gen_ai.client.session_id', 'gen_ai.client.status', 'gen_ai.tool.call.id'):
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
    assert span.attributes['gen_ai.client.prompt.bytes'] == 200
    assert span.attributes['gen_ai.client.prompt.length'] == 100
    assert len(span.attributes['gen_ai.client.prompt.text']) == 10
    assert 'o11y.capture.shortened_or_dropped_fields' in span.attributes


def test_streaming_missing_end_is_explicit():
    state = {'trace_id': '1' * 32, 'session_span_id': '2' * 16}
    plan(state, 'PreToolUse', {'session_id': 's', 'tool_use_id': 't', '_hook_observed_time_ns': 10}, 'g')
    records = plan(state, 'SessionEnd', {'session_id': 's', '_hook_observed_time_ns': 30}, None)
    tool = next(r for r in records if r['name'] == 'gen_ai.client.tool')
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
    assert span.attributes['gen_ai.client.hook.event_id'] == 'callback-long'
    assert span.attributes['gen_ai.tool.call.id'] == 'invocation-long'
    assert len(span.attributes['gen_ai.client.tool_input.sha256']) == 64


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
    tool = next(s for s in spans if s.name == 'gen_ai.client.tool')
    assert tool.attributes['o11y.timing.basis'] == 'timeout'
    assert tool.attributes['o11y.timing.missing_end'] is True
    assert not any(s.name == 'gen_ai.client.hook.SessionEnd' for s in spans)


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
    assert span.attributes['gen_ai.client.tool_input.bytes'] == len('{"command":"é"}'.encode())
    assert 'gen_ai.client.tool.input' not in span.attributes
    assert len(span.attributes['gen_ai.client.tool.input.sha256']) == 64


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
    assert len(span.attributes['gen_ai.client.prompt.text']) == 40


def test_streaming_state_limit_emits_explicit_inferred_scope_closure():
    state = {'trace_id': '1' * 32, 'session_span_id': '2' * 16}
    for i in range(129):
        records = plan(state, 'PreToolUse', {'session_id': 's', 'tool_use_id': str(i),
                       '_hook_observed_time_ns': 100 + i}, 'g')
        state['stream_outbox'] = []  # emulate successful delivery
    assert len(state['stream_scopes']) == 128
    closed = next(r for r in records if r['name'] == 'gen_ai.client.tool')
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
    assert sum(record['name'] == 'gen_ai.client.tool' for record in records) == 1
    assert sum(record['name'].startswith('gen_ai.client.hook.') for record in records) == 5


def test_effective_privacy_and_log_settings_partition_pending_queues(runtime, monkeypatch):
    fingerprints = []
    for settings in ({}, {'capture': {'mask': True}}, {'capture': {'user_identity': True}}, {'logs': {'enabled': True}}):
        for key in ('IDE_OTEL_MASK_PROMPTS', 'IDE_OTEL_CAPTURE_USER_IDENTITY', 'IDE_OTEL_ENABLE_LOGS'):
            monkeypatch.delenv(key, raising=False)
        hook._apply_config_env(settings)
        fingerprints.append(hook._CAPTURE_POLICY.fingerprint)
    assert len(set(fingerprints)) == 4
