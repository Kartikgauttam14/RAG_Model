import asyncio

from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.database.models import Role, User
from app.database.session import SessionFactory


async def main() -> None:
    settings = get_settings()
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        raise SystemExit(
            "BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD are required"
        )
    async with SessionFactory() as db:
        existing = await db.scalar(
            select(User).where(User.email == settings.bootstrap_admin_email.lower())
        )
        if existing:
            print("Admin already exists")
            return
        db.add(
            User(
                email=settings.bootstrap_admin_email.lower(),
                password_hash=hash_password(settings.bootstrap_admin_password),
                role=Role.admin,
                tenant_id="default",
            )
        )
        await db.commit()
        print("Admin created")


if __name__ == "__main__":
    asyncio.run(main())
