"""
Exercise the repository's Telnyx call sites with the real telnyx SDK against a
local HTTP server (no HTTP-layer mocks), so the test behaves the same on
classic telnyx 2.x and on 4.x.
"""

import asyncio
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import pytest

API_KEY = "KEY0123456789_breakwater"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        self.server.requests.append(
            {
                "method": self.command,
                "path": urlparse(self.path).path,
                "query": urlparse(self.path).query,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )
        return urlparse(self.path).path

    def do_POST(self):
        path = self._record()
        if path.rstrip("/") == "/v2/messages":
            self._reply(
                200,
                {
                    "data": {
                        "record_type": "message",
                        "direction": "outbound",
                        "id": "40385f64-5717-4562-b3fc-2c963f66afa6",
                        "type": "SMS",
                        "text": "hello from breakwater",
                        "from": {
                            "phone_number": "+13125550001",
                            "carrier": "T-Mobile",
                            "line_type": "Wireless",
                        },
                        "to": [
                            {
                                "phone_number": "+13125550002",
                                "status": "queued",
                                "carrier": "T-Mobile",
                                "line_type": "Wireless",
                            }
                        ],
                        "parts": 1,
                    }
                },
            )
        else:
            self._reply(404, {"errors": [{"code": "10005", "title": "Resource not found"}]})

    def do_GET(self):
        path = self._record()
        if path.rstrip("/") == "/v2/available_phone_numbers":
            self._reply(
                200,
                {
                    "data": [
                        {
                            "record_type": "available_phone_number",
                            "phone_number": "+13125550123",
                            "vanity_format": "",
                            "best_effort": False,
                            "quickship": True,
                            "reservable": True,
                            "region_information": [
                                {"region_type": "country_code", "region_name": "US"}
                            ],
                            "cost_information": {
                                "upfront_cost": "1.00",
                                "monthly_cost": "1.00",
                                "currency": "USD",
                            },
                            "features": [{"name": "sms"}, {"name": "voice"}],
                        }
                    ],
                    "meta": {
                        "total_results": 1,
                        "best_effort_results": 0,
                        "number_of_pages": 1,
                        "page_number": 1,
                        "page_size": 1,
                    },
                },
            )
        else:
            self._reply(404, {"errors": [{"code": "10005", "title": "Resource not found"}]})


def _load_voice_module(name):
    # backend/voice/__init__.py imports modules that fail to import in this
    # repository, so load the module file directly (it only needs the stdlib).
    path = os.path.join(os.path.dirname(__file__), "..", "backend", "voice", name + ".py")
    spec = importlib.util.spec_from_file_location("breakwater_voice_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def telnyx_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % server.server_address[1]

    # Classic SDK reads telnyx.api_base; 4.x reads TELNYX_BASE_URL when a client is created.
    monkeypatch.setenv("TELNYX_API_BASE", url)
    monkeypatch.setenv("TELNYX_BASE_URL", url + "/v2")
    monkeypatch.setenv("TELNYX_API_KEY", API_KEY)
    monkeypatch.setenv("TELEPHONY_PROVIDER", "telnyx")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

    import telnyx

    monkeypatch.setattr(telnyx, "api_base", url, raising=False)

    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sms_manager_sends_telnyx_message(telnyx_server):
    sms = _load_voice_module("sms")
    SMSManager, SMSStatus = sms.SMSManager, sms.SMSStatus

    manager = SMSManager()
    assert manager.is_configured()

    message = asyncio.run(
        manager.send_sms(
            from_number="+13125550001",
            to_number="+13125550002",
            body="hello from breakwater",
            user_id="user_1",
            agent_id="agent_1",
        )
    )

    assert message.error_message is None
    assert message.status == SMSStatus.SENT
    assert message.provider_sid == "40385f64-5717-4562-b3fc-2c963f66afa6"

    posts = [r for r in telnyx_server.requests if r["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["path"] == "/v2/messages"
    assert posts[0]["auth"] == "Bearer " + API_KEY
    assert posts[0]["body"]["to"] == "+13125550002"
    assert posts[0]["body"]["text"] == "hello from breakwater"


def test_telephony_manager_searches_telnyx_numbers(telnyx_server):
    TelephonyManager = _load_voice_module("telephony").TelephonyManager

    manager = TelephonyManager()
    assert manager.is_configured()

    numbers = asyncio.run(manager.search_available_numbers(country="US", limit=5))

    assert len(numbers) == 1
    assert numbers[0]["phone_number"] == "+13125550123"
    assert numbers[0]["provider"] == "telnyx"
    assert numbers[0]["region"] == "US"

    gets = [r for r in telnyx_server.requests if r["method"] == "GET"]
    assert len(gets) == 1
    assert gets[0]["path"] == "/v2/available_phone_numbers"
    assert gets[0]["auth"] == "Bearer " + API_KEY
