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
NODE_SPACING = 50          # horizontal gap between nodes within a family
CLUSTER_SPACING = 200      # extra horizontal gap between disconnected family trees


async def compute_tree_layout(
    db: AsyncSession,
    persons: List[Person],
    relationships: List[Relationship],
) -> List[Dict]:
    """
    Assign x,y positions to each person for React Flow rendering.

    Strategy: compute layout independently for each connected component, then
    place the components side-by-side (largest first) with CLUSTER_SPACING
    between them. This guarantees no card overlap across disconnected trees.

    Returns list of dicts: {person_id, x, y, generation}
    """
    if not persons:
        return []

    person_map: Dict[uuid.UUID, Person] = {p.id: p for p in persons}
    pid_set: Set[uuid.UUID] = set(person_map.keys())

    # ── Step 0: identify connected components via Union-Find ──────────
    uf: Dict[uuid.UUID, uuid.UUID] = {p.id: p.id for p in persons}

    def find(x: uuid.UUID) -> uuid.UUID:
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a: uuid.UUID, b: uuid.UUID) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            uf[ra] = rb

    for r in relationships:
        if r.person_a_id in pid_set and r.person_b_id in pid_set:
            union(r.person_a_id, r.person_b_id)

    # Group persons by component root, sorted by component size desc
    comp_members: Dict[uuid.UUID, List[Person]] = defaultdict(list)
    for p in persons:
        comp_members[find(p.id)].append(p)
    sorted_comps = sorted(comp_members.values(), key=len, reverse=True)

    # Filter relationships per component
    def rels_for(comp_pids: Set[uuid.UUID]) -> List:
        return [r for r in relationships
                if r.person_a_id in comp_pids and r.person_b_id in comp_pids]

    # ── Step 1: layout each component independently ───────────────────
    all_positions: List[Dict] = []
    current_x_offset = 0.0

    for comp in sorted_comps:
        comp_pids = {p.id for p in comp}
        comp_rels = rels_for(comp_pids)
        comp_pos = _layout_component(comp, comp_rels, person_map)

        if not comp_pos:
            continue

        # Find bounding box of this component
        xs = [pos["x"] for pos in comp_pos]
        x_min = min(xs)
        x_max = max(xs) + NODE_WIDTH

        # Shift component so its left edge starts at current_x_offset
        shift = current_x_offset - x_min
        for pos in comp_pos:
            pos["x"] += shift
        all_positions.extend(comp_pos)

        current_x_offset += (x_max - x_min) + CLUSTER_SPACING

    # Center the whole layout around x=0
    if all_positions:
        all_xs = [pos["x"] for pos in all_positions]
        center_shift = -(min(all_xs) + max(all_xs) + NODE_WIDTH) / 2
        for pos in all_positions:
            pos["x"] += center_shift

    return all_positions


def _layout_component(
    persons: List[Person],
    relationships: List,
    person_map: Dict[uuid.UUID, Person],
) -> List[Dict]:
    """Lay out a single connected component. Returns positions with x relative to 0."""
    if not persons:
        return []

    pid_set: Set[uuid.UUID] = {p.id for p in persons}

    parents_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)
    children_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)
    spouses_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)
    siblings_of: Dict[uuid.UUID, Set[uuid.UUID]] = defaultdict(set)

    for r in relationships:
        a, b = r.person_a_id, r.person_b_id
        if a not in pid_set or b not in pid_set:
            continue
        if r.type == "parent":
            parents_of[b].add(a)
            children_of[a].add(b)
        elif r.type == "child":
            parents_of[a].add(b)
            children_of[b].add(a)
        elif r.type == "spouse":
            spouses_of[a].add(b)
            spouses_of[b].add(a)
        elif r.type == "sibling":
            siblings_of[a].add(b)
            siblings_of[b].add(a)

    generations: Dict[uuid.UUID, int] = {}

    no_parents = {p.id for p in persons if len(parents_of[p.id] & pid_set) == 0}
    true_roots = [pid for pid in no_parents if children_of[pid] & pid_set]
    if not true_roots:
        true_roots = list(no_parents)
    if not true_roots:
        sorted_persons = sorted(
            persons,
            key=lambda p: p.birth_date.toordinal() if p.birth_date else 999999
        )
        true_roots = [sorted_persons[0].id]

    # Pass 1: BFS through parent→child
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

    # Pass 2: spouses share the same generation
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

    # Pass 2b: siblings share the same generation
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

    # Any remaining unvisited persons
    max_gen = max(generations.values(), default=0)
    for p in persons:
        if p.id not in generations:
            generations[p.id] = max_gen + 1

    # Group by generation
    gen_groups: Dict[int, List[uuid.UUID]] = defaultdict(list)
    for pid, gen in generations.items():
        gen_groups[gen].append(pid)

    # Sort within each generation by birth date then name, spouses adjacent
    for gen in gen_groups:
        gen_groups[gen].sort(
            key=lambda pid: (
                person_map[pid].birth_date.toordinal() if person_map[pid].birth_date else 999999,
                person_map[pid].first_name or "",
            )
        )
        gen_groups[gen] = _reorder_spouses(gen_groups[gen], spouses_of)

    # Compute x positions: center each generation within this component
    positions: Dict[uuid.UUID, Dict] = {}
    slot = NODE_WIDTH + NODE_SPACING

    for gen, members in gen_groups.items():
        n = len(members)
        total_width = n * NODE_WIDTH + (n - 1) * NODE_SPACING
        start_x = -(total_width / 2)
        y = gen * GENERATION_HEIGHT
        for i, pid in enumerate(members):
            x = start_x + i * slot
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
