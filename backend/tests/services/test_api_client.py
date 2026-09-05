"""Tests for the shared provider API client.

Focus: an empty response body is data, not a failure. Polar answers
``204 No Content`` when a user has nothing new for a data type -- the documented,
routine case -- and the client used to call ``.json()`` on it regardless, raise a
JSONDecodeError, log "API request failed" at error level and wrap it in a 500.

PolarData247 swallowed that 500 again by string-matching "Expecting value" in the
detail, so no data was lost. Two things were wrong anyway: every quiet sync wrote
error lines that bury real failures, and the string match cannot tell "no content"
from "corrupt content" -- a truncated body or a proxy's HTML error page was
silently accepted as "no data".
"""

import json
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.providers import api_client


def _response(status_code: int, content: bytes, payload: Any | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.text = content.decode(errors="replace")
    response.raise_for_status.return_value = None
    if payload is None:
        response.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
    else:
        response.json.return_value = payload
    return response


def _call(response: MagicMock) -> Any:
    client = MagicMock()
    client.__enter__.return_value.request.return_value = response
    with (
        patch.object(api_client, "_get_valid_token", return_value="token"),
        patch.object(api_client.httpx, "Client", return_value=client),
    ):
        return api_client.make_authenticated_request(
            db=MagicMock(),
            user_id=uuid4(),
            connection_repo=MagicMock(),
            oauth=MagicMock(),
            api_base_url="https://api.example",
            provider_name="polar",
            endpoint="/v3/users/sleep",
        )


class TestEmptyResponseBody:
    def test_204_returns_none(self) -> None:
        """Polar's 'nothing new since last time' is a result, not an error."""
        assert _call(_response(204, b"")) is None

    def test_200_with_empty_body_returns_none(self) -> None:
        """Some providers answer 200 with a zero-length body for the same thing."""
        assert _call(_response(200, b"")) is None

    def test_204_logs_no_error(self) -> None:
        """A routine empty response must not write an error line."""
        with patch.object(api_client, "log_structured") as logged:
            _call(_response(204, b""))
        levels = [call.args[1] for call in logged.call_args_list if len(call.args) > 1]
        assert "error" not in levels


class TestMalformedBodyStillFails:
    def test_unparseable_non_empty_body_raises(self) -> None:
        """A proxy's HTML error page is corruption, and must not read as 'no data'.

        This is the case the old string-match workaround swallowed.
        """
        with pytest.raises(HTTPException) as exc:
            _call(_response(200, b"<html>502 Bad Gateway</html>"))
        assert exc.value.status_code == 500


class TestNormalBody:
    def test_json_body_is_returned(self) -> None:
        assert _call(_response(200, b'{"ok": true}', payload={"ok": True})) == {"ok": True}
