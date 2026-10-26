"""Who is calling, taken from the Cognito authorizer's verified claims.

API Gateway has already validated the token's signature and expiry before the
Lambda runs; this module only reads the claims it passes through. Identity is
never taken from the request body, so a reviewer cannot act as someone else.
"""

from __future__ import annotations

from dataclasses import dataclass

REVIEWER_GROUP = "reviewers"


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class Reviewer:
    username: str
    display_name: str


def reviewer_from_event(event: dict) -> Reviewer:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims")
    if not claims:
        raise Forbidden("not signed in")
    if REVIEWER_GROUP not in _groups(claims.get("cognito:groups")):
        raise Forbidden(f"user is not in the '{REVIEWER_GROUP}' group")
    username = claims.get("cognito:username") or claims.get("sub")
    if not username:
        raise Forbidden("token has no username")
    return Reviewer(username=username, display_name=claims.get("email") or username)


def _groups(raw) -> set[str]:
    """REST API authorizers pass list claims as a string ("a,b" or "[a, b]");
    accept either form, or a real list."""
    if not raw:
        return set()
    if isinstance(raw, list):
        return {str(g) for g in raw}
    cleaned = str(raw).strip().strip("[]")
    return {g.strip() for g in cleaned.replace(" ", ",").split(",") if g.strip()}
