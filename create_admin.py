# create_admin.py
import asyncio
from getpass import getpass
from app.core.database import AsyncSessionLocal, init_db
from app.models import User
from app.routers.auth import get_password_hash  # reuse our existing password hashing function

async def create_admin(email: str, password: str):
    async with AsyncSessionLocal() as session:
        # Check if an admin with this email already exists
        result = await session.execute(
            "SELECT * FROM users WHERE email = :email",
            {"email": email}
        )
        existing = result.first()
        if existing:
            print("A user with this email already exists.")
            return

        hashed = get_password_hash(password)
        admin_user = User(email=email, hashed_password=hashed, role="superadmin")
        session.add(admin_user)
        await session.commit()
        await session.refresh(admin_user)
        print(f"Admin created successfully! ID: {admin_user.id}")

def main():
    # Ensure the database is initialized
    asyncio.run(init_db())
    email = input("Enter admin email: ")
    password = getpass("Enter admin password: ")
    asyncio.run(create_admin(email, password))

if __name__ == "__main__":
    main()
