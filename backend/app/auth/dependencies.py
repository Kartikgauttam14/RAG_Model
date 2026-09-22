import uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tokens import decode_access_token
from app.config import Settings, get_settings
from app.database import get_db
from app.database.models import Role, User

bearer = HTTPBearer(auto_error=False)
PUBLIC_USER_EMAIL = "public@mansam.local"
# This value is never used for authentication; the synthetic public account has no password.
PUBLIC_PASSWORD_HASH = "authentication-disabled"  # noqa: S105


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    role: Role
    tenant_id: str


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    if not settings.authentication_enabled:
        # Public mode deliberately gives every request one shared principal.  Persist it
        # so conversations, feedback, retrieval events, and uploaded documents keep
        # valid user foreign keys even on a freshly provisioned database.
        user = await db.scalar(select(User).where(User.email == PUBLIC_USER_EMAIL))
        if user is None:
            user = User(
                email=PUBLIC_USER_EMAIL,
                password_hash=PUBLIC_PASSWORD_HASH,
                role=Role.admin,
                tenant_id="default",
            )
            db.add(user)
            await db.flush()
        return Principal(user.id, Role.admin, user.tenant_id)
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        payload = decode_access_token(credentials.credentials, settings)
        return Principal(
            user_id=uuid.UUID(payload["sub"]),
            role=Role(payload["role"]),
            tenant_id=str(payload["tenant_id"]),
        )
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc


def require_roles(*roles: Role):
    async def dependency(
        principal: Principal = Depends(get_current_principal), settings: Settings = Depends(get_settings)
    ) -> Principal:
        if not settings.authentication_enabled:
            return principal
        if principal.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return principal

    return dependency
