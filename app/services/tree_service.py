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
NODE_SPACING = 80          # horizontal gap between nodes within a family unit
FAMILY_GAP = 130           # extra gap between distinct families on the same row
CLUSTER_SPACING = 420      # gap between fully disconnected family trees


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

    # Pass 3: enforce parent strictly above child after passes 2/2b may have
    # shifted generations. Propagate downward so the whole subtree stays consistent.
    changed = True
    while changed:
        changed = False
        for pid in pid_set:
            p_gen = generations.get(pid)
            if p_gen is None:
                continue
            for child_id in children_of[pid]:
                c_gen = generations.get(child_id)
                if c_gen is not None and c_gen <= p_gen:
                    generations[child_id] = p_gen + 1
                    changed = True

    # Pass 4: generation inference from extended relationship types.
    # Extended types (grandparent, uncle_aunt, etc.) are stored in the DB but
    # ignored by the BFS. Any node reachable ONLY through these types gets
    # placed at max_gen+1 by the fallback — wrong. We propagate from already-
    # placed nodes to unplaced ones using the known generational offsets.
    # Only fills in MISSING generations; never moves already-placed nodes.
    B_OFFSET: Dict[str, int] = {
        "grandparent": 2, "grandchild": -2,
        "uncle_aunt": 1,  "nephew_niece": -1,
        "step_parent": 1, "step_child": -1,
        "cousin": 0,
    }
    changed = True
    while changed:
        changed = False
        for r in relationships:
            a, b = r.person_a_id, r.person_b_id
            if a not in pid_set or b not in pid_set:
                continue
            offset = B_OFFSET.get(r.type)
            if offset is None:
                continue
            a_placed = a in generations
            b_placed = b in generations
            if a_placed and not b_placed:
                generations[b] = generations[a] + offset
                changed = True
            elif b_placed and not a_placed:
                generations[a] = generations[b] - offset
                changed = True

    # Any remaining unvisited persons (truly isolated from all known types)
    max_gen = max(generations.values(), default=0)
    for p in persons:
        if p.id not in generations:
            generations[p.id] = max_gen + 1

    # Group by generation
    gen_groups: Dict[int, List[uuid.UUID]] = defaultdict(list)
    for pid, gen in generations.items():
        gen_groups[gen].append(pid)

    def birth_key(pid: uuid.UUID):
        return (
            person_map[pid].birth_date.toordinal() if person_map[pid].birth_date else 999999,
            person_map[pid].first_name or "",
        )

    # Compute x positions generation by generation, top → bottom.
    # Pour chaque génération (sauf la racine), on ordonne les nœuds selon la
    # position moyenne de leurs parents déjà placés : les enfants se regroupent
    # ainsi sous leur couple parental (gère co-épouses simultanées comme
    # remariages successifs, peu importe l'ordre des mariages). À défaut de
    # parent placé, on retombe sur la date de naissance.
    positions: Dict[uuid.UUID, Dict] = {}
    slot = NODE_WIDTH + NODE_SPACING
    ordered_gens = sorted(gen_groups.keys())

    for idx, gen in enumerate(ordered_gens):
        members = gen_groups[gen]

        if idx == 0:
            members.sort(key=birth_key)
        else:
            def parent_anchor(pid: uuid.UUID):
                px = [
                    positions[pp]["x"]
                    for pp in parents_of[pid]
                    if pp in positions
                ]
                # Sans parent placé : très grande valeur → reste à droite, trié
                # ensuite par naissance.
                return (sum(px) / len(px) if px else float("inf"), *birth_key(pid))
            members.sort(key=parent_anchor)

        members = _reorder_spouses(members, spouses_of)
        gen_groups[gen] = members

        n = len(members)
        total_width = n * NODE_WIDTH + (n - 1) * NODE_SPACING
        start_x = -(total_width / 2)
        y = gen * GENERATION_HEIGHT
        for i, pid in enumerate(members):
            positions[pid] = {"person_id": str(pid), "x": start_x + i * slot, "y": y, "generation": gen}

    # ── Raffinement itératif des coordonnées (Sugiyama / barycentre) ───────
    # Le placement top-down ne fait qu'ORDONNER les nœuds puis les tasse à
    # gauche : les traits parent→enfant sont diagonaux. Un SEUL passage
    # bottom-up ne suffit pas dès qu'il y a 3+ générations — centrer les
    # parents sur leurs enfants décale alors les grands-parents, et rien ne
    # corrige en retour. On alterne donc des balayages montants (s'aligner sur
    # les enfants) et descendants (s'aligner sur les parents), répétés jusqu'à
    # convergence. L'ordre dans chaque génération reste fixe ; seul l'x bouge,
    # _place_row garantissant l'espacement minimal.
    keys_present = set(positions.keys())

    def _desired_from(pid: uuid.UUID, neighbors: Set[uuid.UUID]) -> Optional[float]:
        xs = [positions[n]["x"] for n in neighbors if n in keys_present]
        return sum(xs) / len(xs) if xs else None

    def _sweep(gens_order: List[int], use_children: bool) -> float:
        """Un balayage. Renvoie le déplacement total (pour tester la convergence)."""
        moved = 0.0
        for gen in gens_order:
            members = gen_groups[gen]
            desired: List[float] = []
            for pid in members:
                # Cible primaire : barycentre des enfants (montant) ou des parents (descendant).
                primary = _desired_from(pid, children_of[pid] if use_children else parents_of[pid])
                # Le conjoint tire vers lui pour garder les couples soudés et
                # centrer le nœud FAM ; un nœud sans enfant/parent suit son couple.
                spouse_x = _desired_from(pid, spouses_of[pid])
                # Un nœud « feuille » dans ce sens (ni cible primaire) s'accroche
                # à sa fratrie déjà ancrée, sinon garde sa position.
                if primary is None:
                    sib_x = _desired_from(
                        pid,
                        {s for s in siblings_of[pid]
                         if (children_of[s] if use_children else parents_of[s]) & keys_present},
                    )
                    target = sib_x if sib_x is not None else spouse_x
                    target = target if target is not None else positions[pid]["x"]
                elif spouse_x is not None:
                    # Mélange enfants/parents (70%) et conjoint (30%) → couple aligné.
                    target = 0.7 * primary + 0.3 * spouse_x
                else:
                    target = primary
                desired.append(target)

            # Écart minimal entre voisins : `slot` au sein d'une même famille,
            # élargi de FAMILY_GAP quand deux fiches adjacentes appartiennent à
            # des familles distinctes (ni conjoints, ni parents communs) → les
            # branches respirent sans se coller.
            gaps: List[float] = []
            for i in range(1, len(members)):
                a, b = members[i - 1], members[i]
                same_unit = (
                    b in spouses_of[a]
                    or a in spouses_of[b]
                    or bool(parents_of[a] & parents_of[b])   # fratrie
                    or a in parents_of[b] or b in parents_of[a]
                )
                gaps.append(slot if same_unit else slot + FAMILY_GAP)

            for pid, x in zip(members, _place_row(desired, slot, gaps)):
                moved += abs(positions[pid]["x"] - x)
                positions[pid]["x"] = x
        return moved

    up_order = list(reversed(ordered_gens[:-1])) if len(ordered_gens) > 1 else []
    down_order = ordered_gens[1:] if len(ordered_gens) > 1 else []

    for _ in range(12):  # converge en pratique en 4-6 itérations
        moved = _sweep(up_order, use_children=True)
        moved += _sweep(down_order, use_children=False)
        if moved < 1.0:  # stabilisé
            break

    return list(positions.values())


def _place_row(
    desired: List[float],
    slot: float,
    gaps: Optional[List[float]] = None,
) -> List[float]:
    """
    Place une rangée de nœuds ordonnés au plus près des positions souhaitées
    `desired`, en respectant l'ordre donné et un écart minimal entre voisins.

    L'écart minimal vaut `slot` par défaut, mais peut varier par paire via
    `gaps` (gaps[i] = écart minimal entre members[i] et members[i+1]) — ce qui
    permet d'élargir l'espace entre familles distinctes d'une même rangée.

    Méthode : régression isotone (PAVA — Pool Adjacent Violators). On cherche
    les positions finales `xs` minimisant Σ(xs[i] − desired[i])² sous la
    contrainte xs[i] − xs[i−1] ≥ gaps[i−1]. En posant cum[i] = Σ des écarts
    avant i, et y[i] = xs[i] − cum[i], la contrainte devient « y non
    décroissant » → régression isotone des cibles t[i] = desired[i] − cum[i],
    résolue optimalement par PAVA.

    Pourquoi PAVA et pas un push + recentrage global : pousser à droite puis
    décaler toute la rangée d'un même delta désaligne chaque sous-famille par
    rapport à son couple parental dès qu'une génération a plusieurs branches.
    PAVA donne le déplacement minimal SANS décalage global : chaque sous-arbre
    se cale sur son propre barycentre, d'où des descentes verticales propres.
    """
    n = len(desired)
    if n == 0:
        return []
    # Écarts cumulés depuis le 1er nœud (cum[0] = 0).
    cum = [0.0] * n
    for i in range(1, n):
        g = gaps[i - 1] if gaps is not None else slot
        cum[i] = cum[i - 1] + g
    # Cibles normalisées : contrainte d'espacement → simple monotonie.
    targets = [desired[i] - cum[i] for i in range(n)]
    # PAVA : on empile des blocs [somme, effectif, moyenne] et on fusionne tant
    # que la moyenne du dernier bloc viole la monotonie (< bloc précédent).
    blocks: List[List[float]] = []
    for t in targets:
        blocks.append([t, 1.0, t])
        while len(blocks) > 1 and blocks[-2][2] > blocks[-1][2]:
            s2, c2, _ = blocks.pop()
            s1, c1, _ = blocks.pop()
            s, c = s1 + s2, c1 + c2
            blocks.append([s, c, s / c])
    y: List[float] = []
    for s, c, v in blocks:
        y.extend([v] * int(c))
    return [y[i] + cum[i] for i in range(n)]


def _reorder_spouses(
    pids: List[uuid.UUID],
    spouses_of: Dict[uuid.UUID, Set[uuid.UUID]],
) -> List[uuid.UUID]:
    """
    Réordonne une génération pour que les conjoints soient adjacents et qu'un
    conjoint « pivot » (marié à plusieurs personnes) se retrouve ENTRE ses
    conjoints.

    Exemple clé : X marié à Y, et Z marié à Y. On veut [X, Y, Z] (Y au milieu)
    et non [X, Y, …, Z]. Cela permet aux enfants du couple (X,Y) de se
    regrouper à gauche et ceux du couple (Z,Y) à droite, puisque les enfants
    sont ensuite triés par position moyenne de leurs parents.

    Algorithme : on isole chaque composante de conjoints, puis on en fait un
    parcours en chaîne (les paires et les chaînes X–Y–Z sont gérées
    naturellement ; les configurations en étoile — polygamie à 3+ — placent le
    pivot adjacent à tous ses conjoints).
    """
    in_set = set(pids)
    adj: Dict[uuid.UUID, Set[uuid.UUID]] = {
        p: {s for s in spouses_of.get(p, set()) if s in in_set} for p in pids
    }

    visited: Set[uuid.UUID] = set()
    result: List[uuid.UUID] = []

    for start in pids:
        if start in visited:
            continue

        # Récupère toute la composante de conjoints reliée à `start`.
        comp: List[uuid.UUID] = []
        seen = {start}
        stack = [start]
        while stack:
            n = stack.pop()
            comp.append(n)
            for s in adj[n]:
                if s not in seen:
                    seen.add(s)
                    stack.append(s)

        if len(comp) == 1:
            visited.add(start)
            result.append(start)
            continue

        result.extend(_order_spouse_component(comp, adj))
        visited.update(comp)

    return result


def _order_spouse_component(
    comp: List[uuid.UUID],
    adj: Dict[uuid.UUID, Set[uuid.UUID]],
) -> List[uuid.UUID]:
    """
    Ordonne une composante de conjoints en chaîne. Démarre de préférence depuis
    un nœud de degré 1 (extrémité) pour que les pivots (degré ≥ 2) se
    retrouvent au milieu : X–Y–Z → [X, Y, Z].
    """
    comp_set = set(comp)

    # Extrémités = conjoints « simples » (un seul mariage dans la composante).
    endpoints = [n for n in comp if len(adj[n] & comp_set) == 1]
    start = endpoints[0] if endpoints else max(comp, key=lambda n: len(adj[n] & comp_set))

    path = [start]
    used = {start}
    cur = start
    while True:
        nxts = [s for s in adj[cur] & comp_set if s not in used]
        if not nxts:
            break
        # On continue par le conjoint le moins « ramifié » pour étendre la chaîne.
        nxts.sort(key=lambda s: len(adj[s] & comp_set))
        nxt = nxts[0]
        path.append(nxt)
        used.add(nxt)
        cur = nxt

    # Cas étoile (polygamie 3+) : on insère les conjoints restants juste à côté
    # d'un conjoint déjà placé pour garder l'adjacence.
    for n in comp:
        if n in used:
            continue
        neighbor = next((s for s in adj[n] & comp_set if s in used), None)
        if neighbor is None:
            path.append(n)
        else:
            path.insert(path.index(neighbor) + 1, n)
        used.add(n)

    return path


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
