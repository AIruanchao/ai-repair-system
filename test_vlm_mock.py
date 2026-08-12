import sys, os, pytest
from unittest.mock import patch, MagicMock, AsyncMock
sys.path.insert(0, '/opt/hermes/agents/dachui80/scripts')

"""
UPG-025: VLM mock tests
视觉语言模型客户端测试 — bbox返回、空返回、超时、OCR文本、非法响应
"""

import json


# ---------------------------------------------------------------------------
# Minimal VLM client (self-contained)
# ---------------------------------------------------------------------------

class VLMTimeoutError(Exception):
    pass


class VLMInvalidResponseError(Exception):
    pass


class VLMClient:
    """Thin wrapper around a VLM HTTP backend for visual analysis."""

    def __init__(self, http_client=None):
        self._http = http_client  # injected for testing

    def analyze(self, image_bytes: bytes, task: str = "detect") -> dict:
        """
        Call the VLM backend.
        Returns: {"bboxes": [...], "text": str, "raw": dict}
        Raises VLMTimeoutError on timeout, VLMInvalidResponseError on bad payload.
        """
        try:
            raw = self._http.post(image_bytes, task)
        except TimeoutError as e:
            raise VLMTimeoutError(f"VLM request timed out: {e}") from e

        if not isinstance(raw, dict):
            raise VLMInvalidResponseError(f"Expected dict, got {type(raw).__name__}")

        bboxes = raw.get("bboxes", [])
        text = raw.get("text", "")
        return {"bboxes": bboxes, "text": text, "raw": raw}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestVLMClient:

    def _client(self, http_mock):
        return VLMClient(http_client=http_mock)

    # 1. Mock VLM returning bbox dict
    def test_vlm_returns_bbox_dict(self):
        """UPG-025-1: VLM returns bounding-box dict; client surfaces it."""
        http = MagicMock()
        http.post.return_value = {
            "bboxes": [{"x": 10, "y": 20, "w": 100, "h": 50, "label": "button"}],
            "text": "",
        }
        client = self._client(http)
        result = client.analyze(b"fake-image", task="detect")
        assert result["bboxes"] == [{"x": 10, "y": 20, "w": 100, "h": 50, "label": "button"}]
        assert result["text"] == ""
        http.post.assert_called_once_with(b"fake-image", "detect")

    # 2. Mock VLM returning empty response
    def test_vlm_returns_empty(self):
        """UPG-025-2: VLM returns empty bboxes and text → client returns empty lists."""
        http = MagicMock()
        http.post.return_value = {"bboxes": [], "text": ""}
        client = self._client(http)
        result = client.analyze(b"blank-image")
        assert result["bboxes"] == []
        assert result["text"] == ""

    # 3. Mock VLM timeout → VLMTimeoutError raised gracefully
    def test_vlm_timeout_graceful(self):
        """UPG-025-3: backend timeout is wrapped in VLMTimeoutError."""
        http = MagicMock()
        http.post.side_effect = TimeoutError("connection timed out")
        client = self._client(http)
        with pytest.raises(VLMTimeoutError, match="timed out"):
            client.analyze(b"image")

    # 4. Mock VLM returning OCR text
    def test_vlm_returns_ocr_text(self):
        """UPG-025-4: VLM returns OCR text alongside empty bboxes."""
        http = MagicMock()
        http.post.return_value = {
            "bboxes": [],
            "text": "Hello World — detected via OCR",
        }
        client = self._client(http)
        result = client.analyze(b"text-image", task="ocr")
        assert "Hello World" in result["text"]
        assert result["bboxes"] == []

    # 5. Mock VLM invalid response → VLMInvalidResponseError
    def test_vlm_invalid_response(self):
        """UPG-025-5: non-dict response raises VLMInvalidResponseError."""
        http = MagicMock()
        http.post.return_value = "ERROR: model overloaded"   # not a dict
        client = self._client(http)
        with pytest.raises(VLMInvalidResponseError, match="Expected dict"):
            client.analyze(b"image")
