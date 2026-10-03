"""Request size cap enforced before the body is decoded (room-o-matic/docs#21).

Field limits (message body, note value, typed payload) are checked after parsing; this
keeps a client from making the server buffer and decode an arbitrarily large JSON body
first. Declared Content-Length is rejected up front; chunked bodies are counted as they
stream and cut off at the cap.
"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestSizeLimit:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send: Send) -> None:
        body = json.dumps({"detail": f"request body exceeds {self.max_bytes} bytes"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            return await self._reject(send)
        seen = 0
        exceeded = False

        async def counted() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    # Stop reading; whatever the app makes of the truncated body is
                    # replaced with a 413 below.
                    exceeded = True
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        async def guarded(msg: Message) -> None:
            if not exceeded:
                await send(msg)

        try:
            await self.app(scope, counted, guarded)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            await self._reject(send)
