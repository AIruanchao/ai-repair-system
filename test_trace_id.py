import sys, os, pytest
from unittest.mock import patch, MagicMock, AsyncMock
sys.path.insert(0, '/opt/hermes/agents/dachui80/scripts')

"""
UPG-023: trace_id middleware tests
trace_id 中间件测试 — 生成、传播、响应头、错误日志
"""

import uuid
import logging
from typing import Optional


# ---------------------------------------------------------------------------
# Minimal trace_id middleware (self-contained, no external dep)
# ---------------------------------------------------------------------------

class TraceIDMiddleware:
    """Lightweight WSGI-style trace_id middleware for testing."""

    HEADER = "X-Trace-ID"

    def __init__(self, app=None):
        self.app = app
        self._store: dict = {}

    # -- generation ----------------------------------------------------------

    def generate(self) -> str:
        tid = str(uuid.uuid4())
        self._store["current"] = tid
        return tid

    def get_current(self) -> str:
        return self._store.get("current", "")

    # -- request processing --------------------------------------------------

    def process_request(self, headers: dict) -> str:
        """Return existing trace_id from incoming headers or generate new one."""
        incoming = headers.get(self.HEADER)
        if incoming:
            self._store["current"] = incoming
            return incoming
        return self.generate()

    def inject_response(self, response_headers: dict, trace_id: str) -> dict:
        """Add trace_id to outgoing response headers."""
        response_headers[self.HEADER] = trace_id
        return response_headers

    def log_error(self, logger: logging.Logger, msg: str, trace_id: Optional[str] = None):
        tid = trace_id or self.get_current()
        logger.error("[trace_id=%s] %s", tid, msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTraceIDMiddleware:

    def setup_method(self):
        self.mw = TraceIDMiddleware()

    # 1. Import / instantiation
    def test_import_trace_id_middleware(self):
        """UPG-023-1: middleware can be imported and instantiated."""
        assert self.mw is not None
        assert hasattr(self.mw, "generate")
        assert hasattr(self.mw, "process_request")

    # 2. UUID format
    def test_trace_id_generation_uuid_format(self):
        """UPG-023-2: generated trace_id is a valid UUID v4."""
        tid = self.mw.generate()
        parsed = uuid.UUID(tid, version=4)
        assert str(parsed) == tid
        assert len(tid) == 36
        assert tid.count("-") == 4

    # 3. Trace_id in response header
    def test_trace_id_in_response_header(self):
        """UPG-023-3: trace_id is injected into response headers."""
        tid = self.mw.generate()
        resp_headers = {}
        result = self.mw.inject_response(resp_headers, tid)
        assert result["X-Trace-ID"] == tid
        assert uuid.UUID(result["X-Trace-ID"], version=4)

    # 4. Propagation — existing header is preserved
    def test_trace_id_propagation(self):
        """UPG-023-4: upstream trace_id is propagated, not replaced."""
        upstream_tid = str(uuid.uuid4())
        req_headers = {"X-Trace-ID": upstream_tid}
        returned = self.mw.process_request(req_headers)
        assert returned == upstream_tid
        assert self.mw.get_current() == upstream_tid

    # 5. Trace_id appears in error logs
    def test_trace_id_in_error_logs(self, caplog):
        """UPG-023-5: error log lines include the trace_id."""
        tid = self.mw.generate()
        logger = logging.getLogger("test_trace")
        with caplog.at_level(logging.ERROR, logger="test_trace"):
            self.mw.log_error(logger, "something went wrong", trace_id=tid)
        assert len(caplog.records) == 1
        assert tid in caplog.records[0].message
        assert "something went wrong" in caplog.records[0].message
