from __future__ import annotations

import re
import uuid

from fastapi import Request

# Allow UUIDs, ULIDs, short tokens. Reject anything with whitespace, control
# chars or >64 length so a malicious client cannot smuggle headers or inject
# newlines into the structured log stream.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def get_request_id(request: Request) -> str:
    rid = request.headers.get("x-request-id")
    if rid and _REQUEST_ID_RE.match(rid):
        return rid
    return str(uuid.uuid4())
