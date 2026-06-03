from collections import deque

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.person import Person
from app.models.relationship import Relationship
from app.models.audit import AuditLog

router = APIRouter(prefix="/audit", tags=["audit"])


async def _connected_person_ids(db: AsyncSession, root_person_id, tree_id) -> set[str]:
    """BFS over relationships (both directions) from root person id, scoped to a tree."""
    result = await db.execute(
        select(Relationship.person_a_id, Relationship.person_b_id)
        .where(Relationship.family_tree_id == tree_id)
    )
    adjacency: dict[str, set[str]] = {}
    for a, b in result.all():
        adjacency.setdefault(str(a), set()).add(str(b))
        adjacency.setdefault(str(b), set()).add(str(a))

    root = str(root_person_id)
    visited: set[str] = {root}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        for neighbor in adjacency.get(node, ()):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    return visited


async def _resolve_actor_name(db: AsyncSession, actor_user_id) -> str | None:
    if actor_user_id is None:
        return "Quelqu'un"
    result = await db.execute(select(User).where(User.id == actor_user_id))
    user = result.scalar_one_or_none()
    if not user:
        return "Quelqu'un"
    if user.person_id is not None:
        p_result = await db.execute(
            select(Person).where(Person.id == user.person_id)
        )
        person = p_result.scalar_one_or_none()
        if person:
            return f"{person.first_name} {person.last_name or ''}".strip()
    if user.phone:
        return user.phone
    return "Quelqu'un"


@router.get("/my-tree")
async def my_tree_audit(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.person_id is None:
        return {"entries": []}

    # Arbre de la fiche de l'utilisateur → on scope le journal à cet arbre.
    p_res = await db.execute(
        select(Person.family_tree_id).where(Person.id == current_user.person_id)
    )
    my_tree_id = p_res.scalar_one_or_none()
    if my_tree_id is None:
        return {"entries": []}

    component = await _connected_person_ids(db, current_user.person_id, my_tree_id)

    # Fetch a generous window of recent audit rows, then filter in Python so
    # we can inspect JSON details for relationship/merge entries.
    result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(2000)
    )
    rows = result.scalars().all()

    matched = []
    for row in rows:
        keep = False
        details = row.details or {}
        if row.entity_type == "person":
            if row.entity_id in component:
                keep = True
            elif row.action == "merge_persons":
                if str(details.get("source_id")) in component:
                    keep = True
            elif row.action in ("ignore_duplicate", "unignore_duplicate"):
                if (
                    str(details.get("person_a_id")) in component
                    or str(details.get("person_b_id")) in component
                ):
                    keep = True
        elif row.entity_type == "relationship":
            if (
                str(details.get("person_a_id")) in component
                or str(details.get("person_b_id")) in component
            ):
                keep = True
        if keep:
            matched.append(row)
        if len(matched) >= 200:
            break

    name_cache: dict = {}
    entries = []
    for row in matched:
        if row.actor_user_id in name_cache:
            actor_name = name_cache[row.actor_user_id]
        else:
            actor_name = await _resolve_actor_name(db, row.actor_user_id)
            name_cache[row.actor_user_id] = actor_name
        entries.append(
            {
                "id": row.id,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "actor_name": actor_name,
                "details": row.details,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )

    return {"entries": entries}
