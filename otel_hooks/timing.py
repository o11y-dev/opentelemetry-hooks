"""Branch-local callback boundaries and stable export identities."""
import contextlib
import contextvars
import secrets

_IDENTITIES = contextvars.ContextVar('hook_span_identity', default=None)


class PersistedIdGenerator:
    """SDK IdGenerator protocol without importing or initializing the SDK."""
    def generate_span_id(self):
        identity = _IDENTITIES.get()
        return int(identity['span_id'], 16) if identity else secrets.randbits(64) or 1

    def generate_trace_id(self):
        identity = _IDENTITIES.get()
        return int(identity['trace_id'], 16) if identity else secrets.randbits(128) or 1

    def is_trace_id_random(self):
        return False


def new_span_id():
    return f'{secrets.randbits(64) or 1:016x}'


@contextlib.contextmanager
def export_identity(trace_id, span_id):
    token = _IDENTITIES.set({'trace_id': trace_id, 'span_id': span_id})
    try:
        yield
    finally:
        _IDENTITIES.reset(token)


def branch(data):
    return (data.get('session_id') or data.get('conversation_id'),
            data.get('agent_id') or data.get('subagent_id') or '')


_PAIRS = {'PreToolUse': {'PostToolUse', 'PostToolUseFailure'},
          'BeforeShellExecution': {'AfterShellExecution'},
          'BeforeMCPExecution': {'AfterMCPExecution'},
          'SubagentStart': {'SubagentStop'}, 'BeforeModel': {'AfterModel'},
          'BeforeReadFile': {'AfterReadFile'}, 'BeforeFileEdit': {'AfterFileEdit'}}


def boundaries(batch):
    """Keep observed timestamps; infer intervals only inside the same branch."""
    result = []
    for index, entry in enumerate(batch):
        data = entry.get('data') or {}
        start = entry['timestamp_ns']
        later = [e for e in batch[index + 1:] if branch(e.get('data') or {}) == branch(data)]
        ends = _PAIRS.get(entry.get('event'), set())
        matched = matching_terminal(batch, index)
        if matched:
            result.append((matched['timestamp_ns'], 'matching_callback', False))
        elif later:
            result.append((later[0]['timestamp_ns'], 'next_callback', bool(ends)))
        else:
            # The terminal callback is an observation, not a fabricated 1ms interval.
            result.append((start, 'observation', bool(ends)))
    return result


def matching_terminal(batch, index):
    entry = batch[index]
    data = entry.get('data') or {}
    invocation = data.get('tool_use_id') or data.get('tool_call_id') or data.get('call_id')
    return next((other for other in batch[index + 1:]
                 if branch(other.get('data') or {}) == branch(data)
                 and other.get('event') in _PAIRS.get(entry.get('event'), set())
                 and ((invocation and invocation == ((other.get('data') or {}).get('tool_use_id')
                       or (other.get('data') or {}).get('tool_call_id') or (other.get('data') or {}).get('call_id')))
                      or (entry.get('event') == 'SubagentStart' and data.get('agent_id')))), None)
