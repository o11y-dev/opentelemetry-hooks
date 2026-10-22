"""GenAI operation identity, separate from hook callback observations.

Reference: open-telemetry/semantic-conventions-genai at 9ffc7e0.
Only reconstructed invocations represent execute_tool / invoke_agent operations.
"""
import json


TOOL_EVENTS = {'PreToolUse', 'PostToolUse', 'PostToolUseFailure',
               'BeforeShellExecution', 'AfterShellExecution',
               'BeforeMCPExecution', 'AfterMCPExecution'}


def text(value):
    return value if isinstance(value, str) and value.strip() else None


def tool_name(event, data):
    return text(data.get('mcp_tool')) or text(data.get('tool_name'))


def json_attribute(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None


def object_attribute(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return json_attribute(value) if isinstance(value, dict) else None


def tool_definitions(value):
    if not isinstance(value, list):
        return None
    definitions = []
    for tool in value:
        if not isinstance(tool, dict):
            return None
        function = tool.get('function')
        definition = function if isinstance(function, dict) else tool
        name = text(definition.get('name'))
        kind = text(tool.get('type')) or ('function' if isinstance(function, dict) or 'input_schema' in tool else None)
        if not name or not kind:
            return None
        definitions.append({'type': kind, 'name': name})
    return json_attribute(definitions)


def operation_attributes(role, event, data):
    attrs = {}
    if role in {'generation', 'subagent'}:
        attrs['gen_ai.operation.name'] = 'invoke_agent'
        name = text(data.get('agent_name')) or text(data.get('subagent_type')) or text(data.get('agent_type'))
        if name:
            attrs['gen_ai.agent.name'] = name
    elif role == 'tool' and tool_name(event, data):
        attrs['gen_ai.operation.name'] = 'execute_tool'
        attrs['gen_ai.tool.name'] = tool_name(event, data)
    return attrs


def span_name(role, event, data):
    attrs = operation_attributes(role, event, data)
    operation = attrs.get('gen_ai.operation.name')
    if operation:
        target = attrs.get('gen_ai.tool.name') or attrs.get('gen_ai.agent.name')
        return operation + (' ' + target if target else '')
    return 'o11y.client.hook.' + event if role == 'callback' else 'o11y.client.' + role


def scope_role(event):
    if event == 'SubagentStart':
        return 'subagent'
    if event in {'PreToolUse', 'BeforeShellExecution', 'BeforeMCPExecution'}:
        return 'tool'
    return 'callback'


def completed_data(start, end, event):
    """Preserve start identity and attach the matching terminal result facts."""
    result = {**start, **{key: end[key] for key in (
        'status', 'exit_code', 'error', 'error_length', 'error_type', 'error_code',
        'is_interrupt', '_hook_failure_observed', 'tool_response', 'tool_output',
        'mcp_output', 'result_json', 'tool_response_truncated', 'is_partial', 'truncated',
    ) if key in end}}
    result['_payload_facts'] = {**start.get('_payload_facts', {}), **end.get('_payload_facts', {})}
    if event in {'PostToolUseFailure', 'StopFailure'}:
        result['_hook_failure_observed'] = True
    if event == 'Interrupt':
        result['is_interrupt'] = True
    return result
