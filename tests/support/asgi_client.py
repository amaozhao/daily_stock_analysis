# -*- coding: utf-8 -*-
"""ASGI test client for environments where anyio thread workers hang."""

from __future__ import annotations

import asyncio
import threading
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import httpx


_THREADPOOL_CONTROL_KWARGS = {"abandon_on_cancel", "cancellable", "limiter"}


def _strip_threadpool_control_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in kwargs.items()
        if key not in _THREADPOOL_CONTROL_KWARGS
    }


async def _run_sync_in_worker(func: Any, *args: Any, **kwargs: Any) -> Any:
    call_kwargs = _strip_threadpool_control_kwargs(kwargs)
    result_box: list[Any] = []
    error_box: list[BaseException] = []

    def worker() -> None:
        try:
            result_box.append(func(*args, **call_kwargs))
        except BaseException as exc:
            error_box.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if error_box:
        raise error_box[0]
    return result_box[0] if result_box else None


class ASGITestClient:
    """Small synchronous wrapper around httpx ASGITransport.

    Starlette's TestClient depends on anyio's blocking portal and worker
    threads. The local stocker Python 3.13 environment can hang in that path,
    so API contract tests use this client and route Starlette/FastAPI
    threadpool calls through short-lived standard threads while preserving
    HTTP request/response semantics.
    """

    def __init__(self, app: Any, *, base_url: str = "http://testserver") -> None:
        self._app = app
        self._base_url = base_url

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(transport=transport, base_url=self._base_url) as client:
                return await client.request(method, url, **kwargs)

        return self._run(send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("HEAD", url, **kwargs)

    def close(self) -> None:
        return None

    def __enter__(self) -> "ASGITestClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    @staticmethod
    def _run(coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            with ExitStack() as stack:
                stack.enter_context(patch("anyio.to_thread.run_sync", new=_run_sync_in_worker))
                for target in (
                    "fastapi.routing.run_in_threadpool",
                    "fastapi.dependencies.utils.run_in_threadpool",
                    "starlette.concurrency.run_in_threadpool",
                    "starlette.datastructures.run_in_threadpool",
                ):
                    stack.enter_context(patch(target, new=_run_sync_in_worker))
                return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()
