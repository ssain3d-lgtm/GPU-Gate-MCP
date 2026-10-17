"""The tiny HTTP helper every adapter uses: one JSON request, never an exception.

Standard library only. Requests bypass any system proxy (these are all localhost servers), carry a
Bearer token when given one, and never carry an Origin header.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _parse(raw: bytes):
    """The JSON in raw, or None when it is empty or not JSON."""
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:  # includes UnicodeDecodeError
        return None


def http_json(
    url: str,
    method: str = "GET",
    body: object | None = None,
    timeout: float = 5.0,
    api_key: str | None = None,
) -> tuple[int, object | None]:
    """Send one request; return (HTTP status, parsed JSON or None).

    Status 0 means nothing answered (refused, unreachable, timed out, dropped). HTTP errors return
    their own status and parsed body. A POST without a body sends `{}`: the servers we talk to
    insist on a JSON content type and read the body.
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if method == "POST" or body is not None:
        data = json.dumps({} if body is None else body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, _parse(response.read())
    except urllib.error.HTTPError as err:
        try:
            return err.code, _parse(err.read())
        except (OSError, http.client.HTTPException):
            return err.code, None
        finally:
            err.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0, None
