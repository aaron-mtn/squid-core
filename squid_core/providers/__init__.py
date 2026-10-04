"""Generic provider contracts and cancellation signal; no concrete transports."""

from squid_core.providers.base import BaseProvider, StreamCancelled

__all__ = ["BaseProvider", "StreamCancelled"]
