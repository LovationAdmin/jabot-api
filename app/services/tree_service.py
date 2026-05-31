"""
Tree layout service.

compute_tree_layout():
- Generation 0 = oldest known ancestors (top of screen)
- Each generation spaced 300px vertically
- Within a generation siblings ordered by birth_date (eldest leftmost)
- Spouses placed next to each other
- Horizontal centering per generation
- Returns list of {person_id, x, y, generation}

merge_persons():
- Detects duplicate persons, merges their data
- Transfers all relationships from source to target
- Soft-deletes source
"""

import uuid
import logging
from typing import List, Dict, Optional, Set
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, and_

from app.models.person import Person, CanvasPosition
from app.models.relationship import Relationship

logger = logging.getLogger(__name__)

GENERATION_HEIGHT = 300   # px between generations
NODE_WIDTH = 220           # px per node
NODE_SPACING = 40          # horizontal gap between nodes


async def compute_tree_layout(
    db: AsyncSession,
    persons: List[Person],
    relationships: List[Relationship],
) -> List[Dict]:
    """
    Assign x,y positions to each person for React Flow rendering.

    Returns list of dicts: {person_id, x, y, generation}
    """
    if not persons:
        return []

    person_map: Dict[uuid.UUID, Person] = {p.id: p for p in persons}
    pid_set: Set[uuid.UUID] = set(person_map.keys())

    # Build adjacency: parent→children, child→parents
    parents_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)
    children_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)
    spouses_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)
    siblings_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)

    for r in relationships:
        a, b = r.person_a_id, r.person_b_id
        if a not in pid_set or b not in pid_set:
            continue
        if r.type == "parent":
            # person_a is parent of person_b
            parents_of[b].add(a)
            children_of[a].add(b)
        elif r.type == "child":
            # person_a is child of person_b
            parents_of[a].add(b)
            children_of[b].add(a)
        elif r.type == "spouse":
            spouses_of[a].add(b)
            spouses_of[b].add(a)
        elif r.type == "sibling":
            siblings_of[a].add(b)
            siblings_of[b].add(a)

    # Assign generations using BFS from roots (persons with no parents in the set)
    # Strategy:
    #   Pass 1 — BFS strictly through parent→child edges. Seed only with persons
    #             that have no parents AND are not reachable as spouses of placed nodes.
    #   Pass 2 — assign spouses the same generation as their placed partner (override
    #             any preliminary generation they were given).
    #   Pass 3 — place remaining disconnected persons after the deepest known gen.
    generations: Dict[uuid.UUID, int] = {}

    # Persons with no parents in this subgraph
    no_parents = {p.id for p in persons if len(parents_of[p.id] & pid_set) == 0}

    # Among those, prefer persons who have children (true structural ancestors).
    # Persons with no parents AND no children are likely "external spouses" — defer them.
    true_roots = [pid for pid in no_parents if children_of[pid] & pid_set]
    if not true_roots:
        # Fall back: use all no-parent nodes (but pass 2 will fix spouses)
        true_roots = list(no_parents)
    if not true_roots:
        sorted_persons = sorted(
            persons,
            key=lambda p: p.birth_date.toordinal() if p.birth_date else 999999
        )
        true_roots = [sorted_persons[0].id]

    # Pass 1: BFS through parent→child only
    queue = list(true_roots)
    for pid in queue:
        if pid not in generations:
            generations[pid] = 0

    head = 0
    while head < len(queue):
        pid = queue[head]
        head += 1
        gen = generations[pid]
        for child_id in children_of[pid]:
            if child_id not in generations:
                generations[child_id] = gen + 1
                queue.append(child_id)

    # Pass 2: assign spouses the same generation as their placed partner.
    # Override any generation assigned in pass 1 if the partner's generation is higher.
    # Iterate until stable (handles chains of spouses).
    changed = True
    while changed:
        changed = False
        for pid in list(pid_set):
            partner_gens = [
                generations[sp]
                for sp in spouses_of[pid]
                if sp in generations and sp in (children_of.keys() | set(true_roots))
            ]
            if not partner_gens:
                partner_gens = [generations[sp] for sp in spouses_of[pid] if sp in generations]
            if partner_gens:
                best_gen = max(partner_gens)
                if generations.get(pid, -1) != best_gen:
                    generations[pid] = best_gen
                    changed = True

    # Pass 2b: les frères/sœurs partagent la même génération. On propage la
    # génération connue à travers les arêtes "sibling" (utile si un frère n'a
    # pas d'arête parent mais est relié à un autre frère déjà placé).
    changed = True
    while changed:
        changed = False
        for pid in list(pid_set):
            if pid not in generations:
                continue
            for sib in siblings_of[pid]:
                if sib in pid_set and generations.get(sib) != generations[pid]:
                    generations[sib] = generations[pid]
                    changed = True

    # Assign remaining unvisited persons (disconnected nodes)
    max_gen = max(generations.values(), default=0)
    for p in persons:
        if p.id not in generations:
            generations[p.id] = max_gen + 1

    # Group persons by generation
    gen_groups: Dict[int, List[uuid.UUID]] = defaultdict(list)
    for pid, gen in generations.items():
        gen_groups[gen].append(pid)

    # Sort each generation by birth_date then name
    for gen in gen_groups:
        gen_groups[gen].sort(
            key=lambda pid: (
                person_map[pid].birth_date.toordinal() if person_map[pid].birth_date else 999999,
                person_map[pid].first_name or "",
            )
        )
        # Move spouses next to each other within generation
        gen_groups[gen] = _reorder_spouses(gen_groups[gen], spouses_of)

    # Compute x positions: center each generation
    positions: Dict[uuid.UUID, Dict] = {}
    all_generations = sorted(gen_groups.keys())

    for gen in all_generations:
        members = gen_groups[gen]
        n = len(members)
        total_width = n * NODE_WIDTH + (n - 1) * NODE_SPACING
        start_x = -(total_width / 2)
        y = gen * GENERATION_HEIGHT

        for i, pid in enumerate(members):
            x = start_x + i * (NODE_WIDTH + NODE_SPACING)
            positions[pid] = {"person_id": str(pid), "x": x, "y": y, "generation": gen}

    return list(positions.values())


def _reorder_spouses(
    pids: List[uuid.UUID],
    spouses_of: Dict[uuid.UUID, Set[uuid.UUID]],
) -> List[uuid.UUID]:
    """
    Reorder a list of person IDs so that spouses appear adjacent to each other.
    Uses a greedy insertion approach.
    """
    result: List[uuid.UUID] = []
    placed: Set[uuid.UUID] = set()

    for pid in pids:
        if pid in placed:
            continue
        result.append(pid)
        placed.add(pid)
        # Place any spouses immediately after
        for spouse_id in spouses_of.get(pid, set()):
            if spouse_id in pids and spouse_id not in placed:
                result.append(spouse_id)
                placed.add(spouse_id)

    return result


async def merge_persons(
    db: AsyncSession,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> Dict:
    """
    Merge source person into target person:
    1. Copy non-null fields from source → target (target wins on conflicts)
    2. Transfer all relationships: replace source_id with target_id
    3. Transfer all media records
    4. Soft-delete the source person

    Returns a summary dict.
    """
    source_result = await db.execute(
        select(Person).where(Person.id == source_id, Person.deleted_at.is_(None))
    )
    source = source_result.scalar_one_or_none()
    if source is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Personne source {source_id} introuvable")

    target_result = await db.execute(
        select(Person).where(Person.id == target_id, Person.deleted_at.is_(None))
    )
    target = target_result.scalar_one_or_none()
    if target is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Personne cible {target_id} introuvable")

    changes: List[str] = []

    # Merge fields: target wins, but fill in blanks from source
    if not target.last_name and source.last_name:
        target.last_name = source.last_name
        changes.append(f"nom de famille copié: {source.last_name}")

    if not target.gender and source.gender:
        target.gender = source.gender
        changes.append(f"genre copié: {source.gender}")

    if not target.birth_date and source.birth_date:
        target.birth_date = source.birth_date
        changes.append(f"date de naissance copiée: {source.birth_date}")

    if not target.death_date and source.death_date:
        target.death_date = source.death_date
        changes.append(f"date de décès copiée: {source.death_date}")

    if not target.city_of_origin and source.city_of_origin:
        target.city_of_origin = source.city_of_origin
        changes.append(f"ville d'origine copiée: {source.city_of_origin}")

    # Merge nicknames (union of both lists)
    src_nicks = set(source.nicknames or [])
    tgt_nicks = set(target.nicknames or [])
    merged_nicks = tgt_nicks | src_nicks
    if merged_nicks != tgt_nicks:
        target.nicknames = sorted(merged_nicks)
        changes.append(f"surnoms fusionnés: {merged_nicks - tgt_nicks}")

    target.updated_at = datetime.now(timezone.utc)

    # Transfer relationships
    from app.models.media import Media as MediaModel

    rels_result = await db.execute(
        select(Relationship).where(
            or_(Relationship.person_a_id == source_id, Relationship.person_b_id == source_id)
        )
    )
    source_rels = rels_result.scalars().all()
    transferred_rels = 0
    skipped_rels = 0

    for rel in source_rels:
        new_a = target_id if rel.person_a_id == source_id else rel.person_a_id
        new_b = target_id if rel.person_b_id == source_id else rel.person_b_id

        # Skip self-referential
        if new_a == new_b:
            skipped_rels += 1
            await db.delete(rel)
            continue

        # Check if identical relationship already exists on target
        existing = await db.execute(
            select(Relationship).where(
                Relationship.person_a_id == new_a,
                Relationship.person_b_id == new_b,
                Relationship.type == rel.type,
            )
        )
        if existing.scalar_one_or_none():
            await db.delete(rel)
            skipped_rels += 1
        else:
            rel.person_a_id = new_a
            rel.person_b_id = new_b
            transferred_rels += 1

    # Transfer media
    media_result = await db.execute(
        select(MediaModel).where(MediaModel.person_id == source_id)
    )
    source_media = media_result.scalars().all()
    transferred_media = 0
    for m in source_media:
        m.person_id = target_id
        transferred_media += 1

    # Transfer canvas position if target has none
    from app.models.person import CanvasPosition
    tgt_pos_result = await db.execute(
        select(CanvasPosition).where(CanvasPosition.person_id == target_id)
    )
    if not tgt_pos_result.scalar_one_or_none():
        src_pos_result = await db.execute(
            select(CanvasPosition).where(CanvasPosition.person_id == source_id)
        )
        src_pos = src_pos_result.scalar_one_or_none()
        if src_pos:
            src_pos.person_id = target_id

    # Soft-delete source
    source.deleted_at = datetime.now(timezone.utc)

    await db.commit()

    return {
        "message": "Fusion réussie",
        "source_id": str(source_id),
        "target_id": str(target_id),
        "changes": changes,
        "relationships_transferred": transferred_rels,
        "relationships_skipped": skipped_rels,
        "media_transferred": transferred_media,
    }
