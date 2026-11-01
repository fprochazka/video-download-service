"""CSRF protection of the routes the page calls to change state.

The page response sets a random token in an HttpOnly cookie and embeds the same token in the HTML, and the page sends
it back in the X-CSRF-Token header. Another site can make the browser send the cookie, but it cannot read the page to
learn the header value. The cookie is HttpOnly because the script never needs to read it, the token in the page is
enough. SameSite=Strict keeps the cookie off cross-site requests too, and the Sec-Fetch-Site and Origin checks reject
cross-site requests from browsers that send those headers before the token is even compared.

It is a per-route dependency and not middleware: clients that are not this page (the MCP endpoint) authenticate
differently and must not need the token.
"""

import hmac
import re
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import Response

CSRF_COOKIE = 'csrf_token'
CSRF_HEADER = 'X-CSRF-Token'
TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')

ALLOWED_FETCH_SITES = ('same-origin', 'none')


def is_https(request: Request) -> bool:
    """Tailscale serve terminates TLS and proxies plain http, it says so in X-Forwarded-Proto"""
    forwarded_proto = request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower()
    return request.url.scheme == 'https' or forwarded_proto == 'https'


def page_token(request: Request) -> str:
    """The token of the browser's cookie if it has a valid one, so several open tabs keep working, else a new one"""
    token = request.cookies.get(CSRF_COOKIE, '')
    return token if TOKEN_RE.match(token) else secrets.token_urlsafe(32)


def set_token_cookie(request: Request, response: Response, token: str):
    response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite='strict', secure=is_https(request), path='/')


def request_hosts(request: Request) -> set[str]:
    hosts = {request.headers.get('host', '')}
    if forwarded_host := request.headers.get('x-forwarded-host'):
        hosts.add(forwarded_host.split(',')[0].strip())
    return {host.lower() for host in hosts if host}


def forbidden(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


def require_csrf(request: Request):
    """FastAPI dependency for every state-changing route the page calls"""
    fetch_site = request.headers.get('sec-fetch-site')
    if fetch_site is not None and fetch_site not in ALLOWED_FETCH_SITES:
        raise forbidden('Cross-site requests are not allowed.')

    origin = request.headers.get('origin')
    if origin is not None and urlsplit(origin).netloc.lower() not in request_hosts(request):
        raise forbidden('Cross-site requests are not allowed.')

    cookie = request.cookies.get(CSRF_COOKIE, '')
    header = request.headers.get(CSRF_HEADER, '')
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        raise forbidden('The page has expired, reload it and try again.')
