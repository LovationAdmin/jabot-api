import uuid
import asyncio
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
from app.services.ws_manager import manager as ws_manager
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
    # Calcul de layout = pur CPU lourd : on l'exécute dans un thread pour ne pas
    # bloquer l'event loop (sinon le health check Render expire → redémarrage).
    layout = await asyncio.to_thread(compute_tree_layout, persons, relationships)
    layout_map = {item["person_id"]: item for item in layout}

    # Confidentialité : un visiteur ANONYME ne reçoit que le prénom + le nom et
    # la structure de l'arbre. Toutes les autres données (dates, genre, photos)
    # sont réservées aux utilisateurs authentifiés. Ce masquage est fait CÔTÉ
    # SERVEUR — le payload anonyme ne contient tout simplement pas ces champs,
    # contrairement à un masquage purement visuel qui resterait scrapable.
    is_auth = current_user is not None

    nodes = []
    for p in persons:
        pos = layout_map.get(str(p.id), {"x": 0, "y": 0, "generation": 0})
        data = {
            "id": str(p.id),
            "first_name": p.first_name,
            "last_name": p.last_name,
            "generation": pos["generation"],
        }
        if is_auth:
            data.update({
                "gender": p.gender,
                "birth_date": p.birth_date.isoformat() if p.birth_date else None,
                "death_date": p.death_date.isoformat() if p.death_date else None,
                "media": [
                    {"id": str(m.id), "type": m.type, "url": m.url}
                    for m in (p.media or [])
                    if m.type == "photo"
                ][:1],
            })
        nodes.append({
            "id": str(p.id),
            "type": "personNode",
            "position": {"x": pos["x"], "y": pos["y"]},
            "data": data,
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
    current_user: Optional[User] = Depends(get_current_user_optional),
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

    layout = await asyncio.to_thread(compute_tree_layout, persons, all_rels)
    layout_map = {item["person_id"]: item for item in layout}

    is_auth = current_user is not None
    nodes = []
    for p in persons:
        pos = layout_map.get(str(p.id), {"x": 0, "y": 0, "generation": 0})
        data = {
            "id": str(p.id),
            "first_name": p.first_name,
            "last_name": p.last_name,
            "is_center": p.id == person_id,
            "generation": pos["generation"],
        }
        if is_auth:
            data.update({
                "gender": p.gender,
                "birth_date": p.birth_date.isoformat() if p.birth_date else None,
            })
        nodes.append({
            "id": str(p.id),
            "type": "personNode",
            "position": {"x": pos["x"], "y": pos["y"]},
            "data": data,
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


async def _safe_add_rel(db: AsyncSession, a_id: uuid.UUID, b_id: uuid.UUID, rel_type: str) -> None:
    """Crée une relation si elle n'existe pas déjà et que A ≠ B."""
    if a_id == b_id:
        return
    existing = await db.execute(
        select(Relationship).where(
            Relationship.person_a_id == a_id,
            Relationship.person_b_id == b_id,
            Relationship.type == rel_type,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(Relationship(id=uuid.uuid4(), person_a_id=a_id, person_b_id=b_id, type=rel_type))


async def _deduce_from_new_parent_link(
    db: AsyncSession, parent_id: uuid.UUID, child_id: uuid.UUID
) -> None:
    """
    Quand parent_id → child_id (type parent) vient d'être créé :
    a) Fratrie : child_id devient frère/sœur de tous les autres enfants
       de parent_id (liens sibling dans les deux sens).
    b) Grands-parents : les parents de parent_id deviennent grands-parents
       de child_id (lien grandparent).
    """
    # a) Autres enfants de ce parent → fratrie avec child_id
    siblings_result = await db.execute(
        select(Relationship).where(
            Relationship.person_a_id == parent_id,
            Relationship.person_b_id != child_id,
            Relationship.type == "parent",
        )
    )
    for sib_rel in siblings_result.scalars().all():
        sib_id = sib_rel.person_b_id
        await _safe_add_rel(db, child_id, sib_id, "sibling")
        await _safe_add_rel(db, sib_id, child_id, "sibling")

    # b) Parents du parent → grands-parents de child_id
    grandparents_result = await db.execute(
        select(Relationship).where(
            Relationship.person_b_id == parent_id,
            Relationship.type == "parent",
        )
    )
    for gp_rel in grandparents_result.scalars().all():
        gp_id = gp_rel.person_a_id
        await _safe_add_rel(db, gp_id, child_id, "grandparent")

    await db.commit()


async def _propagate_parents_to_sibling(
    db: AsyncSession, source_id: uuid.UUID, target_id: uuid.UUID
) -> None:
    """Copie les liens parent→source vers parent→target (sans doublon)."""
    parents_result = await db.execute(
        select(Relationship).where(
            Relationship.person_b_id == source_id,
            Relationship.type == "parent",
        )
    )
    for pr in parents_result.scalars().all():
        parent_id = pr.person_a_id
        if parent_id == target_id:
            continue
        already = await db.execute(
            select(Relationship).where(
                Relationship.person_a_id == parent_id,
                Relationship.person_b_id == target_id,
                Relationship.type == "parent",
            )
        )
        if already.scalar_one_or_none() is None:
            db.add(Relationship(id=uuid.uuid4(), person_a_id=parent_id, person_b_id=target_id, type="parent"))
    await db.commit()


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

    # ── Déduction automatique des liens de parenté ──────────────────
    # 1. Fratrie → propager les parents de A vers B et vice-versa.
    if rel.type == "sibling":
        await _propagate_parents_to_sibling(db, rel.person_a_id, rel.person_b_id)
        await _propagate_parents_to_sibling(db, rel.person_b_id, rel.person_a_id)

    # 2. Nouveau parent → créer automatiquement:
    #    a) lien "sibling" entre le nouvel enfant (B) et tous les autres
    #       enfants du parent (A) → fratrie déduite.
    #    b) lien "grandparent" entre les parents du parent (A) et l'enfant (B).
    if rel.type == "parent":
        await _deduce_from_new_parent_link(db, parent_id=rel.person_a_id, child_id=rel.person_b_id)

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
    await ws_manager.broadcast("relationship.created", {"relationship_id": str(rel.id)}, str(current_user.id))
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
    await ws_manager.broadcast("relationship.deleted", {"relationship_id": str(relationship_id)}, str(current_user.id))


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
    Retourne un score 0-1 indiquant la probabilité que deux personnes soient
    le même individu.

    Règles d'élimination strictes (retour immédiat à 0) :
    - Les deux ont un nom de famille et ils diffèrent → impossible
    - Les deux ont une date de naissance et les années diffèrent → impossible

    Scoring positif :
    - Prénom identique (normalisé) = base 0.70
    - Prénom similaire (≥ 0.80 SequenceMatcher) = base proportionnelle
    - Même année de naissance → +0.20
    - Date exacte identique → +0.10 supplémentaire
    - Même genre → +0.05
    """
    # Éliminations strictes
    ln_a = _normalize(a.last_name or "")
    ln_b = _normalize(b.last_name or "")
    if ln_a and ln_b and ln_a != ln_b:
        return 0.0

    if a.birth_date and b.birth_date and a.birth_date.year != b.birth_date.year:
        return 0.0

    # Comparer uniquement les prénoms
    fn_a = _normalize(a.first_name or "")
    fn_b = _normalize(b.first_name or "")
    if not fn_a or not fn_b:
        return 0.0

    fn_sim = SequenceMatcher(None, fn_a, fn_b).ratio()
    if fn_sim < 0.80:
        return 0.0

    score = 0.50 + fn_sim * 0.30  # 0.74 → 0.80 selon similarité

    if a.birth_date and b.birth_date:
        if a.birth_date.year == b.birth_date.year:
            score = min(1.0, score + 0.20)
        if a.birth_date == b.birth_date:
            score = min(1.0, score + 0.10)

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

    # Comparaison O(n²) pure CPU : exécutée hors event loop (thread) pour ne pas
    # bloquer le worker unique (et donc le health check Render) sur un gros arbre.
    def _compute_pairs() -> list:
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
        return pairs

    pairs = await asyncio.to_thread(_compute_pairs)
    return {"duplicates": pairs, "total": len(pairs)}


@router.post("/auto-merge-duplicates")
async def auto_merge_duplicates(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Fusionne automatiquement toutes les paires de doublons avec un score ≥ 0.85.
    La personne avec le moins d'informations est absorbée par l'autre.
    Retourne un résumé des fusions effectuées.
    """
    persons_result = await db.execute(
        select(Person).where(Person.deleted_at.is_(None))
    )
    persons = persons_result.scalars().all()

    merged = []
    already_merged: set[str] = set()

    for i in range(len(persons)):
        for j in range(i + 1, len(persons)):
            a, b = persons[i], persons[j]
            if str(a.id) in already_merged or str(b.id) in already_merged:
                continue
            score = _duplicate_score(a, b)
            if score < 0.85:
                continue

            # Keep the person with more filled fields (target)
            def filled(p: Person) -> int:
                return sum(1 for v in [p.last_name, p.birth_date, p.death_date, p.gender, p.city_of_origin] if v)

            source, target = (a, b) if filled(a) <= filled(b) else (b, a)

            try:
                await merge_persons(db, source.id, target.id)
                await write_audit(
                    db,
                    actor_user_id=current_user.id,
                    action="merge_persons",
                    entity_type="person",
                    entity_id=str(target.id),
                    details={
                        "source_id": str(source.id),
                        "source_name": await _person_name(db, source.id),
                        "target_id": str(target.id),
                        "target_name": await _person_name(db, target.id),
                        "auto": True,
                        "score": score,
                    },
                )
                already_merged.add(str(source.id))
                merged.append({
                    "source_id": str(source.id),
                    "target_id": str(target.id),
                    "score": round(score, 2),
                })
            except Exception as exc:
                logger.warning(f"auto-merge failed for {source.id} → {target.id}: {exc}")

    return {"merged": merged, "count": len(merged)}
