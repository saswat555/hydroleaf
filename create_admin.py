#!/usr/bin/env python3
import asyncio
from getpass import getpass
import os
import jwt
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, engine, Base
from app.models import User
from app.routers.auth import get_password_hash

# Secret & algorithm must match those used by your app
SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
ALGORITHM = "HS256"


async def ensure_schema():
    """
    Create any missing tables in the database.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def create_admin(email: str, password: str):
    """
    Insert a new superadmin user if one does not already exist.
    """
    async with AsyncSessionLocal() as session:  # type: AsyncSession
        # 1) check for existing user
        result = await session.execute(select(User).where(User.email == email))
        existing = result.scalar_one_or_none()
        if existing:
            print(f"⚠️  A user with email {email!r} already exists (id={existing.id}).")
            return

        # 2) hash & insert
        hashed = get_password_hash(password)
        admin_user = User(
            email=email,
            hashed_password=hashed,
            role="superadmin"
        )
        session.add(admin_user)
        await session.commit()
        await session.refresh(admin_user)

        print(f"✅  Created superadmin {email!r} with id={admin_user.id}")

        # 3) issue a JWT so you can log in immediately
        token_data = {
            "user_id": admin_user.id,
            "role": admin_user.role,
            "exp": datetime.utcnow() + timedelta(hours=1)
        }
        token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
        print(f"🔑  Your admin JWT (valid 1h):\n\n{token}\n")


async def main():
    # 0) ensure tables exist
    await ensure_schema()

    # 1) prompt
    email = input("Enter admin email: ").strip()
    while not email:
        email = input("Email cannot be blank. Enter admin email: ").strip()

    password = getpass("Enter admin password: ")
    confirm = getpass("Confirm password: ")
    if password != confirm:
        print("❌  Passwords do not match.")
        return

    # 2) create
    await create_admin(email, password)


if __name__ == "__main__":
    asyncio.run(main())
