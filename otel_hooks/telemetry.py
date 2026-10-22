"""Schema-v2 facts and bounded canonical attributes (independent of the SDK)."""
import json
import math


def usage_attributes(data, provider):
    """Normalize only known accounting models; never add output subsets twice."""
    source = {**data, **(data.get('usage') if isinstance(data.get('usage'), dict) else {})}

    def number(*paths):
        for path in paths:
            value = source
            for key in path.split('.'):
                value = value.get(key) if isinstance(value, dict) else None
            if type(value) is int and value >= 0:
                return value
        return None

    inp = number('input_tokens', 'prompt_tokens')
    out = number('output_tokens', 'completion_tokens')
    read = number('cache_read_input_tokens', 'cached_input_tokens', 'cache_read_tokens',
                  'input_tokens_details.cached_tokens', 'prompt_tokens_details.cached_tokens')
    write = number('cache_write_input_tokens', 'cache_creation_input_tokens',
                   'cache_creation_tokens', 'cache_write_tokens')
    reasoning = number('reasoning_tokens', 'output_tokens_details.reasoning_tokens',
                       'completion_tokens_details.reasoning_tokens')
    basis = 'provider_reported'
    # Anthropic reports new input separately from cache reads and writes.
    if provider == 'anthropic' and inp is not None and number('input_tokens') is not None:
        inp += (read or 0) + (write or 0)
        basis = 'provider_normalized_inclusive'
    total = number('total_tokens')
    if inp is not None and out is not None:
        total = inp + out
    result = {}
    for key, value in [('input_tokens', inp), ('output_tokens', out), ('total_tokens', total),
                       ('cache_read.input_tokens', read), ('cache_write.input_tokens', write),
                       ('cache_creation.input_tokens', write),
                       ('reasoning.output_tokens', reasoning)]:
        if value is not None:
            result['gen_ai.usage.' + key] = value
    if total is not None:
        # total_tokens is not in the OTel registry. Keep the historical name as
        # an alias while identifying the derived total in our own namespace.
        result['o11y.usage.total_tokens'] = total
    if result:
        result['o11y.usage.basis'] = basis
        if inp is not None:
            result['o11y.usage.input_basis'] = basis
        if out is not None:
            result['o11y.usage.output_basis'] = 'provider_reported'
        if source.get('usage_semantics') in {'per_call', 'cumulative', 'delta', 'correction'}:
            result['o11y.usage.semantics'] = source['usage_semantics']
    return result


class AttributeBudget:
    """Collect canonical fields first, then fit optional extras in the same budget."""
    def __init__(self, span, policy):
        self.span, self.policy, self.attributes = span, policy, {}

    def set_attribute(self, key, value):
        if value is not None:
            self.attributes[key] = value

    def finish(self, extras):
        used, count, dropped = 0, 0, 0
        sdk_limit = getattr(getattr(self.span, '_attributes', None), 'maxlen', None)
        limit = min(self.policy.max_attributes, sdk_limit if type(sdk_limit) is int else 128)
        selected = []
        # Identity and numerical facts survive before bulky content.
        def priority(item):
            key, value = item
            return (0 if key.startswith(('gen_ai.client.hook.', 'o11y.', 'gen_ai.usage.'))
                    or key.endswith(('_id', '.id')) or type(value) in (bool, int, float) else 1)
        for key, value in sorted(self.attributes.items(), key=priority) + list(extras.items()):
            if key in extras and key in self.attributes and value != self.attributes[key]:
                dropped += 1
                continue
            shortened = False
            protected_string = key.endswith(('_id', '.id', '.sha256')) or key.startswith('gen_ai.client.hook.') or key in {'o11y.capture.policy_id', 'o11y.capture.mode'}
            if isinstance(value, str) and key in {'gen_ai.input.messages', 'gen_ai.output.messages', 'gen_ai.tool.definitions'} and len(value) > self.policy.max_string_length:
                dropped += 1
                continue
            if isinstance(value, str) and not protected_string:
                shortened = len(value) > self.policy.max_string_length
                value = value[:self.policy.max_string_length]
            elif isinstance(value, (list, tuple)):
                shortened = len(value) > self.policy.max_array_length
                value = [v[:self.policy.max_string_length] if isinstance(v, str) else v
                         for v in value[:self.policy.max_array_length]]
            if isinstance(value, float) and not math.isfinite(value):
                dropped += 1
                continue
            size = len(key.encode('utf-8')) + len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
            if count >= max(1, limit - 1) or used + size > self.policy.max_attribute_bytes:
                dropped += 1
                continue
            selected.append((key, value))
            count += 1
            used += size
            if shortened:
                dropped += 1
        # Emit lowest priority first so SDK limits preserve canonical facts.
        for key, value in reversed(selected):
            self.span.set_attribute(key, value)
        if dropped:
            self.span.set_attribute('o11y.capture.shortened_or_dropped_fields', dropped)
