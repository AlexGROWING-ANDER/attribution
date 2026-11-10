"""Forward native Responses requests and SSE bytes without Chat conversion."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit


def responses_endpoint(base: str) -> str:
    url = urlsplit(base.rstrip("/"))
    if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError("GROUND_URL must be an HTTPS endpoint without credentials or query parameters")
    path = url.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    if not path.endswith("/responses"):
        path += "/responses"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def route_request(body: dict, model: str) -> tuple[dict, dict]:
    incoming = body.get("model")
    if incoming not in (model, "codex-auto-review"):
        raise ValueError("Unapproved incoming model route")
    if body.get("previous_response_id"):
        raise ValueError("Full conversation replay required for complete access receipts")
    if body.get("stream") is not True:
        raise ValueError("Native Responses transport requires streaming")
    request = copy.deepcopy(body)
    request["model"] = model
    return request, {
        "purpose": "main_agent" if incoming == model else "approval_surrogate",
        "incoming_model": incoming, "upstream_model": model,
        "transport": "responses_passthrough", "protocol_conversion": False,
    }


class StreamMetadata:
    """Observe bounded SSE lines; retain metadata, never reasoning or answer text."""
    def __init__(self, event: dict):
        self.event = event
        self.buffer = b""
        self.completed = False

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.line(line.rstrip(b"\r"))
        if len(self.buffer) > 4 * 1024 * 1024:
            raise ValueError("Responses SSE line exceeded metadata observer limit")

    def line(self, line: bytes) -> None:
        if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
            return
        payload = json.loads(line[5:])
        kind = payload.get("type", "")
        self.event["stream_events"] = self.event.get("stream_events", 0) + 1
        response = payload.get("response") or {}
        if response.get("model"):
            self.event["returned_model"] = response["model"]
        if response.get("usage") is not None:
            self.event["usage"] = response["usage"]
        if kind in ("response.completed", "response.incomplete", "response.failed", "error"):
            self.event["terminal_event"] = kind
            self.event["response_status"] = response.get("status")
            self.completed = kind == "response.completed" and response.get("status") == "completed"

    def finish(self) -> None:
        if self.buffer.strip():
            self.line(self.buffer.rstrip(b"\r"))
        if not self.completed:
            raise ValueError("Responses stream ended without a completed response")


def forward_responses(endpoint, key, body, event, deadline, on_open, on_chunk):
    """Send one request; leave retry decisions to Codex, preserving wire bytes."""
    wire = json.dumps(body, ensure_ascii=False).encode("utf-8")
    event.update(transport="responses_passthrough", protocol_conversion=False,
                 request_bytes=len(wire), request_sha256=hashlib.sha256(wire).hexdigest())
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise TimeoutError("Case wall deadline")
    request = urllib.request.Request(endpoint, data=wire, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })
    observer = StreamMetadata(event)
    digest = hashlib.sha256()
    event["response_bytes"] = 0
    with urllib.request.urlopen(request, timeout=min(600, remaining)) as response:
        event["http_status"] = response.status
        content_type = response.headers.get("Content-Type", "")
        if "text/event-stream" not in content_type.lower():
            raise ValueError("Expected native Responses SSE, received " + content_type)
        on_open(response.status, content_type)
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("Case wall deadline while forwarding Responses")
            chunk = response.read1(65536)
            if not chunk:
                break
            digest.update(chunk)
            event["response_bytes"] += len(chunk)
            observer.feed(chunk)
            on_chunk(chunk)
    event["response_sha256"] = digest.hexdigest()
    observer.finish()
    event["delivered_to_cli"] = True
