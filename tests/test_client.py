"""Unit tests for the Kapruka API client error-handling layer."""

import pytest
import httpx

from src.api.client import handle_api_error


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://example.com")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("", request=request, response=response)


def test_handle_404():
    result = handle_api_error(_http_error(404))
    assert "not found" in result.lower()


def test_handle_401():
    result = handle_api_error(_http_error(401))
    assert "KAPRUKA_API_KEY" in result


def test_handle_429():
    result = handle_api_error(_http_error(429))
    assert "rate limit" in result.lower()


def test_handle_timeout():
    result = handle_api_error(httpx.ReadTimeout("timed out"))
    assert "timed out" in result.lower()


def test_handle_connect_error():
    result = handle_api_error(httpx.ConnectError("refused"))
    assert "connect" in result.lower()
