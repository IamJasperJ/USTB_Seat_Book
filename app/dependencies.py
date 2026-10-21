"""Lazy HTTP dependencies, independent of application initialization order."""
from fastapi import Cookie, Header, Request

from .accounts import Account


async def current_account(
    request: Request,
    session_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> Account:
    from . import main

    return await main.current_account(session_token, authorization, request)
