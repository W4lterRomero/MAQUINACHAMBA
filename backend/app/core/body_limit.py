"""
Middleware ASGI para limitar estrictamente el tamaño del cuerpo recibido antes
de que el framework procese multipart/form-data o JSON en memoria (Hito 2.1).
Principio: Fail-closed y acotamiento de recursos en el transporte ASGI.
Aplica a:
- Peticiones con cabecera Content-Length excesiva.
- Peticiones con cabecera Content-Length ausente o transferencias chunked/streaming.
"""

import json
from starlette.datastructures import Headers


# 5.5 MB = 5 MB de archivo máximo + 512 KB margen de cabeceras/multipart
MAX_REQUEST_BODY_SIZE = 5 * 1024 * 1024 + 512 * 1024


class PayloadTooLargeException(BaseException):
    """Excepción lanzada cuando el cuerpo recibido en streaming supera el límite permitido."""
    pass



class RequestBodyLimitMiddleware:
    """
    Middleware ASGI que inspecciona y acota el cuerpo de las peticiones HTTP
    antes de que los parseadores multipart o JSON de Starlette/FastAPI lean y
    almacenen datos en memoria o disco.
    """

    def __init__(self, app, max_body_size: int = MAX_REQUEST_BODY_SIZE):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # 1. Comprobación temprana por cabecera Content-Length (antes de recibir bytes)
        headers = Headers(scope=scope)
        content_length_str = headers.get("content-length")
        if content_length_str is not None:
            try:
                content_length = int(content_length_str)
                if content_length > self.max_body_size:
                    return await self._send_error_response(
                        send,
                        status_code=413,
                        detail=(
                            f"El tamaño del cuerpo de la petición excede el tamaño máximo permitido "
                            f"de {self.max_body_size} bytes ({content_length} bytes declarados)."
                        ),
                    )
            except ValueError:
                return await self._send_error_response(
                    send,
                    status_code=400,
                    detail="Cabecera Content-Length inválida.",
                )

        # 2. Envoltura de receive para peticiones chunked, streaming o sin Content-Length
        bytes_received = 0

        async def bounded_receive():
            nonlocal bytes_received
            message = await receive()
            if message["type"] == "http.request":
                body_chunk = message.get("body", b"")
                bytes_received += len(body_chunk)
                if bytes_received > self.max_body_size:
                    raise PayloadTooLargeException(
                        f"El tamaño del cuerpo de la petición excede el tamaño máximo permitido "
                        f"de {self.max_body_size} bytes ({bytes_received} bytes recibidos)."
                    )
            return message

        try:
            return await self.app(scope, bounded_receive, send)
        except PayloadTooLargeException as exc:
            return await self._send_error_response(
                send,
                status_code=413,
                detail=str(exc),
            )

    async def _send_error_response(self, send, status_code: int, detail: str):
        response_body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(response_body)).encode("ascii")),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": response_body,
        })
