import asyncio
from database import AsyncSessionLocal, engine, Base
from models import User

async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        db.add_all([
            User(username="alice", token="secret"),
            User(username="bob",   token="bob"),
            User(username="carol", token="carol"),
        ])
        await db.commit()
    print("Users:")
    print("  alice / secret")
    print("  bob   / bob")
    print("  carol / carol")

asyncio.run(main())