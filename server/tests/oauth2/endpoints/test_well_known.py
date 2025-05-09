import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_jwks(client: AsyncClient) -> None:
    response = await client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    json = response.json()

    assert len(json["keys"]) > 0
    for key in json["keys"]:
        assert "kid" in key
        assert "d" not in key


@pytest.mark.asyncio
async def test_openid_configuration(client: AsyncClient) -> None:
    response = await client.get("/.well-known/openid-configuration")

    assert response.status_code == 200

    json = response.json()
    assert len(json["revocation_endpoint_auth_methods_supported"]) > 0
    assert len(json["introspection_endpoint_auth_methods_supported"]) > 0
    assert len(json["code_challenge_methods_supported"]) > 0
