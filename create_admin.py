#!/usr/bin/env python3
import asyncio
import os
import jwt
from getpass import getpass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, engine, Base
from app.models import Admin
from app.routers.auth import get_password_hash

# Make sure this matches your app’s SECRET_KEY & algorithm
SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
ALGORITHM  = "HS256"


async def ensure_schema():
    """
    Create any missing tables in the database.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def create_admin(email: str, password: str):
    """
    Insert a new superadmin if one does not already exist.
    """
    async with AsyncSessionLocal() as session:  # type: AsyncSession
        # 1) Check for existing admin
        result   = await session.execute(select(Admin).where(Admin.email == email))
        existing = result.scalar_one_or_none()
        if existing:
            print(f"⚠️  An admin with email {email!r} already exists (id={existing.id}).")
            return

        # 2) Hash & insert
        hashed_pw = get_password_hash(password)
        admin     = Admin(email=email, hashed_password=hashed_pw, role="superadmin")
        session.add(admin)
        await session.commit()
        await session.refresh(admin)

        print(f"✅  Created superadmin {email!r} with id={admin.id}")

        # 3) Issue a JWT so you can authenticate immediately
        token_data = {
            "user_id": admin.id,
            "role":    admin.role,
            "exp":     datetime.utcnow() + timedelta(hours=1)
        }
        token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
        print(f"\n🔑  Your admin JWT (valid 1h):\n\n{token}\n")


async def main():
    # 0) Ensure tables exist
    await ensure_schema()

    # 1) Prompt for email & password
    email = input("Enter admin email: ").strip()
    while not email:
        email = input("Email cannot be blank. Enter admin email: ").strip()

    password = getpass("Enter admin password: ")
    confirm  = getpass("Confirm password: ")
    if password != confirm:
        print("❌  Passwords do not match.")
        return

    # 2) Create the admin
    await create_admin(email, password)


if __name__ == "__main__":
    asyncio.run(main())
