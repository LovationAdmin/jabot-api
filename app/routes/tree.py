import uuid
import logging
import unicodedata
from difflib import SequenceMatcher
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.person import Person, CanvasPosition
from app.models.relationship import Relationship
from app.schemas.relationship import RelationshipCreate, RelationshipResponse
from app.schemas.person import PersonResponse
from app.middleware.auth import get_current_user, get_current_user_optional
from app.models.user import User
from app.services.tree_service import compute_tree_layout, merge_persons
from app.services.audit_service import write_audit


async def _person_name(db: AsyncSession, person_id) -> str:
    result = await db.execute(select(Person).where(Person.id == person_id))
    p = result.scalar_one_or_none()
    if p is None:
        return str(person_id)
    return f"{p.first_name} {p.last_name or ''}".strip()

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_full_tree(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Retourne l'arbre complet: noeuds (personnes) + arêtes (relations) pour React Flow.
    Accessible sans authentification (visiteur anonyme). Si un token valide est fourni,
    l'utilisateur est identifié (current_user non None).
    """
    persons_result = await db.execute(
        select(Person)
        .options(selectinload(Person.media))
        .where(Person.deleted_at.is_(None))
    )
    persons = persons_result.scalars().all()

    rels_result = await db.execute(select(Relationship))
    relationships = rels_result.scalars().all()

    # Compute layout
    layout = await compute_tree_layout(db, persons, relationships)
    layout_map = {item["person_id"]: item for item in layout}

    nodes = []
    for p in persons:
        pos = layout_map.get(str(p.id), {"x": 0, "y": 0, "generation": 0})
        nodes.append({
            "id": str(p.id),
            "type": "personNode",
            "position": {"x": pos["x"], "y": pos["y"]},
            "data": {
                "id": str(p.id),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "gender": p.gender,
                "birth_date": p.birth_date.isoformat() if p.birth_date else None,
                "death_date": p.death_date.isoformat() if p.death_date else None,
                "generation": pos["generation"],
                "media": [
                    {"id": str(m.id), "type": m.type, "url": m.url}
                    for m in (p.media or [])
                    if m.type == "photo"
                ][:1],
            },
        })

    edges = []
    for r in relationships:
        edges.append({
            "id": str(r.id),
            "source": str(r.person_a_id),
            "target": str(r.person_b_id),
            "type": "smoothstep",
            "data": {"relationship_type": r.type},
            "label": r.type,
        })

    return {"nodes": nodes, "edges": edges}


@router.get("/person/{person_id}")
async def get_person_subtree(
    person_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Retourne le sous-arbre centré sur une personne (3 générations: parents, personne, enfants).
    """
    # Check person exists
    p_result = await db.execute(
        select(Person).where(Person.id == person_id, Person.deleted_at.is_(None))
    )
    center = p_result.scalar_one_or_none()
    if center is None:
        raise HTTPException(status_code=404, detail="Personne introuvable")

    # Find all related person IDs within 2 hops
    related_ids = {person_id}
    rels_result = await db.execute(
        select(Relationship).where(
            or_(Relationship.person_a_id == person_id, Relationship.person_b_id == person_id)
        )
    )
    direct_rels = rels_result.scalars().all()
    for r in direct_rels:
        related_ids.add(r.person_a_id)
        related_ids.add(r.person_b_id)

    # Second hop
    second_hop_rels_result = await db.execute(
        select(Relationship).where(
            or_(
                Relationship.person_a_id.in_(related_ids),
                Relationship.person_b_id.in_(related_ids),
            )
        )
    )
    all_rels = second_hop_rels_result.scalars().all()
    for r in all_rels:
        related_ids.add(r.person_a_id)
        related_ids.add(r.person_b_id)

    persons_result = await db.execute(
        select(Person).where(Person.id.in_(related_ids), Person.deleted_at.is_(None))
    )
    persons = persons_result.scalars().all()

    layout = await compute_tree_layout(db, persons, all_rels)
    layout_map = {item["person_id"]: item for item in layout}

    nodes = []
    for p in persons:
        pos = layout_map.get(str(p.id), {"x": 0, "y": 0, "generation": 0})
        nodes.append({
            "id": str(p.id),
            "type": "personNode",
            "position": {"x": pos["x"], "y": pos["y"]},
            "data": {
                "id": str(p.id),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "gender": p.gender,
                "birth_date": p.birth_date.isoformat() if p.birth_date else None,
                "is_center": p.id == person_id,
                "generation": pos["generation"],
            },
        })

    edges = []
    for r in all_rels:
        if r.person_a_id in related_ids and r.person_b_id in related_ids:
            edges.append({
                "id": str(r.id),
                "source": str(r.person_a_id),
                "target": str(r.person_b_id),
                "type": "smoothstep",
                "data": {"relationship_type": r.type},
            })

    return {"nodes": nodes, "edges": edges, "center_id": str(person_id)}


@router.post("/relationships", response_model=RelationshipResponse, status_code=201)
async def add_relationship(
    body: RelationshipCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ajoute une relation entre deux personnes (authentification requise)."""
    if body.person_a_id == body.person_b_id:
        raise HTTPException(status_code=400, detail="Une personne ne peut pas être en relation avec elle-même")

    # Verify both persons exist
    for pid in [body.person_a_id, body.person_b_id]:
        res = await db.execute(
            select(Person).where(Person.id == pid, Person.deleted_at.is_(None))
        )
        if res.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Personne {pid} introuvable")

    # Check duplicate
    existing = await db.execute(
        select(Relationship).where(
            Relationship.person_a_id == body.person_a_id,
            Relationship.person_b_id == body.person_b_id,
            Relationship.type == body.type,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Cette relation existe déjà")

    rel = Relationship(
        id=uuid.uuid4(),
        person_a_id=body.person_a_id,
        person_b_id=body.person_b_id,
        type=body.type,
    )
    db.add(rel)
    await db.commit()
    await db.refresh(rel)
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="create_relationship",
        entity_type="relationship",
        entity_id=str(rel.id),
        details={
            "person_a_id": str(rel.person_a_id),
            "person_b_id": str(rel.person_b_id),
            "person_a_name": await _person_name(db, rel.person_a_id),
            "person_b_name": await _person_name(db, rel.person_b_id),
            "type": rel.type,
        },
    )
    return rel


@router.delete("/relationships/{relationship_id}", status_code=204)
async def delete_relationship(
    relationship_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Supprime une relation (authentification requise)."""
    result = await db.execute(select(Relationship).where(Relationship.id == relationship_id))
    rel = result.scalar_one_or_none()
    if rel is None:
        raise HTTPException(status_code=404, detail="Relation introuvable")
    details = {
        "person_a_id": str(rel.person_a_id),
        "person_b_id": str(rel.person_b_id),
        "person_a_name": await _person_name(db, rel.person_a_id),
        "person_b_name": await _person_name(db, rel.person_b_id),
        "type": rel.type,
    }
    await db.delete(rel)
    await db.commit()
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="delete_relationship",
        entity_type="relationship",
        entity_id=str(relationship_id),
        details=details,
    )


@router.post("/merge")
async def merge_family_branches(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Fusionne deux branches familiales quand des personnes liées sont trouvées.
    Détecte les doublons, fusionne les données, met à jour toutes les relations.
    """
    source_id_str = body.get("source_person_id")
    target_id_str = body.get("target_person_id")

    if not source_id_str or not target_id_str:
        raise HTTPException(status_code=400, detail="source_person_id et target_person_id sont requis")

    try:
        source_id = uuid.UUID(source_id_str)
        target_id = uuid.UUID(target_id_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Identifiants UUID invalides")

    if source_id == target_id:
        raise HTTPException(status_code=400, detail="Impossible de fusionner une personne avec elle-même")

    source_name = await _person_name(db, source_id)
    target_name = await _person_name(db, target_id)
    result = await merge_persons(db, source_id, target_id)
    await write_audit(
        db,
        actor_user_id=current_user.id,
        action="merge_persons",
        entity_type="person",
        entity_id=str(target_id),
        details={"source_id": str(source_id), "source_name": source_name, "target_id": str(target_id), "target_name": target_name},
    )
    return result


def _normalize(s: str) -> str:
    """Lowercase, remove accents and extra spaces."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def _name_similarity(a: Person, b: Person) -> float:
    """Return 0.0-1.0 similarity score between two persons based on name."""
    full_a = _normalize(f"{a.first_name or ''} {a.last_name or ''}")
    full_b = _normalize(f"{b.first_name or ''} {b.last_name or ''}")
    return SequenceMatcher(None, full_a, full_b).ratio()


def _duplicate_score(a: Person, b: Person) -> float:
    """
    Returns a confidence score 0-1 that two persons are duplicates.
    > 0.75 = high confidence, 0.5-0.75 = medium, < 0.5 = ignored.
    """
    name_sim = _name_similarity(a, b)
    if name_sim < 0.5:
        return 0.0

    score = name_sim
    # Bonus: same birth year
    if a.birth_date and b.birth_date:
        if a.birth_date.year == b.birth_date.year:
            score = min(1.0, score + 0.2)
            # Bonus: exact same date
            if a.birth_date == b.birth_date:
                score = min(1.0, score + 0.1)
    # Bonus: same gender
    if a.gender and b.gender and a.gender == b.gender:
        score = min(1.0, score + 0.05)

    return score


@router.get("/duplicates")
async def detect_duplicates(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Détecte les doublons potentiels dans l'arbre.
    Retourne des paires de personnes avec un score de confiance.
    """
    persons_result = await db.execute(
        select(Person).where(Person.deleted_at.is_(None))
    )
    persons = persons_result.scalars().all()

    pairs = []
    for i in range(len(persons)):
        for j in range(i + 1, len(persons)):
            score = _duplicate_score(persons[i], persons[j])
            if score >= 0.6:
                a, b = persons[i], persons[j]
                pairs.append({
                    "person_a": {
                        "id": str(a.id),
                        "first_name": a.first_name,
                        "last_name": a.last_name,
                        "birth_date": a.birth_date.isoformat() if a.birth_date else None,
                        "gender": a.gender,
                    },
                    "person_b": {
                        "id": str(b.id),
                        "first_name": b.first_name,
                        "last_name": b.last_name,
                        "birth_date": b.birth_date.isoformat() if b.birth_date else None,
                        "gender": b.gender,
                    },
                    "score": round(score, 2),
                    "confidence": "high" if score >= 0.85 else "medium",
                })

    pairs.sort(key=lambda x: x["score"], reverse=True)
    return {"duplicates": pairs, "total": len(pairs)}
