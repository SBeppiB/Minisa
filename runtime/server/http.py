"""HTTP/JSON transport for the Minisa application service."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import ipaddress
import uuid
from urllib.parse import urlsplit

from runtime.server.application import MinisaApplication


LOGGER = logging.getLogger("minisa.http")


class MinisaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, application: MinisaApplication):
        self.application = application
        super().__init__(address, MinisaRequestHandler)


class MinisaRequestHandler(BaseHTTPRequestHandler):
    server: MinisaHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/health":
                self._send_json(HTTPStatus.OK, self.server.application.health())
            elif path == "/v1/cognitive-health":
                self._send_json(HTTPStatus.OK, self.server.application.cognitive_health())
            elif path == "/v1/dream-status":
                self._send_json(HTTPStatus.OK, self.server.application.dream_status())
            elif path == "/status":
                self._send_json(HTTPStatus.OK, self.server.application.status())
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found.")
        except Exception:
            LOGGER.exception("Minisa status request failed", extra={"request_id": self._request_id})
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Minisa could not inspect runtime status.",
            )

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        qualification = path == '/__test/chat'
        if path != "/v1/chat" and not qualification:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found.")
            return
        if qualification and not getattr(self.server.application.config, 'qualification_route_enabled', False):
            self._error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found.")
            return
        payload = self._read_json()
        if payload is None:
            return
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid_request",
                "'message' must be a non-empty string.",
            )
            return
        if qualification:
            try:
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    self._error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found.")
                    return
            except ValueError:
                self._error(HTTPStatus.NOT_FOUND, "not_found", "Endpoint not found.")
                return
        try:
            # Provenance comes from the trusted server route, never request JSON.
            response = self.server.application.chat(
                message.strip(), provenance='qualification' if qualification else 'user')
        except Exception:
            LOGGER.exception("Minisa request failed", extra={"request_id": self._request_id})
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Minisa could not process the request.",
            )
            return
        self._send_json(HTTPStatus.OK, response)

    def _read_json(self) -> object | None:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            self._error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json.",
            )
            return None
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", "Invalid Content-Length.")
            return None
        maximum = self.server.application.config.max_request_bytes
        if size <= 0 or size > maximum:
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if size > maximum else HTTPStatus.BAD_REQUEST
            self._error(status, "invalid_request", "Request body size is invalid.")
            return None
        try:
            return json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_json", "Request body is not valid JSON.")
            return None

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self._request_id)
        self.end_headers()
        self.wfile.write(body)

    @property
    def _request_id(self) -> str:
        if not hasattr(self, "_minisa_request_id"):
            self._minisa_request_id = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        return self._minisa_request_id

    def log_message(self, format: str, *args) -> None:
        LOGGER.info(
            "%s %s %s",
            self.command,
            self.path,
            format % args,
            extra={"request_id": self._request_id},
        )
