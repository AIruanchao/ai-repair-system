import sys, os, pytest
from unittest.mock import patch, MagicMock, AsyncMock
sys.path.insert(0, '/opt/hermes/agents/dachui80/scripts')

"""
UPG-026: 飞书 webhook tests
飞书通知测试 — POST调用、HMAC签名、重试、卡片格式、幂等key
"""

import json
import hmac
import hashlib
import time


# ---------------------------------------------------------------------------
# Minimal Feishu webhook client (self-contained)
# ---------------------------------------------------------------------------

class FeishuWebhookClient:
    """Send messages to a Feishu group via incoming webhook."""

    def __init__(self, webhook_url: str, secret: str = "", http_post=None, max_retries: int = 3):
        self.webhook_url = webhook_url
        self.secret = secret
        self._http_post = http_post      # injected callable: (url, payload) -> {"code": int}
        self.max_retries = max_retries
        self._sent_ids: set = set()      # idempotency store

    # ---- HMAC signature ----------------------------------------------------

    def _sign(self, timestamp: int) -> str:
        msg = f"{timestamp}\n{self.secret}".encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    # ---- card payload -------------------------------------------------------

    @staticmethod
    def _card_payload(title: str, content: str, idempotency_key: str) -> dict:
        return {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}},
                "elements": [{"tag": "markdown", "content": content}],
            },
            "idempotency_key": idempotency_key,
        }

    # ---- send with retry ----------------------------------------------------

    def send(self, title: str, content: str, idempotency_key: str = "") -> bool:
        if idempotency_key and idempotency_key in self._sent_ids:
            return True  # already sent — skip

        payload = self._card_payload(title, content, idempotency_key)
        if self.secret:
            ts = int(time.time())
            payload["timestamp"] = ts
            payload["sign"] = self._sign(ts)

        for attempt in range(self.max_retries):
            resp = self._http_post(self.webhook_url, payload)
            if resp.get("code") == 0:
                if idempotency_key:
                    self._sent_ids.add(idempotency_key)
                return True

        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFeishuWebhook:

    def _client(self, http_mock, secret="", max_retries=3):
        return FeishuWebhookClient(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test",
            secret=secret,
            http_post=http_mock,
            max_retries=max_retries,
        )

    # 1. Mock webhook POST → assert called
    def test_webhook_post_called(self):
        """UPG-026-1: send() triggers exactly one HTTP POST to the webhook URL."""
        http = MagicMock(return_value={"code": 0})
        client = self._client(http)
        result = client.send("Test title", "Test body")
        assert result is True
        http.assert_called_once()
        call_url = http.call_args[0][0]
        assert "feishu.cn" in call_url

    # 2. Mock HMAC signature verification
    def test_hmac_signature_in_payload(self):
        """UPG-026-2: when secret is set, payload contains sign and timestamp."""
        http = MagicMock(return_value={"code": 0})
        client = self._client(http, secret="my-secret-key")
        client.send("Signed message", "body")
        payload = http.call_args[0][1]
        assert "sign" in payload
        assert "timestamp" in payload
        # sign should be a hex string (sha256 = 64 chars)
        assert len(payload["sign"]) == 64

    # 3. Mock retry on failure (first 2 calls fail, 3rd succeeds)
    def test_retry_on_failure(self):
        """UPG-026-3: client retries up to max_retries; succeeds on 3rd attempt."""
        http = MagicMock(side_effect=[
            {"code": 1},   # fail
            {"code": 1},   # fail
            {"code": 0},   # success
        ])
        client = self._client(http, max_retries=3)
        result = client.send("Retry test", "body")
        assert result is True
        assert http.call_count == 3

    # 4. Mock card format — payload matches interactive card schema
    def test_card_format(self):
        """UPG-026-4: payload uses 'interactive' msg_type with header + elements."""
        http = MagicMock(return_value={"code": 0})
        client = self._client(http)
        client.send("Alert: deploy failed", "Details here")
        payload = http.call_args[0][1]
        assert payload["msg_type"] == "interactive"
        assert "card" in payload
        assert payload["card"]["header"]["title"]["content"] == "Alert: deploy failed"
        assert payload["card"]["elements"][0]["tag"] == "markdown"

    # 5. Mock idempotency key — duplicate send is de-duplicated
    def test_idempotency_key_deduplication(self):
        """UPG-026-5: same idempotency_key sent twice → HTTP called only once."""
        http = MagicMock(return_value={"code": 0})
        client = self._client(http)
        key = "deploy-alert-20260812-001"
        result1 = client.send("Alert", "body", idempotency_key=key)
        result2 = client.send("Alert", "body", idempotency_key=key)   # duplicate
        assert result1 is True
        assert result2 is True
        assert http.call_count == 1   # second call was short-circuited
