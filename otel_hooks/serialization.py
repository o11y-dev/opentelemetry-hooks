"""JSON serialization for diagnostic logs and exported spans; no SDK setup."""
import json
import logging
from datetime import datetime, timezone


# Standard Python LogRecord fields to exclude from the JSON "attributes" bucket
_LOG_RESERVED_ATTRS = frozenset({
    "args", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg",
    "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
    # Our own top-level keys (already emitted explicitly)
    "trace_id", "span_id",
})


class _JsonFormatter(logging.Formatter):
    """Emit diagnostic records as JSON with trace IDs and extra attributes."""

    def format(self, record: logging.LogRecord) -> str:
        # Build the base envelope
        obj = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "0"),
            "span_id": getattr(record, "span_id", "0"),
        }

        # Collect all extra attributes (anything not in the reserved set)
        attrs = {}
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _LOG_RESERVED_ATTRS:
                continue
            # Skip None values and internal callables
            if value is None or callable(value):
                continue
            try:
                # Ensure JSON-serializable
                json.dumps(value)
                attrs[key] = value
            except (TypeError, ValueError):
                attrs[key] = str(value)
        if attrs:
            obj["attributes"] = attrs

        # Include exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(obj, ensure_ascii=False, default=str)


def _span_to_dict(span) -> dict:
    """Serialize an OTel ReadableSpan to a JSON-compatible dict."""
    ctx = span.context
    resource = getattr(span, "resource", None)
    resource_attributes = dict(getattr(resource, "attributes", {}) or {})
    parent_id = None
    parent_ctx = span.parent
    if parent_ctx is not None and getattr(parent_ctx, "span_id", 0) != 0:
        parent_id = format(parent_ctx.span_id, "016x")
    links = []
    for link in getattr(span, "links", ()) or ():
        link_context = getattr(link, "context", None)
        if link_context is None or not getattr(link_context, "is_valid", False):
            continue
        links.append({
            "trace_id": format(link_context.trace_id, "032x"),
            "span_id": format(link_context.span_id, "016x"),
            "attributes": dict(getattr(link, "attributes", {}) or {}),
        })
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x") if ctx else None,
        "span_id": format(ctx.span_id, "016x") if ctx else None,
        "parent_span_id": parent_id,
        "start_time_ns": span.start_time,
        "end_time_ns": span.end_time,
        "attributes": dict(span.attributes or {}),
        "resource": resource_attributes,
        "links": links,
        "status": span.status.status_code.name if span.status else None,
    }


