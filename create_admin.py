import asyncio
from getpass import getpass
import os
from sqlalchemy import text
from app.core.database import AsyncSessionLocal, init_db
from app.models import User
from app.routers.auth import get_password_hash
import jwt
import datetime

# Use the secret and algorithm from your environment
SECRET_KEY = os.getenv("SECRET_KEY", "your-default-secret")
ALGORITHM = "HS256"

async def create_admin(email: str, password: str):
    async with AsyncSessionLocal() as session:
        # Check if an admin with this email already exists
        result = await session.execute(
            text("SELECT * FROM users WHERE email = :email"),
            {"email": email}
        )
        existing = result.first()
        if existing:
            print("A user with this email already exists.")
            return

        # Hash the password and create the admin user with role "superadmin"
        hashed = get_password_hash(password)
        admin_user = User(email=email, hashed_password=hashed, role="superadmin")
        session.add(admin_user)
        await session.commit()
        await session.refresh(admin_user)
        print(f"Admin created successfully! ID: {admin_user.id}")

        # Generate a JWT token so that the admin can log in immediately
        token_data = {
            "user_id": admin_user.id,
            "role": admin_user.role,
            "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        }
        token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
        print(f"Admin JWT Token: {token}")

def main():
    # Initialize the database
    init_db()
    email = input("Enter admin email: ")
    password = getpass("Enter admin password: ")
    asyncio.run(create_admin(email, password))

if __name__ == "__main__":
    main()
