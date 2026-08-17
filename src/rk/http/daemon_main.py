"""Standard-library HTTP daemon for the framework-neutral published application."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from rk.http_shell import (
    HttpApplicationProtocol,
    HttpErrorClass,
    HttpRequest,
    HttpResponse,
    HttpStreamResponse,
    ProductHttpError,
    error_response,
)


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Bound request concurrency before allocating another handler thread."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_connections: int,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, handler)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if not self._slots.acquire(blocking=False):
            if isinstance(request, tuple):
                request[1].close()
            else:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class ProductHttpDaemon:
    """Own a real listener whose lifecycle is independent from a browser client."""

    def __init__(
        self,
        *,
        app: HttpApplicationProtocol,
        deployment_id: str,
        host: str,
        port: int,
        max_request_body_bytes: int = 16 * 1024 * 1024,
        request_timeout_seconds: float = 15.0,
        max_connections: int = 128,
    ) -> None:
        if not deployment_id or not host or not 0 <= port <= 65_535:
            raise ValueError("daemon listener configuration is invalid")
        if max_request_body_bytes <= 0 or request_timeout_seconds <= 0 or max_connections <= 0:
            raise ValueError("daemon limits must be positive")
        self._app = app
        self._deployment_id = deployment_id
        self._host = host
        self._port = port
        self._max_request_body_bytes = max_request_body_bytes
        self._request_timeout_seconds = request_timeout_seconds
        self._max_connections = max_connections
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("daemon is not running")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("daemon is already running")
        daemon = self
        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, name="rk-product-async")
        loop_thread.start()
        self._loop = loop
        self._loop_thread = loop_thread

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                self._dispatch()

            def do_POST(self) -> None:
                self._dispatch()

            def do_PUT(self) -> None:
                self._dispatch()

            def do_DELETE(self) -> None:
                self._dispatch()

            def _dispatch(self) -> None:
                self.connection.settimeout(daemon._request_timeout_seconds)
                if self.command == "GET" and urlsplit(self.path).path == "/healthz":
                    daemon._write_response(
                        self,
                        HttpResponse(
                            200,
                            {
                                "schema_version": "rk.product.daemon_health.v1",
                                "deployment_id": daemon._deployment_id,
                                "status": "AVAILABLE",
                            },
                        ),
                    )
                    return
                try:
                    content_length = daemon._content_length(self.headers.get_all("content-length"))
                    body = self.rfile.read(content_length) if content_length else b""
                    if len(body) != content_length:
                        raise ValueError("request body ended before Content-Length")
                except (ValueError, TimeoutError):
                    daemon._write_response(
                        self,
                        error_response(
                            ProductHttpError(
                                code="REQUEST_BODY_INVALID",
                                error_class=HttpErrorClass.SCHEMA,
                                path="$.headers.content-length",
                            )
                        ),
                    )
                    self.close_connection = True
                    return
                request = HttpRequest(
                    self.command,
                    self.path,
                    {key: value for key, value in self.headers.items()},
                    body,
                )
                try:
                    if daemon._loop is None:
                        raise RuntimeError("daemon async runtime is unavailable")
                    result = asyncio.run_coroutine_threadsafe(
                        daemon._app(request), daemon._loop
                    ).result()
                except Exception as error:
                    result = error_response(error)
                daemon._write_response(self, result)

            def log_message(self, format: str, *args: object) -> None:
                return None

        server = _BoundedThreadingHTTPServer(
            (self._host, self._port), Handler, max_connections=self._max_connections
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="rk-product-http")
        thread.start()
        self._server = server
        self._thread = thread

    def _content_length(self, values: list[str] | None) -> int:
        if not values:
            return 0
        if len(set(values)) != 1:
            raise ValueError("conflicting Content-Length headers")
        raw = values[0]
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError("Content-Length must be a non-negative decimal")
        value = int(raw)
        if value > self._max_request_body_bytes:
            raise ValueError("request body exceeds configured limit")
        return value

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None or thread is None:
            raise RuntimeError("daemon is not running")
        server.shutdown()
        server.server_close()
        thread.join()
        loop = self._loop
        loop_thread = self._loop_thread
        if loop is not None and loop_thread is not None:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join()
            loop.close()
        self._server = None
        self._thread = None
        self._loop = None
        self._loop_thread = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    @staticmethod
    def _write_response(
        handler: BaseHTTPRequestHandler,
        response: HttpResponse | HttpStreamResponse,
    ) -> None:
        handler.send_response(response.status)
        headers: dict[str, str] = dict(response.headers)
        if isinstance(response, HttpResponse):
            payload = json.dumps(
                _json_tree(response.body),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["content-length"] = str(len(payload))
        else:
            payload = b""
            headers.pop("content-length", None)
            headers["transfer-encoding"] = "chunked"
        for key, value in headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        if isinstance(response, HttpResponse):
            handler.wfile.write(payload)
            return
        try:
            for chunk in response.body:
                if not chunk:
                    continue
                handler.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                handler.wfile.write(chunk)
                handler.wfile.write(b"\r\n")
                handler.wfile.flush()
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            close = getattr(response.body, "close", None)
            if close is not None:
                close()


__all__ = ["ProductHttpDaemon"]


def _json_tree(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_tree(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_tree(item) for item in value]
    raise TypeError(f"HTTP response contains non-JSON value {type(value).__name__}")
