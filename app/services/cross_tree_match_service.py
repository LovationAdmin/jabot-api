"""
Cross-tree similarity matching for pre-convergence scan.

For each person in the source tree, finds the best match in the target tree
using a progressive 4-stage algorithm:

  Stage 1 – first_name only (quick filter, threshold 0.55)
  Stage 2 – first_name + last_name (weighted combo, reject if last_name diverges)
  Stage 3 – biographical boosts (birth year, gender, city_of_origin)
  Stage 4 – cross-tree family context (do their respective parents match by name?)

Returns a deduplicated ranked list of CrossTreeMatchPair (one match per source
person, one match per target person — highest confidence wins conflicts).
"""

import logging
from typing import Dict, List, Optional, Set, Tuple
from uuid import UUID

import jellyfish
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.person import Person
from app.models.relationship import Relationship
from app.schemas.tree_meta import CrossTreeMatchPair
from app.services.search_service import compute_name_score, normalize_name

logger = logging.getLogger(__name__)

# Minimum confidence to include a pair in results
_MIN_CONFIDENCE = 0.55  # was 0.35 — trop permissif avec les noms de famille partagés

# First-name score below this → no point scoring further
_FNAME_GATE = 0.55  # was 0.40 — "Moussa" vs "Mamadou" ≈ 0.55 JW, devait passer

# Last-name score below this when both names are present → penalise
_LNAME_REJECT = 0.35


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class _FamilyMap:
    """Pre-computed parent/sibling/uncle_aunt sets by person_id for a tree."""
    def __init__(
        self,
        parents: Dict[UUID, Set[UUID]],
        children: Dict[UUID, Set[UUID]],
        siblings: Dict[UUID, Set[UUID]],
        uncles_aunts: Dict[UUID, Set[UUID]],
    ):
        self.parents = parents
        self.children = children
        self.siblings = siblings
        self.uncles_aunts = uncles_aunts


def _build_family_map(persons: List[Person], rels: List[Relationship]) -> _FamilyMap:
    pid_set = {p.id for p in persons}
    parents: Dict[UUID, Set[UUID]] = {p.id: set() for p in persons}
    children: Dict[UUID, Set[UUID]] = {p.id: set() for p in persons}
    siblings: Dict[UUID, Set[UUID]] = {p.id: set() for p in persons}
    uncles_aunts_direct: Dict[UUID, Set[UUID]] = {p.id: set() for p in persons}
    spouses_of: Dict[UUID, Set[UUID]] = {p.id: set() for p in persons}

    for r in rels:
        a, b = r.person_a_id, r.person_b_id
        if a not in pid_set or b not in pid_set:
            continue
        if r.type == "parent":
            parents[b].add(a)
            children[a].add(b)
        elif r.type == "child":
            parents[a].add(b)
            children[b].add(a)
        elif r.type in ("sibling", "half_sibling", "step_sibling"):
            siblings[a].add(b)
            siblings[b].add(a)
        elif r.type == "uncle_aunt":
            uncles_aunts_direct[b].add(a)   # A is uncle/aunt of B
        elif r.type == "nephew_niece":
            uncles_aunts_direct[a].add(b)   # B is uncle/aunt of A
        elif r.type == "spouse":
            spouses_of[a].add(b)
            spouses_of[b].add(a)

    # Expansion : spouse of an uncle/aunt is also an uncle/aunt (inference at query time)
    uncles_aunts: Dict[UUID, Set[UUID]] = {}
    for pid in pid_set:
        direct = uncles_aunts_direct[pid]
        expanded = set(direct)
        for ua_id in direct:
            expanded |= spouses_of.get(ua_id, set()) & pid_set
        uncles_aunts[pid] = expanded

    return _FamilyMap(parents=parents, children=children, siblings=siblings, uncles_aunts=uncles_aunts)


def _cross_family_context(
    sp: Person,
    tp: Person,
    src_map: _FamilyMap,
    tgt_map: _FamilyMap,
    src_persons: Dict[UUID, Person],
    tgt_persons: Dict[UUID, Person],
) -> float:
    """
    Returns a value in [-0.5, 1.0]:
    - Positive: their parents/siblings/oncles-tantes correspond well by name
    - Zero: no family context available in either tree
    - Negative: both have parents that explicitly DON'T match → penalty

    Signal hierarchy (strongest → weakest):
      1. Parents (weight 1.0)
      2. Siblings (weight 0.5)
      3. Oncles/tantes + leurs conjoints par alliance (weight 0.3)
    """
    sp_parent_ids = src_map.parents.get(sp.id, set())
    tp_parent_ids = tgt_map.parents.get(tp.id, set())

    if sp_parent_ids and tp_parent_ids:
        matched = _count_name_matches(sp_parent_ids, tp_parent_ids, src_persons, tgt_persons)
        total = min(len(sp_parent_ids), len(tp_parent_ids))
        if total == 0:
            return 0.0
        if matched == 0:
            return -0.5  # parents présents des deux côtés mais aucun ne correspond
        return min(matched / total, 1.0)  # borné à 1.0 même si matched > total

    if sp_parent_ids or tp_parent_ids:
        return 0.0  # une seule fiche a des parents → pas de contexte comparable

    # Pas de parents : essayer fratrie
    sp_sib_ids = src_map.siblings.get(sp.id, set())
    tp_sib_ids = tgt_map.siblings.get(tp.id, set())
    if sp_sib_ids and tp_sib_ids:
        matched = _count_name_matches(sp_sib_ids, tp_sib_ids, src_persons, tgt_persons)
        total = min(len(sp_sib_ids), len(tp_sib_ids))
        if total == 0:
            return 0.0
        return min(matched / total, 1.0) * 0.5  # signal plus faible que les parents

    # Pas de parents ni fratrie : essayer oncles/tantes (+ conjoints par alliance)
    sp_ua_ids = src_map.uncles_aunts.get(sp.id, set())
    tp_ua_ids = tgt_map.uncles_aunts.get(tp.id, set())
    if sp_ua_ids and tp_ua_ids:
        matched = _count_name_matches(sp_ua_ids, tp_ua_ids, src_persons, tgt_persons)
        total = min(len(sp_ua_ids), len(tp_ua_ids))
        if total == 0:
            return 0.0
        return min(matched / total, 1.0) * 0.3  # signal tertiaire

    return 0.0


def _count_name_matches(
    ids_a: Set[UUID],
    ids_b: Set[UUID],
    map_a: Dict[UUID, Person],
    map_b: Dict[UUID, Person],
) -> int:
    """How many persons in ids_a have a name match (>0.7) among persons in ids_b."""
    matched = 0
    for aid in ids_a:
        pa = map_a.get(aid)
        if not pa:
            continue
        for bid in ids_b:
            pb = map_b.get(bid)
            if not pb:
                continue
            s, _ = compute_name_score(pa.first_name, pb.first_name, pb.last_name, pb.nicknames)
            if s > 0.70:
                matched += 1
                break
    return matched


def _progressive_score(
    sp: Person,
    tp: Person,
    src_fam: _FamilyMap,
    tgt_fam: _FamilyMap,
    src_persons: Dict[UUID, Person],
    tgt_persons: Dict[UUID, Person],
) -> Tuple[float, List[str], str]:
    """
    Returns (confidence, reasons, stage_name).
    stage_name is one of: 'first_name', 'full_name', 'full_name_context', 'rejected'.
    """
    reasons: List[str] = []

    # ── Stage 1: first_name ───────────────────────────────────────────────
    fn_score, fn_reasons = compute_name_score(
        sp.first_name, tp.first_name, None, tp.nicknames
    )
    if fn_score < _FNAME_GATE:
        return 0.0, [], "rejected"

    reasons.extend(fn_reasons)
    stage = "first_name"

    # ── Stage 2: last_name (when both are present) ────────────────────────
    if sp.last_name and tp.last_name:
        ln_score, ln_reasons = compute_name_score(sp.last_name, tp.last_name, None, None)
        if ln_score < _LNAME_REJECT:
            # Last names diverge → very likely different people
            return fn_score * 0.15, reasons, "rejected"
        reasons.extend(ln_reasons)
        # Le prénom est le signal dominant. Le nom de famille confirme mais ne compense pas
        # un prénom faible : en Afrique de l'Ouest les noms de clan sont partagés par des
        # milliers de personnes (Diallo, Kouyaté, Traoré…).
        base = fn_score * 0.70 + ln_score * 0.10  # was 0.45 + 0.35
        stage = "full_name"
    else:
        # One or both missing last names → rely more on first_name + context
        base = fn_score * 0.60  # was 0.45

    # ── Stage 3: biographical boosts ─────────────────────────────────────
    bio = 0.0
    if sp.gender and tp.gender and sp.gender == tp.gender:
        bio += 0.04

    if sp.birth_date and tp.birth_date:
        if sp.birth_date.year == tp.birth_date.year:
            bio += 0.08
            reasons.append(f"Même année de naissance ({sp.birth_date.year})")
        if sp.birth_date == tp.birth_date:
            bio += 0.04

    if sp.city_of_origin and tp.city_of_origin:
        try:
            city_sim = jellyfish.jaro_winkler_similarity(
                normalize_name(sp.city_of_origin),
                normalize_name(tp.city_of_origin),
            )
            if city_sim > 0.80:
                bio += 0.04
                reasons.append(f"Même ville d'origine ({tp.city_of_origin})")
        except Exception:
            pass

    score = min(base + bio, 1.0)

    # ── Stage 4: cross-tree family context ───────────────────────────────
    ctx = _cross_family_context(sp, tp, src_fam, tgt_fam, src_persons, tgt_persons)
    if ctx > 0:
        score = min(score + ctx * 0.25, 1.0)
        reasons.append(f"Proches correspondants ({ctx:.0%})")
        stage = "full_name_context"
    elif ctx < 0:
        score = max(score + ctx * 0.30, 0.0)
        reasons.append("Proches différents dans les deux arbres")

    return score, reasons, stage


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scan_cross_tree_matches(
    db: AsyncSession,
    source_tree_id: UUID,
    target_tree_id: UUID,
) -> Tuple[List[CrossTreeMatchPair], int]:
    """
    Scan source tree for persons that match persons in target tree.

    Returns:
        (proposed_pairs, unmatched_source_count)

    Proposed pairs are deduplicated: each source person appears at most once,
    each target person appears at most once (highest confidence wins).
    """
    # ── Fetch persons ─────────────────────────────────────────────────────
    src_rows = (await db.execute(
        select(Person).where(
            Person.family_tree_id == source_tree_id,
            Person.deleted_at.is_(None),
        )
    )).scalars().all()

    tgt_rows = (await db.execute(
        select(Person).where(
            Person.family_tree_id == target_tree_id,
            Person.deleted_at.is_(None),
        )
    )).scalars().all()

    if not src_rows or not tgt_rows:
        return [], len(src_rows)

    src_persons: Dict[UUID, Person] = {p.id: p for p in src_rows}
    tgt_persons: Dict[UUID, Person] = {p.id: p for p in tgt_rows}

    # ── Fetch relationships ───────────────────────────────────────────────
    src_rels = (await db.execute(
        select(Relationship).where(
            Relationship.family_tree_id == source_tree_id,
        )
    )).scalars().all()

    tgt_rels = (await db.execute(
        select(Relationship).where(
            Relationship.family_tree_id == target_tree_id,
        )
    )).scalars().all()

    src_fam = _build_family_map(src_rows, src_rels)
    tgt_fam = _build_family_map(tgt_rows, tgt_rels)

    # ── Score all (source, target) pairs ─────────────────────────────────
    # We collect (score, source_id, target_id, reasons, stage) and sort desc.
    scored: List[Tuple[float, UUID, UUID, List[str], str]] = []

    for sp in src_rows:
        for tp in tgt_rows:
            score, reasons, stage = _progressive_score(
                sp, tp, src_fam, tgt_fam, src_persons, tgt_persons
            )
            if score >= _MIN_CONFIDENCE:
                scored.append((score, sp.id, tp.id, reasons, stage))

    # Sort by confidence descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Deduplicate: each source/target appears at most once (greedy, highest first)
    used_sources: Set[UUID] = set()
    used_targets: Set[UUID] = set()
    pairs: List[CrossTreeMatchPair] = []

    for score, sid, tid, reasons, stage in scored:
        if sid in used_sources or tid in used_targets:
            continue
        used_sources.add(sid)
        used_targets.add(tid)

        sp = src_persons[sid]
        tp = tgt_persons[tid]

        pairs.append(CrossTreeMatchPair(
            source_person_id=str(sid),
            source_first_name=sp.first_name,
            source_last_name=sp.last_name,
            target_person_id=str(tid),
            target_first_name=tp.first_name,
            target_last_name=tp.last_name,
            confidence=round(score, 3),
            match_reasons=reasons[:4],
            match_stage=stage,
        ))

    unmatched = len(src_rows) - len(used_sources)
    return pairs, unmatched
