from datetime import datetime, timedelta, timezone

import jwt
import pytest

from dashboard_auth import DashboardTokenError, tenant_for_claim, validate_dashboard_token


TENANTS = [{"name": "VALK Squadron", "api_key": "legacy-secret"}]
SECRET = "test-dashboard-secret"


def make_token(**overrides):
    claims = {"sub": "discord-42", "tenant_id": "valk-squadron", "role": "leadership", "capabilities": ["dashboard:read", "objectives:write"], "aud": "valk-api", "jti": "unique-1", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    claims.update(overrides)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def test_resolves_tenant_by_stable_slug():
    assert tenant_for_claim(TENANTS, "valk-squadron")["name"] == "VALK Squadron"


def test_validates_short_lived_dashboard_token():
    tenant, claims = validate_dashboard_token(make_token(), TENANTS, SECRET)
    assert tenant["name"] == "VALK Squadron"
    assert claims["sub"] == "discord-42"


def test_rejects_unknown_tenant():
    with pytest.raises(DashboardTokenError):
        validate_dashboard_token(make_token(tenant_id="unknown"), TENANTS, SECRET)
