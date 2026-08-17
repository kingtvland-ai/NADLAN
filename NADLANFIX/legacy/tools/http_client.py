"""
PlanWatch - HTTP/TLS transport layer
====================================
Everything that talks to ags.iplan.gov.il goes through here, because that
server needs two non-default things:

1. LEGACY TLS CIPHERS
   The host only offers old RSA/SHA1 cipher suites (it negotiates
   `AES128-SHA`, i.e. TLS_RSA_WITH_AES_128_CBC_SHA). OpenSSL 3.x - which
   ships with Python 3.10+ - refuses those at its default security level
   of 2, so the handshake dies with:

       ssl.SSLError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]

   The fix is to drop the OpenSSL *security level* to 1 for this host only.
   Certificate verification and hostname checking stay fully ON - we are
   only re-allowing an older cipher suite, not disabling trust.

2. A USER-AGENT THE WAF ACCEPTS
   The site sits behind a WAF that answers a bare `User-Agent: Mozilla/5.0`
   with an HTML error page ("שגיאה") and HTTP 200 - so it looks like a
   success but `resp.json()` blows up with "Expecting value: line 1
   column 1". A realistic browser UA (or curl's, or requests' own) passes.
   `assert_json_response()` below turns that failure mode into a clear
   error instead of a confusing JSONDecodeError.
"""

from __future__ import annotations

import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

#: A realistic desktop UA. The WAF rejects short/obviously-fake ones.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = (15, 180)  # (connect, read) - geometry pages are ~3 MB


def build_ssl_context() -> ssl.SSLContext:
    """
    A verifying TLS context that also permits the legacy cipher suites
    ags.iplan.gov.il requires.

    `@SECLEVEL=1` re-enables SHA1-signed / non-forward-secret suites that
    OpenSSL 3 disables by default. `check_hostname` and `verify_mode`
    are left at their secure defaults.
    """
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


class LegacyTlsAdapter(HTTPAdapter):
    """Injects our permissive-cipher (still verifying) SSLContext."""

    def __init__(self, *args, ssl_context: ssl.SSLContext | None = None, **kwargs):
        self._ssl_context = ssl_context or build_ssl_context()
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def build_session(
    total_retries: int = 5,
    backoff_factor: float = 1.5,
    user_agent: str = USER_AGENT,
) -> requests.Session:
    """
    A `requests.Session` that can actually reach the iplan ArcGIS server:
    legacy-TLS-capable, WAF-friendly UA, and automatic retry with
    exponential backoff on the transient failures this service does throw
    (502/503/504 under load, and connection resets mid-page).
    """
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "POST")),
        raise_on_status=False,
    )

    adapter = LegacyTlsAdapter(max_retries=retry, pool_maxsize=8)
    sess = requests.Session()
    sess.mount("https://", adapter)
    sess.mount("http://", HTTPAdapter(max_retries=retry))
    sess.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    return sess


class UpstreamBlockedError(RuntimeError):
    """The WAF answered with an HTML block page instead of JSON."""


def assert_json_response(resp: requests.Response) -> None:
    """
    Raise a *useful* error when the WAF returns its HTML block page with
    HTTP 200, instead of letting `.json()` fail with a cryptic
    JSONDecodeError several frames away.
    """
    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype.lower():
        return
    snippet = resp.text[:200].replace("\n", " ")
    raise UpstreamBlockedError(
        f"Expected JSON from {resp.url.split('?')[0]} but got "
        f"Content-Type={ctype!r} (HTTP {resp.status_code}). "
        f"This is normally the WAF block page - check the User-Agent. "
        f"Body starts: {snippet!r}"
    )
