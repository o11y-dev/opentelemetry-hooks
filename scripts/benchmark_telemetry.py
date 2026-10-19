"""Compare synthetic hook CLI latency, peak RSS and OTLP bytes without a collector.

Usage: python scripts/benchmark_telemetry.py BASELINE_CHECKOUT CURRENT_CHECKOUT
Requires the package dependencies in the interpreter running this script.
"""
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

RUNNER = r'''
import json, os, resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk._logs.export import LogExportResult

def measure(signal, payload):
    with open(os.environ['BENCH_OUTPUT'], 'a') as f:
        f.write(json.dumps({'signal': signal, 'bytes': len(payload.SerializeToString())}) + '\n')
def traces(self, spans):
    measure('traces', encode_spans(spans))
    return SpanExportResult.SUCCESS
def logs(self, records):
    measure('logs', encode_logs(records))
    return LogExportResult.SUCCESS
OTLPSpanExporter.export = traces
OTLPLogExporter.export = logs
from otel_hooks.cli import cli
try:
    cli()
finally:
    with open(os.environ['BENCH_OUTPUT'], 'a') as f:
        f.write(json.dumps({'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}) + '\n')
'''

EVENTS = [
    {'hook_event_name': 'SessionStart', 'session_id': 'benchmark'},
    {'hook_event_name': 'UserPromptSubmit', 'session_id': 'benchmark', 'prompt': 'Unicode é prompt ' * 100},
    {'hook_event_name': 'PreToolUse', 'session_id': 'benchmark', 'tool_use_id': 'call',
     'tool_name': 'Bash', 'tool_input': {'command': 'echo hello'}},
    {'hook_event_name': 'PostToolUse', 'session_id': 'benchmark', 'tool_use_id': 'call',
     'tool_name': 'Bash', 'tool_response': {'output': 'Unicode é result ' * 100}},
    {'hook_event_name': 'Stop', 'session_id': 'benchmark', 'last_assistant_message': 'done'},
    {'hook_event_name': 'SessionEnd', 'session_id': 'benchmark'},
]


def benchmark(checkout, mode, iterations=3):
    durations, rss, sizes = [], [], []
    for _ in range(iterations):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            output = root / 'measurements.jsonl'
            config.write_text(json.dumps({
                'capture': {'mode': mode}, 'logs': {'enabled': mode == 'full'},
                'batch': {'on_stop': True, 'disable_sdk_batch': True}, 'local_spans': False,
                'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.example.test:4318',
                'OTEL_EXPORTER_OTLP_PROTOCOL': 'http/protobuf',
            }))
            env = {k: v for k, v in os.environ.items() if not k.startswith(('OTEL_', 'IDE_OTEL_', 'PYTHONPATH'))}
            env.update(IDE_OTEL_HOOK_HOME=str(root), IDE_OTEL_CONFIG=str(config), BENCH_OUTPUT=str(output))
            for event in EVENTS:
                start = time.perf_counter()
                result = subprocess.run([sys.executable, '-c', RUNNER, '--claude'], cwd=checkout, env=env,
                                        input=json.dumps(event), text=True, capture_output=True, timeout=15)
                if result.returncode:
                    raise RuntimeError(result.stderr)
                durations.append((time.perf_counter() - start) * 1000)
            measurements = [json.loads(line) for line in output.read_text().splitlines()]
            rss.extend(m['rss_kib'] for m in measurements if 'rss_kib' in m)
            sizes.append(sum(m.get('bytes', 0) for m in measurements))
    return {'processes': len(durations), 'median_cli_ms': round(statistics.median(durations), 2),
            'p95_cli_ms': round(sorted(durations)[int(.95 * (len(durations) - 1))], 2),
            'peak_rss_kib': max(rss), 'mean_otlp_bytes_per_session': round(statistics.mean(sizes), 2)}


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    print(json.dumps({label: {mode: benchmark(str(Path(path).resolve()), mode)
                             for mode in ('metadata_only', 'full')}
                      for label, path in zip(('baseline', 'schema_v2'), sys.argv[1:3])}, indent=2))
