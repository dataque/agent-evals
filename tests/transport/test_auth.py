"""Local JWT minting decodes to the expected user claim (no signature path)."""

from __future__ import annotations

import base64
import json

from agent_evals.transport.auth import (
    CallableTokenProvider,
    LocalJwtMinter,
    StaticTokenProvider,
)


def _decode_payload(token: str) -> dict:
    body = token.split(".")[1]
    body += "=" * (-len(body) % 4)  # restore base64 padding
    return json.loads(base64.urlsafe_b64decode(body))


def test_local_jwt_minter_default_claim():
    token = LocalJwtMinter("TEST0001", scopes=["readwrite.api.bff"]).get_token()
    assert token.count(".") == 2          # header.payload.sig
    claims = _decode_payload(token)
    assert claims["sub"] == "TEST0001"  # default claim is the standard JWT subject
    assert "ubs_auth_gpn" not in claims  # deployment-specific claim names live in config, not the code default
    assert claims["scope"] == "readwrite.api.bff"


def test_local_jwt_minter_custom_claim_and_extra():
    token = LocalJwtMinter(
        "U2", user_claim="sub", roles=["recruiter"], extra_claims={"tid": "x"}
    ).get_token()
    claims = _decode_payload(token)
    assert claims["sub"] == "U2"
    assert claims["roles"] == ["recruiter"]
    assert claims["tid"] == "x"


def test_static_and_callable_strip_bearer():
    assert StaticTokenProvider("Bearer abc").get_token() == "abc"
    assert CallableTokenProvider(lambda: "Bearer xyz").get_token() == "xyz"
