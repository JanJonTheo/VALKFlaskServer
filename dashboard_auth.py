"""Short-lived dashboard bearer token validation.

The legacy apikey/apiversion flow remains in app.py for Streamlit and the
Discord bot. This module only owns the additional Next.js dashboard boundary.
"""

import json
import os
import re
from functools import wraps

import jwt
from flask import g, jsonify, request


ALLOWED_ROLES = {"member", "leadership", "admin"}


class DashboardTokenError(ValueError):
    pass


def _tenant_slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")


def tenant_for_claim(tenants, tenant_id):
    requested = _tenant_slug(tenant_id)
    for tenant in tenants:
        candidates = {tenant.get("id"), tenant.get("name"), _tenant_slug(tenant.get("name"))}
        if any(_tenant_slug(candidate) == requested for candidate in candidates if candidate):
            return tenant
    return None


def validate_dashboard_token(token, tenants, secret=None):
    signing_secret = secret or os.getenv("DASHBOARD_JWT_SECRET")
    if not signing_secret:
        raise DashboardTokenError("Dashboard bearer authentication is not configured")
    try:
        claims = jwt.decode(token, signing_secret, algorithms=["HS256"], audience="valk-api", options={"require": ["exp", "sub", "aud", "jti", "tenant_id", "role", "capabilities"]})
    except jwt.PyJWTError as exc:
        raise DashboardTokenError("Invalid or expired dashboard token") from exc
    if claims.get("role") not in ALLOWED_ROLES:
        raise DashboardTokenError("Invalid dashboard role")
    if not isinstance(claims.get("capabilities"), list):
        raise DashboardTokenError("Invalid dashboard capabilities")
    tenant = tenant_for_claim(tenants, claims.get("tenant_id"))
    if not tenant:
        raise DashboardTokenError("Unknown dashboard tenant")
    return tenant, claims


def require_capability(capability, logger):
    """Enforce a capability for bearer sessions; legacy API-key clients stay compatible."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            identity = getattr(g, "dashboard_identity", None)
            if identity and capability not in identity.get("capabilities", []):
                return jsonify({"error": {"code": "FORBIDDEN", "message": "Missing dashboard capability", "correlation_id": request.headers.get("x-correlation-id")}}), 403
            response = view(*args, **kwargs)
            if identity:
                status = response[1] if isinstance(response, tuple) and len(response) > 1 else getattr(response, "status_code", 200)
                logger.info(json.dumps({"event": "dashboard_audit", "tenant": identity.get("tenant_id"), "discord_user": identity.get("sub"), "role": identity.get("role"), "action": request.endpoint, "method": request.method, "status": status, "correlation_id": request.headers.get("x-correlation-id")}, ensure_ascii=False))
            return response
        return wrapped
    return decorator
