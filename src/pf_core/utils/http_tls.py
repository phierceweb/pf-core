"""TLS-verification policy for outbound HTTP.

Shared by the URL-inspection helpers (``urls``, ``url_liveness``) so every
outbound ``httpx`` client verifies certificates by default. Disabling
verification removes MITM protection — and since ``fetch_url_content``'s body
flows to downstream LLMs, a MITM could inject content — so the opt-out exists
only for deliberately probing hosts with known-broken certs.
"""

from __future__ import annotations

from pf_core.utils.env import resolve_bool

_VERIFY_TLS_ENV_VAR = "URL_CHECK_VERIFY_TLS"


def verify_tls() -> bool:
    """Resolve TLS verification for outbound HTTP (URL_CHECK_VERIFY_TLS, default True)."""
    return resolve_bool(None, _VERIFY_TLS_ENV_VAR, default=True)
