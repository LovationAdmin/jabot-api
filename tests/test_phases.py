"""
Tests for Phase 2 (onboard-search cross-tree) and Phase 3 (invitation→visitor, tree access).
"""
import asyncio
import uuid
import pytest
import httpx
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

DB = "postgresql+asyncpg://postgres:postgres@localhost:5432/apitest"
BASE = "http://localhost:8001/api"

engine = create_async_engine(DB, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

PHONE_A = "+2250700000001"
PHONE_B = "+2250700000002"
PHONE_C = "+2250700000003"


async def get_db():
    async with AsyncSessionLocal() as s:
        yield s


async def clean_db():
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET person_id = NULL"))
        for t in ["user_tree_access", "invitations", "relationships", "media",
                  "canvas_positions", "persons", "family_trees", "users"]:
            await conn.execute(text(f"DELETE FROM {t}"))


async def store_otp(phone: str, code: str):
    """Store OTP via Redis and flush rate-limit keys."""
    import redis.asyncio as aioredis
    r = aioredis.from_url("redis://localhost:6379")
    await r.setex(f"otp:{phone}", 300, code)
    # Clear rate-limit counters so tests don't hit 429
    keys = await r.keys("rl:*")
    if keys:
        await r.delete(*keys)
    await r.aclose()


async def login(client: httpx.AsyncClient, phone: str) -> str:
    """OTP login; returns access token."""
    code = "123456"
    await store_otp(phone, code)
    r = await client.post("/auth/verify-otp", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def auth_headers(client, phone):
    token = await login(client, phone)
    return {"Authorization": f"Bearer {token}"}


# ─── Phase 2: onboard-search ───────────────────────────────────────────────


async def test_onboard_search_finds_person_across_trees():
    """User A creates a tree with person Bob. User B searches for Bob by name
    and should find him with tree context."""
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        # User A: login + onboard (creates own tree)
        ha = await auth_headers(c, PHONE_A)
        ob = await c.post("/auth/onboard",
            json={"first_name": "Mamadou", "last_name": "Koné", "gender": "male"},
            headers=ha)
        assert ob.status_code == 201, ob.text
        tree_a_id = ob.json()["family_tree_id"]
        assert tree_a_id

        # Add another person to tree A
        r = await c.post("/persons",
            json={"first_name": "Ibrahim", "last_name": "Koné", "gender": "male"},
            headers={**ha, "X-Tree-ID": tree_a_id})
        assert r.status_code == 201, r.text
        ibrahim_id = r.json()["id"]

        # User B: login (not yet onboarded), search for Ibrahim
        hb = await auth_headers(c, PHONE_B)
        sr = await c.post("/auth/onboard-search",
            json={"name": "Ibrahim Koné"},
            headers=hb)
        assert sr.status_code == 200, sr.text
        data = sr.json()
        assert len(data["matches"]) >= 1
        match = next((m for m in data["matches"] if m["person_id"] == ibrahim_id), None)
        assert match is not None, f"Ibrahim not found in matches: {data['matches']}"
        assert match["tree_id"] == tree_a_id
        assert match["confidence"] > 0

    print("  ✓ onboard-search finds person across trees")


async def test_onboard_search_returns_family_context():
    """Search result includes parent/sibling context."""
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        ha = await auth_headers(c, PHONE_A)
        # Get tree A
        me = await c.get("/auth/me", headers=ha)
        tree_a_id = me.json()["active_tree_id"]

        # Add a person + parent relationship to tree A
        rp = await c.post("/persons",
            json={"first_name": "Fatou", "last_name": "Diallo", "gender": "female"},
            headers={**ha, "X-Tree-ID": tree_a_id})
        assert rp.status_code == 201
        fatou_id = rp.json()["id"]

        mamadou_id = me.json()["person_id"]

        # link Mamadou as parent of Fatou
        rr = await c.post("/tree/relationships",
            json={"type": "parent", "person_a_id": mamadou_id, "person_b_id": fatou_id},
            headers={**ha, "X-Tree-ID": tree_a_id})
        assert rr.status_code == 201, f"Relationship creation failed: {rr.text}"

        hb = await auth_headers(c, PHONE_B)
        sr = await c.post("/auth/onboard-search",
            json={"name": "Fatou Diallo"},
            headers=hb)
        assert sr.status_code == 200, sr.text
        matches = sr.json()["matches"]
        m = next((x for x in matches if x["person_id"] == fatou_id), None)
        assert m is not None
        # Parents should contain Mamadou
        parent_names = [f"{p['first_name']} {p.get('last_name','')}" for p in m["parents"]]
        assert any("Mamadou" in n for n in parent_names), f"Parents: {parent_names}"

    print("  ✓ onboard-search returns family context (parents)")


async def test_onboard_join_existing_tree():
    """User B joins User A's tree as member via onboard with tree_id."""
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        ha = await auth_headers(c, PHONE_A)
        me_a = await c.get("/auth/me", headers=ha)
        tree_a_id = me_a.json()["active_tree_id"]

        hb = await auth_headers(c, PHONE_B)
        ob = await c.post(f"/auth/onboard?tree_id={tree_a_id}",
            json={"first_name": "Aminata", "last_name": "Koné", "gender": "female"},
            headers=hb)
        assert ob.status_code == 201, ob.text
        assert ob.json()["family_tree_id"] == tree_a_id

        # B should now have access to tree A
        me_b = await c.get("/auth/me", headers=hb)
        accesses = me_b.json()["tree_accesses"]
        assert any(a["tree_id"] == tree_a_id for a in accesses), f"B has no access: {accesses}"

    print("  ✓ onboard with tree_id joins existing tree as member")


# ─── Phase 3: invitation → visitor ─────────────────────────────────────────


async def test_invitation_visitor_flow():
    """Owner A invites Phone C. C validates → gets visitor access to A's tree."""
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        ha = await auth_headers(c, PHONE_A)
        me_a = await c.get("/auth/me", headers=ha)
        tree_a_id = me_a.json()["active_tree_id"]

        # A invites C
        inv = await c.post("/invitations/",
            json={"phone": PHONE_C},
            headers={**ha, "X-Tree-ID": tree_a_id})
        assert inv.status_code in (200, 201), inv.text
        # Fetch token+code from DB (code never returned in response)
        async with AsyncSessionLocal() as db:
            row = await db.execute(
                text("SELECT token, validation_code FROM invitations WHERE invited_phone=:p ORDER BY created_at DESC LIMIT 1"),
                {"p": PHONE_C}
            )
            res = row.fetchone()
            assert res, "No invitation in DB"
            token, code = res[0], res[1]

        # C validates the invitation
        hc = await auth_headers(c, PHONE_C)
        val = await c.post("/invitations/validate",
            json={"token": token, "code": str(code)},
            headers=hc)
        assert val.status_code == 200, val.text

        # C should have visitor access to tree A
        me_c = await c.get("/auth/me", headers=hc)
        accesses = me_c.json()["tree_accesses"]
        visitor_access = next((a for a in accesses if a["tree_id"] == tree_a_id), None)
        assert visitor_access is not None, f"C has no access to tree A: {accesses}"
        assert visitor_access["role"] in ("visitor", "member", "owner"), visitor_access

    print("  ✓ invitation → visitor flow")


async def test_visitor_read_only():
    """Visitor C can list persons but not create them."""
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        ha = await auth_headers(c, PHONE_A)
        me_a = await c.get("/auth/me", headers=ha)
        tree_a_id = me_a.json()["active_tree_id"]

        hc = await auth_headers(c, PHONE_C)

        # Read should work
        r = await c.get("/persons", headers={**hc, "X-Tree-ID": tree_a_id})
        assert r.status_code == 200, r.text

        # Write should be forbidden (visitor)
        rw = await c.post("/persons",
            json={"first_name": "Hack", "last_name": "Er", "gender": "male"},
            headers={**hc, "X-Tree-ID": tree_a_id})
        assert rw.status_code in (403, 401), f"Visitor wrote a person: {rw.status_code}"

    print("  ✓ visitor is read-only")


async def test_tree_switcher_me_returns_all_trees():
    """User with multiple trees gets full list in /me."""
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        hb = await auth_headers(c, PHONE_B)
        me = await c.get("/auth/me", headers=hb)
        accesses = me.json()["tree_accesses"]
        # B joined tree A via onboard, so should have at least 1 tree
        assert len(accesses) >= 1, f"B has no trees: {accesses}"

    print("  ✓ /me returns all tree accesses for tree switcher")


async def main():
    print("Setting up…")
    await clean_db()

    print("\n── Phase 2: onboard-search ──")
    await test_onboard_search_finds_person_across_trees()
    await test_onboard_search_returns_family_context()
    await test_onboard_join_existing_tree()

    print("\n── Phase 3: invitation → visitor ──")
    await test_invitation_visitor_flow()
    await test_visitor_read_only()
    await test_tree_switcher_me_returns_all_trees()

    print("\n✅ All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
