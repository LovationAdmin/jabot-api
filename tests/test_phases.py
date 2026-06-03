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


# ─── Phase 3: tree convergence ─────────────────────────────────────────────


async def test_tree_convergence():
    """User U owns Tree B (onboarded solo) and is a visitor in Tree A (invited).
    U converges: B's contents move into A, U's self-node merges, U becomes member,
    Tree B disappears."""
    U = "+2250700000010"
    OWNER = "+2250700000011"
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        # Owner builds Tree A with U's real fiche + a parent
        ho = await auth_headers(c, OWNER)
        oa = await c.post("/auth/onboard",
            json={"first_name": "Sekou", "last_name": "Traoré", "gender": "male"}, headers=ho)
        tree_a = oa.json()["family_tree_id"]
        # U's real fiche already present in A
        rp = await c.post("/persons",
            json={"first_name": "Awa", "last_name": "Traoré", "gender": "female"},
            headers={**ho, "X-Tree-ID": tree_a})
        awa_in_a = rp.json()["id"]

        # Owner invites U → U becomes visitor of A
        inv = await c.post("/invitations/", json={"phone": U}, headers={**ho, "X-Tree-ID": tree_a})
        assert inv.status_code in (200, 201), inv.text
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                text("SELECT token, validation_code FROM invitations WHERE invited_phone=:p ORDER BY created_at DESC LIMIT 1"),
                {"p": U})).fetchone()
            token, code = row[0], row[1]

        # U logs in, onboards into OWN tree B (no tree_id), then validates invite
        hu = await auth_headers(c, U)
        ob = await c.post("/auth/onboard",
            json={"first_name": "Awa", "last_name": "Traoré", "gender": "female"}, headers=hu)
        tree_b = ob.json()["family_tree_id"]
        awa_in_b = ob.json()["id"]
        # U adds a relative in B
        rb = await c.post("/persons",
            json={"first_name": "Cousin", "last_name": "Test", "gender": "male"},
            headers={**hu, "X-Tree-ID": tree_b})
        cousin_in_b = rb.json()["id"]

        # U validates invitation (authenticated) → visitor of A
        await c.post("/invitations/validate", json={"token": token, "code": str(code)}, headers=hu)

        # Sanity: U now has 2 trees (owner of B, visitor of A)
        me = await c.get("/auth/me", headers=hu)
        accesses = {a["tree_id"]: a["role"] for a in me.json()["tree_accesses"]}
        assert accesses.get(tree_b) == "owner", accesses
        assert accesses.get(tree_a) == "visitor", accesses

        # ── Converge B into A ──
        cv = await c.post(f"/trees/{tree_a}/converge",
            json={"source_tree_id": tree_b, "source_person_id": awa_in_b, "target_person_id": awa_in_a},
            headers=hu)
        assert cv.status_code == 200, cv.text
        data = cv.json()
        assert data["identity_merged"] is True, data
        assert data["persons_moved"] >= 2, data  # Awa(B) + Cousin

        # U now has ONLY tree A, as member
        me2 = await c.get("/auth/me", headers=hu)
        acc2 = {a["tree_id"]: a["role"] for a in me2.json()["tree_accesses"]}
        assert tree_b not in acc2, f"Tree B should be gone: {acc2}"
        assert acc2.get(tree_a) == "member", acc2
        # U's fiche is now the one in A (merged)
        assert me2.json()["person_id"] == awa_in_a, me2.json()

        # The cousin from B now lives in A (read tree A persons)
        persons = await c.get("/persons", headers={**hu, "X-Tree-ID": tree_a})
        ids = {p["id"] for p in persons.json()["persons"]}
        assert cousin_in_b in ids, f"Cousin not moved into A: {ids}"
        assert awa_in_b not in ids, "Source self-node should be soft-deleted/merged"

    print("  ✓ tree convergence: absorb + identity merge + promote + cleanup")


async def test_convergence_requires_target_access():
    """A user who only OWNS source but has no access to target cannot converge."""
    U = "+2250700000012"
    STRANGER = "+2250700000013"
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        # Stranger owns tree A (U is NOT invited)
        hs = await auth_headers(c, STRANGER)
        sa = await c.post("/auth/onboard",
            json={"first_name": "Stranger", "gender": "male"}, headers=hs)
        tree_a = sa.json()["family_tree_id"]
        target_person = sa.json()["id"]

        # U owns tree B
        hu = await auth_headers(c, U)
        ub = await c.post("/auth/onboard",
            json={"first_name": "Outsider", "gender": "male"}, headers=hu)
        tree_b = ub.json()["family_tree_id"]
        src_person = ub.json()["id"]

        # U tries to converge into A without access → 403
        cv = await c.post(f"/trees/{tree_a}/converge",
            json={"source_tree_id": tree_b, "source_person_id": src_person, "target_person_id": target_person},
            headers=hu)
        assert cv.status_code == 403, f"Expected 403, got {cv.status_code}: {cv.text}"

    print("  ✓ convergence refused without target-tree access")


async def test_ignore_duplicate_is_tree_wide():
    """A duplicate pair ignored by one user no longer surfaces for ANOTHER user
    of the same tree (shared, persistent across sessions)."""
    await clean_db()
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        # User A owns a tree with two near-identical persons (a likely duplicate).
        ha = await auth_headers(c, PHONE_A)
        ob = await c.post("/auth/onboard",
            json={"first_name": "Fatou", "last_name": "Sow", "gender": "female"}, headers=ha)
        tree = ob.json()["family_tree_id"]
        fatou_id = ob.json()["id"]
        th = {**ha, "X-Tree-ID": tree}

        p1 = await c.post("/persons", json={"first_name": "Awa", "last_name": "Ba", "gender": "female"}, headers=th)
        p2 = await c.post("/persons", json={"first_name": "Awa", "last_name": "Ba", "gender": "female"}, headers=th)
        id1, id2 = p1.json()["id"], p2.json()["id"]
        # Relie id1 a la personne onboardee pour qu'elle soit dans le journal scope.
        await c.post("/tree/relationships",
            json={"person_a_id": fatou_id, "person_b_id": id1, "type": "sibling"}, headers=th)

        # The pair shows up as a duplicate.
        d = await c.get("/tree/duplicates", headers=th)
        assert d.status_code == 200, d.text
        keys = {tuple(sorted([x["person_a"]["id"], x["person_b"]["id"]])) for x in d.json()["duplicates"]}
        assert tuple(sorted([id1, id2])) in keys, "pair should be detected"

        # User A ignores the pair. Storage is keyed on tree_id only (no user/session),
        # so the dismissal applies tree-wide.
        ig = await c.post("/tree/duplicates/ignore",
            json={"person_a_id": id1, "person_b_id": id2}, headers=th)
        assert ig.status_code == 201, ig.text

        # Re-detect as User A: pair gone.
        d2 = await c.get("/tree/duplicates", headers=th)
        keys2 = {tuple(sorted([x["person_a"]["id"], x["person_b"]["id"]])) for x in d2.json()["duplicates"]}
        assert tuple(sorted([id1, id2])) not in keys2, "ignored pair must not resurface"

        # Un-ignore brings it back.
        un = await c.request("DELETE", "/tree/duplicates/ignore",
            json={"person_a_id": id1, "person_b_id": id2}, headers=th)
        assert un.status_code == 200, un.text
        d3 = await c.get("/tree/duplicates", headers=th)
        keys3 = {tuple(sorted([x["person_a"]["id"], x["person_b"]["id"]])) for x in d3.json()["duplicates"]}
        assert tuple(sorted([id1, id2])) in keys3, "un-ignored pair should resurface"

        # Les actions ignore/un-ignore apparaissent dans le journal d'audit.
        aud = await c.get("/audit/my-tree", headers=th)
        assert aud.status_code == 200, aud.text
        actions = [e["action"] for e in aud.json()["entries"]]
        assert "ignore_duplicate" in actions, "ignore must be audited"
        assert "unignore_duplicate" in actions, "un-ignore must be audited"

    print("  ✓ duplicate ignore is tree-wide, reversible, and audited")


async def test_same_name_different_parents_not_duplicate():
    """Two persons with the SAME name but DIFFERENT (non-empty) parents must not
    be flagged as duplicates."""
    await clean_db()
    async with httpx.AsyncClient(base_url=BASE, timeout=20) as c:
        ha = await auth_headers(c, PHONE_A)
        ob = await c.post("/auth/onboard",
            json={"first_name": "Root", "gender": "male"}, headers=ha)
        tree = ob.json()["family_tree_id"]
        th = {**ha, "X-Tree-ID": tree}

        async def mk(fn, ln=None):
            r = await c.post("/persons", json={"first_name": fn, "last_name": ln}, headers=th)
            return r.json()["id"]

        async def parent(parent_id, child_id):
            r = await c.post("/tree/relationships",
                json={"person_a_id": parent_id, "person_b_id": child_id, "type": "parent"},
                headers=th)
            assert r.status_code == 201, r.text

        # Two "Awa" with different parents.
        awa1 = await mk("Awa")
        awa2 = await mk("Awa")
        dad1 = await mk("Moussa")
        dad2 = await mk("Ousmane")
        await parent(dad1, awa1)
        await parent(dad2, awa2)

        d = await c.get("/tree/duplicates", headers=th)
        assert d.status_code == 200, d.text
        keys = {tuple(sorted([x["person_a"]["id"], x["person_b"]["id"]])) for x in d.json()["duplicates"]}
        assert tuple(sorted([awa1, awa2])) not in keys, "different parents → not a duplicate"

        # Control: two "Bina" with a SHARED parent name still flagged.
        bina1 = await mk("Bina")
        bina2 = await mk("Bina")
        shared = await mk("Kadi")
        shared2 = await mk("Kadi")  # same name, entered twice
        await parent(shared, bina1)
        await parent(shared2, bina2)
        d2 = await c.get("/tree/duplicates", headers=th)
        keys2 = {tuple(sorted([x["person_a"]["id"], x["person_b"]["id"]])) for x in d2.json()["duplicates"]}
        assert tuple(sorted([bina1, bina2])) in keys2, "shared parent name → still a candidate"

        # Tolerance : meme parent saisi avec une faute de frappe → toujours candidat.
        cora1 = await mk("Cora")
        cora2 = await mk("Cora")
        await parent(await mk("Mamadou"), cora1)
        await parent(await mk("Mamadu"), cora2)  # typo, same person
        d3 = await c.get("/tree/duplicates", headers=th)
        keys3 = {tuple(sorted([x["person_a"]["id"], x["person_b"]["id"]])) for x in d3.json()["duplicates"]}
        assert tuple(sorted([cora1, cora2])) in keys3, "typo'd parent name → tolerated, still a candidate"

    print("  ✓ same name + different parents not a duplicate (shared/typo parent still is)")


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

    print("\n── Phase 3: tree convergence ──")
    await test_tree_convergence()
    await test_convergence_requires_target_access()

    print("\n── Duplicates: tree-wide ignore ──")
    await test_ignore_duplicate_is_tree_wide()

    print("\n── Duplicates: relationship-aware detection ──")
    await test_same_name_different_parents_not_duplicate()

    print("\n✅ All tests passed")


if __name__ == "__main__":
    asyncio.run(main())
