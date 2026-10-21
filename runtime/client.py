"""Small dependency-free Python client for the Minisa server."""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class MinisaClientError(RuntimeError):
    pass


class MinisaClient:
    def __init__(self, base_url: str | None = None, timeout: float = 120):
        selected = base_url if base_url is not None else os.environ.get("MINISA_AWAKE_URL", "")
        endpoint = urlsplit(selected)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            raise ValueError("Set an explicit awake base_url or MINISA_AWAKE_URL; "
                             "the Core persistence API is not an awake chat service")
        self.base_url = selected.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict:
        return self._request("GET", "/health")

    def status(self) -> dict:
        return self._request("GET", "/status")

    def chat(self, message: str) -> dict:
        return self._request("POST", "/v1/chat", {"message": message})

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                detail = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = {"error": {"message": str(error)}}
            message = detail.get("error", {}).get("message", str(error))
            raise MinisaClientError(f"Minisa returned HTTP {error.code}: {message}") from error
        except (URLError, TimeoutError) as error:
            raise MinisaClientError(f"Could not reach Minisa: {error}") from error
