import os
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer()

CLERK_JWKS_URL    = os.getenv("CLERK_JWKS_URL")
CLERK_ISSUER_URL  = os.getenv("CLERK_ISSUER_URL", "")  # e.g. https://pretty-bird-74.clerk.accounts.dev

_jwks_client: PyJWKClient | None = None


def get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(CLERK_JWKS_URL, cache_keys=True)
    return _jwks_client


async def verify_token(credentials: HTTPAuthorizationCredentials = Security(bearer)) -> dict:
    token = credentials.credentials
    try:
        signing_key = get_jwks_client().get_signing_key_from_jwt(token)
        decode_kwargs: dict = {
            "algorithms": ["RS256"],
            "options": {"verify_aud": False},
        }
        if CLERK_ISSUER_URL:
            decode_kwargs["issuer"] = CLERK_ISSUER_URL
        payload = jwt.decode(token, signing_key.key, **decode_kwargs)
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


def get_user_id(payload: dict = Depends(verify_token)) -> str:
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="No user ID in token")
    return user_id
