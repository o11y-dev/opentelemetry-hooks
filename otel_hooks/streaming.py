"""Persistable streaming scope planner; I/O and SDK emission belong to runtime."""
from .timing import branch, new_span_id
from .semconv import completed_data, scope_role, span_name

_START_END = {'PreToolUse': {'PostToolUse', 'PostToolUseFailure'},
              'BeforeShellExecution': {'AfterShellExecution'},
              'BeforeMCPExecution': {'AfterMCPExecution'},
              'SubagentStart': {'SubagentStop'}}


def plan(state, event, data, generation_key):
    now = data.get('_hook_event_time_ns') or data['_hook_observed_time_ns']
    generations = state.setdefault('stream_generations', {})
    pending = state.setdefault('stream_scopes', [])
    outbox = state.setdefault('stream_outbox', [])
    trace_id = state['trace_id']
    session_parent = state['session_span_id']
    state['identity_committed'] = True
    finished = state.setdefault('stream_finished_generations', [])
    generation = generations.get(generation_key)
    if generation_key and generation is None and generation_key not in finished:
        generation = generations[generation_key] = {
            'name': span_name('generation', event, data), 'role': 'generation', 'span_id': new_span_id(), 'trace_id': trace_id,
            'parent_span_id': session_parent, 'start': now, 'data': {**{k: data[k] for k in
                ('conversation_id', 'agent_name', 'agent_type', 'agent_version', 'agent_resource_id', 'model', 'request_model',
                 'provider_name', 'provider', 'model_provider', 'vendor') if k in data}, 'generation_id': generation_key,
                'session_id': data.get('session_id'), '_hook_event_time_ns': now,
                '_hook_observed_time_ns': data.get('_hook_observed_time_ns', now)},
        }
    parent = generation['span_id'] if generation else session_parent
    agent = data.get('agent_id') or data.get('subagent_id')
    agent_scope = next((p for p in reversed(pending) if p['event'] == 'SubagentStart'
                        and agent and p['data'].get('agent_id') == agent), None)
    if agent_scope and event != 'SubagentStart':
        parent = agent_scope['span_id']
    invocation = data.get('tool_use_id') or data.get('tool_call_id') or data.get('call_id')
    matching = next((p for p in reversed(pending) if event in _START_END[p['event']]
                     and branch(p['data']) == branch(data) and (
                         (invocation and invocation == p.get('invocation'))
                         or (event == 'SubagentStop' and agent))), None)
    existing_tool = next((p for p in reversed(pending) if p['event'] != 'SubagentStart'
                          and invocation and p.get('invocation') == invocation
                          and branch(p['data']) == branch(data)), None)
    if existing_tool and event in {'BeforeMCPExecution', 'AfterMCPExecution'}:
        parent = existing_tool['span_id']
    if event in _START_END and event != 'SubagentStart' and not invocation:
        pass  # No reliable invocation identity: retain callback evidence only.
    elif event in _START_END and existing_tool and event == 'BeforeMCPExecution':
        pass  # A correlated alias is another callback, not another invocation.
    elif event in _START_END:
        role = scope_role(event)
        scope = {'name': span_name(role, event, data), 'role': role,
                 'event': event, 'span_id': new_span_id(), 'trace_id': trace_id,
                 'parent_span_id': parent, 'start': now, 'data': data,
                 'invocation': invocation, 'generation_key': generation_key}
        pending.append(scope)
        parent = scope['span_id']
    elif matching:
        parent = matching['span_id']
        closed = dict(matching, end=now, basis='matching_callback', missing=False)
        # Keep original input and end failure facts without replacing start identity.
        closed['data'] = completed_data(matching['data'], data, event)
        closed['scope_only'] = True
        outbox.append(closed)
        pending.remove(matching)
    if not data.get('_scope_closure_only'):
        outbox.append({'name': span_name('callback', event, data), 'role': 'callback', 'event': event, 'span_id': new_span_id(),
                       'trace_id': trace_id, 'parent_span_id': parent, 'start': now,
                       'end': now, 'basis': 'observation', 'data': data})
    if event in {'Stop', 'StopFailure', 'Interrupt', 'AfterAgent', 'SessionEnd'}:
        # Async subagents can outlive a generation. Close only the observed branch.
        for scope in list(pending):
            if event == 'SessionEnd' or branch(scope['data']) == branch(data):
                outbox.append(dict(scope, end=now, basis='timeout' if data.get('_hook_timeout') else 'session_end' if event == 'SessionEnd' else 'generation_end',
                                   missing=True, scope_only=True))
                pending.remove(scope)
        keys = list(generations) if event == 'SessionEnd' else [generation_key]
        for key in keys:
            if key in generations:
                finished.append(key)
                del finished[:-128]
                finished_generation = generations.pop(key)
                outbox.append(dict(finished_generation, event=event, end=now,
                                   data=completed_data(finished_generation['data'], data, event),
                                   basis='timeout' if data.get('_hook_timeout') else 'matching_callback' if event != 'SessionEnd' else 'session_end', scope_only=True))
    # Bound unresolved scopes without silently forgetting an emitted parent ID.
    while len(pending) > 128:
        scope = pending.pop(0)
        outbox.append(dict(scope, end=now, basis='state_limit', missing=True, scope_only=True))
    while len(generations) > 128:
        key = next(iter(generations))
        outbox.append(dict(generations.pop(key), event=event, end=now,
                           basis='state_limit', missing=True, scope_only=True))
    return list(outbox)
