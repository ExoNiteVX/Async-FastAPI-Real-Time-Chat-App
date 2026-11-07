"""Database seeding script - creates tables and demo users with password authentication."""
import asyncio
from database import AsyncSessionLocal, engine, Base
from models import User
from auth import hash_password_async, generate_token


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    async with AsyncSessionLocal() as db:
        # Create demo users with passwords
        # Password for all: "DemoPass123!"
        password_hash = await hash_password_async("DemoPass123!")
        
        users = [
            User(
                username="alice",
                    email="alice@example.com",
                password_hash=password_hash,
                token=generate_token(),
            ),
            User(
                username="bob",
                email="bob@example.com",
                password_hash=password_hash,
                token=generate_token(),
            ),
            User(
                username="carol",
                email="carol@example.com",
                password_hash=password_hash,
                token=generate_token(),
            ),
        ]
        db.add_all(users)
        await db.commit()
        
        # Print tokens for demo login
        for user in users:
            print(f"  {user.username} / {user.token}")
    
    print("\nDemo users created!")
    print("Login with email and password: DemoPass123!")
    print("  alice@example.com")
    print("  bob@example.com")
    print("  carol@example.com")


asyncio.run(main())