from __future__ import annotations

import uuid

from fastapi import Request


def get_request_id(request: Request) -> str:
    rid = request.headers.get("x-request-id")
    return rid or str(uuid.uuid4())
