"""Opencode hook parsing; independent of the telemetry runtime."""
from .base import BaseParser


class OpenCodeEventAdapter(BaseParser):
    provider = "opencode"
