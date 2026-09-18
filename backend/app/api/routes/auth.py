import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    Principal,
    create_access_token,
    generate_refresh_token,
    get_current_principal,
    hash_password,
    hash_refresh_token,
    require_roles,
    verify_password,
)
from app.config import Settings, get_settings
from app.database import get_db
from app.database.models import Role, Session, User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=256)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    role: Role = Role.user
    tenant_id: str = Field(default="default", min_length=1, max_length=100)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    role: Role
    tenant_id: str


def _validate_user_creation(principal: Principal, payload: UserCreate) -> None:
    if payload.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="Users may only be created in your tenant")
    if payload.role == Role.admin:
        raise HTTPException(status_code=403, detail="An administrator cannot create another administrator")


@router.post("/token", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = await db.scalar(select(User).where(User.email == payload.email.lower(), User.deleted_at.is_(None)))
    if not user or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    access = create_access_token(user.id, user.role.value, user.tenant_id, settings)
    refresh, refresh_hash = generate_refresh_token()
    db.add(
        Session(
            user_id=user.id,
            refresh_token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        )
    )
    await db.commit()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    session = await db.scalar(
        select(Session).where(
            Session.refresh_token_hash == hash_refresh_token(payload.refresh_token),
            Session.revoked_at.is_(None),
            Session.expires_at > datetime.now(UTC),
        )
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    user = await db.get(User, session.user_id)
    if not user or not user.is_active or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    session.revoked_at = datetime.now(UTC)
    access = create_access_token(user.id, user.role.value, user.tenant_id, settings)
    refresh_token, refresh_hash = generate_refresh_token()
    db.add(
        Session(
            user_id=user.id,
            refresh_token_hash=refresh_hash,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_days),
        )
    )
    await db.commit()
    return TokenResponse(
        access_token=access,
        refresh_token=refresh_token,
        expires_in=settings.access_token_minutes * 60,
    )


@router.get("/me", response_model=UserResponse)
async def me(principal: Principal = Depends(get_current_principal), db: AsyncSession = Depends(get_db)) -> UserResponse:
    user = await db.get(User, principal.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(id=user.id, email=user.email, role=user.role, tenant_id=user.tenant_id)


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(
    payload: UserCreate,
    principal: Principal = Depends(require_roles(Role.admin)),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    _validate_user_creation(principal, payload)
    email = payload.email.lower()
    if await db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status_code=409, detail="User already exists")
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        tenant_id=payload.tenant_id,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserResponse(id=user.id, email=user.email, role=user.role, tenant_id=user.tenant_id)
