"""Session validation checks rotation and identity without any device commands."""

from unittest.mock import AsyncMock

import pytest

from custom_components.myq.api.client import Client
from custom_components.myq.api.errors import AuthenticationError
from custom_components.myq.api.validation import validate_session

from .conftest import json_response


async def test_session_validation_rotates_and_persists_before_reading_again(tokens, http):
    save = AsyncMock()
    http.request.side_effect = [
        json_response({"user_id": "user"}),
        json_response({"access_token": "renewed", "refresh_token": "rotated", "expires_in": 3600}),
        json_response({"user_id": "user"}),
        json_response({"accounts": []}),
        json_response(
            {
                "account_access": [
                    {"account_id": "account", "role": "owner", "permissions": {"devices": ["All"]}}
                ]
            }
        ),
    ]
    client = Client(tokens=tokens, transport=http, on_token_update=save)
    report = await validate_session(client, expected_user_id="user", refresh=True)
    assert report == {
        "profile_verified": True,
        "refresh_verified": True,
        "account_count": 1,
        "device_count": 0,
        "device_types": {},
        "account_roles": {"owner": 1},
    }
    save.assert_awaited_once_with(client.auth.tokens)
    assert client.auth.tokens.refresh_token == "rotated"
    assert [call.args[0] for call in http.request.await_args_list] == [
        "GET",
        "POST",
        "GET",
        "GET",
        "GET",
    ]
    assert "rotated" not in str(report) and "renewed" not in str(report)


@pytest.mark.parametrize("when", ["before", "after"])
async def test_changed_identity_rejected(tokens, http, when):
    http.request.side_effect = (
        [json_response({"user_id": "other"})]
        if when == "before"
        else [
            json_response({"user_id": "user"}),
            json_response({"access_token": "a", "refresh_token": "r", "expires_in": 3600}),
            json_response({"user_id": "other"}),
        ]
    )
    client = Client(tokens=tokens, transport=http)
    with pytest.raises(AuthenticationError):
        await validate_session(client, expected_user_id="user", refresh=True)
    assert http.request.await_count == (1 if when == "before" else 3)
