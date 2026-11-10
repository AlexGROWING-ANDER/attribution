"""Native transport checks with synthetic requests and mocked upstream bytes."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stepwise/engine"))
import responses_transport as transport


class ResponsesTransportTests(unittest.TestCase):
    def test_endpoint_accepts_base_and_complete_responses_url(self):
        for value in ("https://example.invalid/v1", "https://example.invalid/v1/responses", "https://example.invalid/v1/chat/completions/"):
            self.assertEqual(transport.responses_endpoint(value), "https://example.invalid/v1/responses")

    def test_endpoint_rejects_credentials_query_and_non_https(self):
        for value in ("http://example.invalid/v1", "https://name:secret@example.invalid/v1", "https://example.invalid/v1?key=value", "https://example.invalid/v1#fragment"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                transport.responses_endpoint(value)

    def request(self):
        return {"model": "deepseek-v4.1-flash", "stream": True, "store": False,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Synthetic material"}]}],
                "tools": [{"type": "namespace", "name": "functions", "tools": [
                    {"type": "function", "name": "view_image", "parameters": {"type": "object"}}]}],
                "reasoning": {"effort": "high"}, "text": {"format": {"type": "text"}}}

    def test_main_request_is_preserved_including_tools_and_reasoning(self):
        original = self.request()
        snapshot = copy.deepcopy(original)
        body, route = transport.route_request(original, "deepseek-v4.1-flash")
        self.assertEqual(body, snapshot)
        self.assertEqual(original, snapshot)
        self.assertEqual(route["purpose"], "main_agent")
        self.assertFalse(route["protocol_conversion"])

    def test_approval_only_changes_model_not_output_contract(self):
        original = {**self.request(), "model": "codex-auto-review"}
        body, route = transport.route_request(original, "deepseek-v4.1-flash")
        self.assertEqual(body, {**original, "model": "deepseek-v4.1-flash"})
        self.assertEqual(route["purpose"], "approval_surrogate")

    def test_unknown_model_and_server_side_history_are_rejected(self):
        for overrides in ({"model": "another-model"}, {"previous_response_id": "resp_previous"}, {"stream": False}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                transport.route_request({**self.request(), **overrides}, "deepseek-v4.1-flash")

    def completed_stream(self):
        payload = {"type": "response.completed", "response": {"status": "completed", "model": "deepseek-v4-1-flash-261017", "usage": {"input_tokens": 10, "output_tokens": 4}}}
        return b"event: response.created\r\ndata: {\"type\":\"response.created\"}\r\n\r\n" + b"data: " + json.dumps(payload).encode() + b"\n\n"

    def test_metadata_tolerates_chunk_boundaries_without_storing_text(self):
        event = {}
        observer = transport.StreamMetadata(event)
        wire = self.completed_stream()
        for byte in wire:
            observer.feed(bytes([byte]))
        observer.finish()
        self.assertEqual(event["usage"]["output_tokens"], 4)
        self.assertEqual(event["returned_model"], "deepseek-v4-1-flash-261017")
        self.assertNotIn("output", event)

    def test_incomplete_stream_is_not_marked_as_delivered(self):
        event = {}
        observer = transport.StreamMetadata(event)
        observer.feed(b'data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n')
        with self.assertRaisesRegex(ValueError, "completed"):
            observer.finish()
        self.assertNotIn("delivered_to_cli", event)

    def test_failed_terminal_event_does_not_count_as_completed(self):
        observer = transport.StreamMetadata({})
        observer.feed(b'data: {"type":"response.failed","response":{"status":"failed"}}\n\n')
        with self.assertRaisesRegex(ValueError, "completed"):
            observer.finish()

    def test_metadata_does_not_keep_reasoning_or_answer_deltas(self):
        event = {}
        observer = transport.StreamMetadata(event)
        observer.feed(b'data: {"type":"response.reasoning_summary_text.delta","delta":"synthetic reasoning"}\n\n')
        observer.feed(b'data: {"type":"response.output_text.delta","delta":"synthetic answer"}\n\n')
        observer.feed(self.completed_stream())
        observer.finish()
        self.assertNotIn("synthetic reasoning", json.dumps(event))
        self.assertNotIn("synthetic answer", json.dumps(event))

    def test_wire_bytes_are_forwarded_without_rebuilding_events(self):
        wire = self.completed_stream()
        class Reply(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/event-stream"}
        output = []
        event = {}
        body = self.request()
        with patch.object(transport.urllib.request, "urlopen", return_value=Reply(wire)) as send:
            transport.forward_responses("https://example.invalid/v1/responses", "synthetic", body, event, time.monotonic() + 10, lambda status, kind: None, output.append)
        self.assertEqual(b"".join(output), wire)
        self.assertEqual(json.loads(send.call_args.args[0].data), body)
        self.assertEqual(event["response_sha256"], hashlib.sha256(wire).hexdigest())
        self.assertTrue(event["delivered_to_cli"])

    def test_upstream_http_error_is_not_converted_into_chat_or_retried(self):
        event = {}
        error = urllib.error.HTTPError("https://example.invalid", 429, "Limited", {}, io.BytesIO(b"{}"))
        with patch.object(transport.urllib.request, "urlopen", side_effect=error) as send:
            with self.assertRaises(urllib.error.HTTPError):
                transport.forward_responses("https://example.invalid/v1/responses", "synthetic", self.request(), event, time.monotonic() + 10, lambda *args: None, lambda data: None)
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("delivered_to_cli", event)


if __name__ == "__main__":
    unittest.main()
