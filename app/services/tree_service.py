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

GENERATION_HEIGHT = 380   # px between generations
NODE_WIDTH = 240           # px per node (layout unit, card is 208 px wide → 32 px margin)
NODE_SPACING = 110         # horizontal gap between nodes within a family unit
FAMILY_GAP = 200           # extra gap between distinct families on the same row
CLUSTER_SPACING = 650      # gap between fully disconnected family trees


def compute_tree_layout(
    persons: List[Person],
    relationships: List[Relationship],
) -> List[Dict]:
    """
    Assign x,y positions to each person for React Flow rendering.

    Fonction PURE CPU (aucune I/O base) : Union-Find + balayages barycentre
    itératifs + PAVA. Appelée sur CHAQUE GET /tree, sur un gros arbre elle peut
    monopoliser plusieurs centaines de ms. Avec un seul worker uvicorn, cela
    bloque l'event loop et fait expirer le health check Render (timeout 5 s) →
    redémarrage de l'instance. Les appelants DOIVENT donc l'exécuter hors de
    l'event loop via asyncio.to_thread(). On opère uniquement sur des attributs
    déjà chargés des objets ORM (id, birth_date, first_name…), jamais de lazy
    load, donc l'exécution en thread est sûre.

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
        elif r.type in ("sibling", "half_sibling", "step_sibling"):
            # Tous les types de fratrie partagent la même génération.
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

    # Pass 2: inférence pour les nœuds non encore placés — un conjoint ou un
    # frère/sœur d'un nœud déjà placé hérite de SA génération. Indispensable pour
    # les conjoints « mariés dans la famille » (sans parents propres) : sans ça
    # ils tombaient dans le fallback (max+1) et se retrouvaient une ligne plus
    # bas que leur partenaire. On ne fait que REMPLIR les générations manquantes.
    changed = True
    while changed:
        changed = False
        for pid in pid_set:
            if pid in generations:
                continue
            for nb in (spouses_of[pid] | siblings_of[pid]):
                if nb in generations:
                    generations[pid] = generations[nb]
                    changed = True
                    break

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

    # Pass 5 : contraintes générationnelles appliquées SIMULTANÉMENT jusqu'à
    # point fixe, sur des générations désormais toutes définies. Les traiter
    # séparément laissait la règle « parent au-dessus de l'enfant » redécaler UN
    # SEUL des deux conjoints après leur égalisation, sans les réaligner → couples
    # à des hauteurs différentes. Chaque opération ne fait qu'AUGMENTER une
    # génération (monotone) → convergence garantie.
    #   a) conjoints à la même génération (alignés sur le plus bas = max)
    #   b) fratrie (y c. demi / par alliance) à la même génération
    #   c) parent strictement au-dessus de l'enfant
    for _ in range(len(pid_set) + 10):
        changed = False

        for pid in pid_set:                       # (c) parent au-dessus de l'enfant
            p_gen = generations[pid]
            for child_id in children_of[pid]:
                if generations[child_id] <= p_gen:
                    generations[child_id] = p_gen + 1
                    changed = True

        for pid in pid_set:                       # (a) conjoints au même niveau
            for sp in spouses_of[pid]:
                if generations[sp] != generations[pid]:
                    m = max(generations[pid], generations[sp])
                    generations[pid] = generations[sp] = m
                    changed = True

        for pid in pid_set:                       # (b) fratrie au même niveau
            for sib in siblings_of[pid]:
                if generations[sib] != generations[pid]:
                    m = max(generations[pid], generations[sib])
                    generations[pid] = generations[sib] = m
                    changed = True

        if not changed:
            break

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
    commit: bool = True,
) -> Dict:
    """
    Merge source person into target person:
    1. Copy non-null fields from source → target (target wins on conflicts)
    2. Transfer all relationships: replace source_id with target_id
    3. Transfer all media records
    4. Soft-delete the source person

    Si `commit=False`, ne valide pas la transaction : l'appelant est responsable
    du commit (utilisé par la convergence d'arbres pour rester atomique).

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

    # Repointer les utilisateurs dont la fiche (person_id) était la source :
    # sinon leur lien « ma fiche » casserait après la fusion.
    from app.models.user import User as UserModel
    from sqlalchemy import update as _sql_update
    await db.execute(
        _sql_update(UserModel)
        .where(UserModel.person_id == source_id)
        .values(person_id=target_id)
    )

    # Soft-delete source
    source.deleted_at = datetime.now(timezone.utc)

    if commit:
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


async def converge_trees(
    db: AsyncSession,
    user_id: uuid.UUID,
    source_tree_id: uuid.UUID,
    target_tree_id: uuid.UUID,
    source_person_id: Optional[uuid.UUID],
    target_person_id: Optional[uuid.UUID],
    additional_merge_pairs: Optional[List[Dict]] = None,
    skip_permission_check: bool = False,
) -> Dict:
    """Fusionne (« convergence ») l'arbre source DANS l'arbre cible.

    Cas d'usage : un utilisateur, visiteur invité dans l'arbre cible (sa vraie
    famille), avait aussi démarré son propre arbre source lors de l'onboarding.
    En découvrant sa fiche dans la cible, il rapatrie tout le contenu de son
    arbre source dans la cible, fusionne sa fiche en double, et devient membre.

    Stratégie (cf. analyse) : re-pointage en masse de `family_tree_id`
    (persons, relationships, invitations) de source → cible, puis fusion du
    SEUL nœud d'identité confirmé (la fiche de l'utilisateur). Les éventuels
    doublons de proches restent des fiches distinctes, à fusionner manuellement
    via l'outil de fusion existant — aucune fusion automatique hasardeuse.

    Le tout dans UNE transaction (atomique). L'appelant gère cache/WS ensuite.
    """
    from fastapi import HTTPException
    from sqlalchemy import update as _sql_update, func as _func
    from app.models.family_tree import FamilyTree, UserTreeAccess
    from app.models.invitation import Invitation
    from app.services import tree_access_service

    if source_tree_id == target_tree_id:
        raise HTTPException(status_code=400, detail="Les arbres source et cible sont identiques.")

    # ── Garde-fous d'autorisation ──────────────────────────────────────────
    # skip_permission_check=True est réservé à l'approbation d'une merge request,
    # où les droits ont déjà été vérifiés par le route handler.
    if not skip_permission_check:
        src_role = await tree_access_service.get_role(db, user_id, source_tree_id)
        if src_role != "owner":
            raise HTTPException(status_code=403, detail="Vous devez être propriétaire de l'arbre source.")

        tgt_role = await tree_access_service.get_role(db, user_id, target_tree_id)
        if tgt_role is None:
            raise HTTPException(status_code=403, detail="Vous n'avez pas accès à l'arbre cible.")

    # 3. L'arbre source ne doit avoir qu'un seul accès (l'utilisateur lui-même).
    #    S'il a invité d'autres personnes, on refuse plutôt que de deviner.
    count_access = (await db.execute(
        select(_func.count()).select_from(UserTreeAccess).where(
            UserTreeAccess.family_tree_id == source_tree_id
        )
    )).scalar_one()
    if count_access > 1:
        raise HTTPException(
            status_code=409,
            detail="L'arbre source est partagé avec d'autres membres ; la convergence n'est pas possible automatiquement.",
        )

    # Vérifier que la cible existe.
    target_tree = (await db.execute(
        select(FamilyTree).where(FamilyTree.id == target_tree_id)
    )).scalar_one_or_none()
    if target_tree is None:
        raise HTTPException(status_code=404, detail="Arbre cible introuvable.")
    source_tree = (await db.execute(
        select(FamilyTree).where(FamilyTree.id == source_tree_id)
    )).scalar_one_or_none()
    if source_tree is None:
        raise HTTPException(status_code=404, detail="Arbre source introuvable.")

    # Valider l'appartenance des fiches à leurs arbres respectifs (avant déplacement).
    if source_person_id is not None:
        sp = (await db.execute(
            select(Person).where(Person.id == source_person_id, Person.deleted_at.is_(None))
        )).scalar_one_or_none()
        if sp is None or sp.family_tree_id != source_tree_id:
            raise HTTPException(status_code=404, detail="Fiche source introuvable dans l'arbre source.")
    if target_person_id is not None:
        tp = (await db.execute(
            select(Person).where(Person.id == target_person_id, Person.deleted_at.is_(None))
        )).scalar_one_or_none()
        if tp is None or tp.family_tree_id != target_tree_id:
            raise HTTPException(status_code=404, detail="Fiche cible introuvable dans l'arbre cible.")

    # ── Re-pointage en masse (UPDATE atomiques) ─────────────────────────────
    from app.models.ignored_duplicate import IgnoredDuplicate
    from sqlalchemy.dialects.postgresql import insert as _pg_insert

    moved_persons = (await db.execute(
        _sql_update(Person)
        .where(Person.family_tree_id == source_tree_id)
        .values(family_tree_id=target_tree_id)
    )).rowcount
    (await db.execute(
        _sql_update(Relationship)
        .where(Relationship.family_tree_id == source_tree_id)
        .values(family_tree_id=target_tree_id)
    ))
    (await db.execute(
        _sql_update(Invitation)
        .where(Invitation.family_tree_id == source_tree_id)
        .values(family_tree_id=target_tree_id)
    ))

    # ── Migration des doublons ignorés (source → cible) ─────────────────────
    # Les IgnoredDuplicate sont liés à family_tree_id avec CASCADE. Si on ne les
    # migre pas avant de supprimer l'arbre source, toutes les paires ignorées
    # disparaissent et réapparaissent comme nouveaux doublons dans l'arbre cible.
    src_ignored = (await db.execute(
        select(IgnoredDuplicate).where(IgnoredDuplicate.family_tree_id == source_tree_id)
    )).scalars().all()
    for ig in src_ignored:
        stmt = _pg_insert(IgnoredDuplicate).values(
            id=uuid.uuid4(),
            family_tree_id=target_tree_id,
            person_low_id=ig.person_low_id,
            person_high_id=ig.person_high_id,
            ignored_by=ig.ignored_by,
        ).on_conflict_do_nothing(
            constraint="uq_ignored_duplicate_pair"
        )
        await db.execute(stmt)

    # ── Fusion de l'unique nœud d'identité confirmé ─────────────────────────
    merge_summary = None
    if source_person_id and target_person_id and source_person_id != target_person_id:
        merge_summary = await merge_persons(
            db, source_id=source_person_id, target_id=target_person_id, commit=False
        )

    # ── Fusions supplémentaires confirmées par l'utilisateur ─────────────────
    # Les fiches ont déjà été re-pointées vers target_tree_id, donc merge_persons
    # peut les trouver normalement. On collecte les erreurs sans interrompre.
    additional_merges = 0
    # Normalize to str for consistent comparison (pairs arrive as str, IDs as UUID)
    source_person_id_str = str(source_person_id) if source_person_id else None
    already_merged_targets: set = {str(target_person_id)} if target_person_id else set()

    for pair in (additional_merge_pairs or []):
        src_pid = str(pair.get("source_person_id") if isinstance(pair, dict) else pair.source_person_id)
        tgt_pid = str(pair.get("target_person_id") if isinstance(pair, dict) else pair.target_person_id)
        if not src_pid or not tgt_pid:
            continue
        if src_pid == source_person_id_str:
            # Already handled as identity merge above
            continue
        if tgt_pid in already_merged_targets:
            # Prevent two sources being merged into the same target in one pass
            logger.warning("converge_trees: skipping duplicate target %s", tgt_pid)
            continue
        try:
            await merge_persons(db, source_id=src_pid, target_id=tgt_pid, commit=False)
            additional_merges += 1
            already_merged_targets.add(tgt_pid)
        except Exception as exc:
            logger.warning("converge_trees: additional merge %s→%s failed: %s", src_pid, tgt_pid, exc)

    # ── Promotion de l'utilisateur : visiteur → membre de la cible ──────────
    await tree_access_service.grant_access(db, user_id, target_tree_id, "member")

    # ── Suppression de l'arbre source (vidé) ────────────────────────────────
    # CASCADE retire les UserTreeAccess restants de l'arbre source.
    await db.delete(source_tree)

    await db.commit()

    return {
        "message": "Convergence réussie",
        "source_tree_id": str(source_tree_id),
        "target_tree_id": str(target_tree_id),
        "persons_moved": moved_persons,
        "identity_merged": merge_summary is not None,
        "additional_merges": additional_merges,
        "merge": merge_summary,
    }
