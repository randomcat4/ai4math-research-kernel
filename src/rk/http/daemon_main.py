"""Standard-library HTTP daemon for the framework-neutral published application."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self
from urllib.parse import urlsplit

from rk.http_shell import (
    HttpApplicationProtocol,
    HttpRequest,
    HttpResponse,
    HttpStreamResponse,
    error_response,
)


class ProductHttpDaemon:
    """Own a real listener whose lifecycle is independent from a browser client."""

    def __init__(
        self,
        *,
        app: HttpApplicationProtocol,
        deployment_id: str,
        host: str,
        port: int,
    ) -> None:
        if not deployment_id or not host or not 0 <= port <= 65_535:
            raise ValueError("daemon listener configuration is invalid")
        self._app = app
        self._deployment_id = deployment_id
        self._host = host
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

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
                content_length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(content_length) if content_length else b""
                request = HttpRequest(
                    self.command,
                    self.path,
                    {key: value for key, value in self.headers.items()},
                    body,
                )
                try:
                    result = asyncio.run(daemon._app(request))
                except Exception as error:
                    result = error_response(error)
                daemon._write_response(self, result)

            def log_message(self, format: str, *args: object) -> None:
                return None

        server = ThreadingHTTPServer((self._host, self._port), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="rk-product-http")
        thread.start()
        self._server = server
        self._thread = thread

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None or thread is None:
            raise RuntimeError("daemon is not running")
        server.shutdown()
        server.server_close()
        thread.join()
        self._server = None
        self._thread = None

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
                response.body,
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
