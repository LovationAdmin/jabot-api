"""
Search service: fuzzy + phonetic + diminutive-aware search for West African names.

Pipeline:
1. Normalize name (lowercase, remove accents, collapse whitespace)
2. Trigram search via PostgreSQL pg_trgm similarity() — threshold 0.3
3. Phonetic search via jellyfish Soundex + Metaphone
4. Diminutive/variant group matching (West African name groups)
5. Score combination → confidence 0.0–1.0
6. Family context boost when parent/sibling names also match
"""

import logging
import unicodedata
from typing import List, Optional, Dict, Set, Tuple

import jellyfish
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select, or_
from sqlalchemy.orm import selectinload

from app.models.person import Person
from app.models.relationship import Relationship
from app.schemas.person import SearchRequest, SearchMatch, PersonResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# West African diminutive / variant groups
# Each set is a cluster of names that refer to the same person
# ---------------------------------------------------------------------------
WEST_AFRICAN_NAME_GROUPS: List[Set[str]] = [
    {"ibrahima", "ibrahim", "ibou", "brama", "baba", "ibre"},
    {"fatimata", "fatou", "faty", "fat", "fatoumata", "fatima"},
    {"mamadou", "madou", "mady", "papa", "mamadu", "mamd"},
    {"ousmane", "osmane", "ousman", "usman", "osman"},
    {"abdoulaye", "abdou", "laye", "abdoullaye", "abdoulai"},
    {"moussa", "musa", "mouss"},
    {"aminata", "ami", "minate", "amin", "aminat"},
    {"aissatou", "aissata", "aissetu", "aissetou", "awa"},
    {"mariama", "mariam", "marie", "maria", "maram"},
    {"boubacar", "bouba", "boubakar", "boubak", "bakary"},
    {"cheikh", "sheikh", "cheik", "cheikhou", "seikh"},
    {"demba", "demd", "demb"},
    {"saliou", "sali", "salif", "salieu"},
    {"pape", "papa", "pa"},
    {"modou", "modo", "mod"},
    {"aliou", "ali", "alioune", "aliu"},
    {"souleymane", "souley", "suleyman", "suleymane", "sulayman"},
    {"amadou", "amadu", "amad"},
    {"seydou", "seydi", "seyd"},
    {"doudou", "doud"},
    {"coumba", "cumba", "kumba"},
    {"rokhaya", "rokha", "rokhya"},
    {"ndèye", "ndeye", "ndey"},
    {"thierno", "tierno", "terno"},
    {"alassane", "alass", "alasan"},
    {"lamine", "lamin", "lamin"},
    {"mouhamed", "mouhammed", "muhammed", "mohammed", "mohamed", "mohamad"},
    {"khadija", "khadijatou", "hadja", "hadj"},
    {"bineta", "binta", "binet"},
    {"kadiatou", "kadia", "kadja"},
    {"oumou", "oum", "umou"},
    {"assane", "assaan", "assan"},
    {"mor", "more", "morel"},
    {"talla", "tall"},
    {"babacar", "babac", "babs"},
    {"elhadj", "elhaj", "elhadji"},
    {"gorgui", "gorgi"},
    {"malick", "malik", "malic"},
]

# Build a fast lookup: normalized_name → group_index
_NAME_TO_GROUP: Dict[str, int] = {}
for _gidx, _group in enumerate(WEST_AFRICAN_NAME_GROUPS):
    for _name in _group:
        _NAME_TO_GROUP[_name] = _gidx


def normalize_name(name: str) -> str:
    """Lowercase, remove accents, collapse whitespace."""
    if not name:
        return ""
    # NFD decomposition strips combining accents
    nfd = unicodedata.normalize("NFD", name)
    ascii_str = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return ascii_str.lower().strip()


def names_in_same_group(a: str, b: str) -> bool:
    """Return True if both names belong to the same West African diminutive group."""
    na, nb = normalize_name(a), normalize_name(b)
    ga = _NAME_TO_GROUP.get(na)
    gb = _NAME_TO_GROUP.get(nb)
    if ga is None or gb is None:
        return False
    return ga == gb


def phonetic_similarity(a: str, b: str) -> float:
    """
    Returns 0.0–1.0 based on Soundex + Metaphone agreement
    and Jaro-Winkler distance on the phonetic codes.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0

    score = 0.0

    # Soundex exact match
    try:
        if jellyfish.soundex(na) == jellyfish.soundex(nb):
            score += 0.4
    except Exception:
        pass

    # Metaphone exact match
    try:
        if jellyfish.metaphone(na) == jellyfish.metaphone(nb):
            score += 0.4
    except Exception:
        pass

    # Jaro-Winkler on raw normalized names (captures partial similarity)
    try:
        jw = jellyfish.jaro_winkler_similarity(na, nb)
        score += jw * 0.2
    except Exception:
        pass

    return min(score, 1.0)


def compute_name_score(query: str, candidate_first: str, candidate_last: Optional[str],
                        candidate_nicknames: Optional[List[str]]) -> Tuple[float, List[str]]:
    """
    Compute a combined name match score and collect reasons.
    Returns (score 0.0-1.0, list of match reasons).
    """
    reasons: List[str] = []
    scores: List[float] = []

    nq = normalize_name(query)
    targets = [(normalize_name(candidate_first), "prénom")]
    if candidate_last:
        targets.append((normalize_name(candidate_last), "nom de famille"))
    if candidate_nicknames:
        for nick in candidate_nicknames:
            targets.append((normalize_name(nick), f"surnom '{nick}'"))

    best = 0.0
    for target_name, label in targets:
        if not target_name:
            continue

        # Exact match
        if nq == target_name:
            best = max(best, 1.0)
            reasons.append(f"Correspondance exacte sur {label}")
            continue

        # Prefix match (e.g. "Ibra" matches "Ibrahim")
        if target_name.startswith(nq) or nq.startswith(target_name):
            s = min(len(nq), len(target_name)) / max(len(nq), len(target_name))
            s = 0.5 + s * 0.4  # scale to 0.5–0.9
            if s > best:
                best = s
                reasons.append(f"Correspondance partielle sur {label}")

        # Diminutive group
        if names_in_same_group(nq, target_name):
            s = 0.85
            if s > best:
                best = s
                reasons.append(f"Variante/diminutif du {label}")

        # Phonetic
        ph = phonetic_similarity(nq, target_name)
        if ph > 0.5:
            s = ph * 0.8
            if s > best:
                best = s
                reasons.append(f"Similitude phonétique sur {label} ({ph:.0%})")

        # Jaro-Winkler raw
        try:
            jw = jellyfish.jaro_winkler_similarity(nq, target_name)
            s = jw * 0.75
            if s > best:
                best = s
                if jw > 0.7:
                    reasons.append(f"Similitude orthographique sur {label} ({jw:.0%})")
        except Exception:
            pass

    return best, list(dict.fromkeys(reasons))  # deduplicate reasons


async def _trigram_search(
    db: AsyncSession,
    name: str,
    threshold: float = 0.3,
) -> List[Tuple[str, float]]:
    """
    Use pg_trgm similarity() to find persons with similar names.
    Returns list of (person_id_str, max_trgm_score).
    """
    nname = normalize_name(name)
    sql = text("""
        SELECT id::text,
               GREATEST(
                   similarity(lower(first_name), :name),
                   COALESCE(similarity(lower(last_name), :name), 0)
               ) AS trgm_score
        FROM persons
        WHERE deleted_at IS NULL
          AND (
              similarity(lower(first_name), :name) > :threshold
              OR similarity(lower(last_name), :name) > :threshold
          )
        ORDER BY trgm_score DESC
        LIMIT 50
    """)
    try:
        result = await db.execute(sql, {"name": nname, "threshold": threshold})
        rows = result.fetchall()
        return [(row[0], float(row[1])) for row in rows]
    except Exception as e:
        logger.warning(f"Trigram search failed (pg_trgm maybe not installed?): {e}")
        return []


async def search_persons(
    db: AsyncSession,
    req: SearchRequest,
    trigram_threshold: float = 0.3,
) -> List[SearchMatch]:
    """
    Main search entry point.
    Returns a ranked list of SearchMatch with confidence scores.
    """
    # Collect all candidate persons

    # Step 1: trigram search from PostgreSQL for primary name
    candidate_ids: Dict[str, float] = {}  # id → best trgm score

    if req.name:
        trgm_hits = await _trigram_search(db, req.name, threshold=trigram_threshold)
        for pid, score in trgm_hits:
            candidate_ids[pid] = max(candidate_ids.get(pid, 0), score)

    if req.nickname:
        trgm_hits = await _trigram_search(db, req.nickname, threshold=trigram_threshold)
        for pid, score in trgm_hits:
            candidate_ids[pid] = max(candidate_ids.get(pid, 0), score * 0.9)

    # Step 2: phonetic + diminutive fallback — scan all persons
    # Only do full scan if trigram returned few results
    if len(candidate_ids) < 10 and req.name:
        all_persons_result = await db.execute(
            select(Person)
            .options(selectinload(Person.canvas_position), selectinload(Person.media))
            .where(Person.deleted_at.is_(None)).limit(2000)
        )
        all_persons = all_persons_result.scalars().all()
        for p in all_persons:
            pid = str(p.id)
            if pid in candidate_ids:
                continue
            ph = phonetic_similarity(req.name, p.first_name)
            if ph > 0.5:
                candidate_ids[pid] = ph * 0.6
            if p.last_name:
                ph2 = phonetic_similarity(req.name, p.last_name)
                if ph2 > 0.5:
                    candidate_ids[pid] = max(candidate_ids.get(pid, 0), ph2 * 0.6)
            if p.nicknames:
                for nick in p.nicknames:
                    ph3 = phonetic_similarity(req.name, nick)
                    if ph3 > 0.5:
                        candidate_ids[pid] = max(candidate_ids.get(pid, 0), ph3 * 0.55)
            # Diminutive check
            nq = normalize_name(req.name)
            if names_in_same_group(nq, normalize_name(p.first_name)):
                candidate_ids[pid] = max(candidate_ids.get(pid, 0), 0.7)

    if not candidate_ids:
        return []

    # Fetch candidate persons
    import uuid as _uuid
    uuid_list = []
    for pid in candidate_ids:
        try:
            uuid_list.append(_uuid.UUID(pid))
        except ValueError:
            pass

    persons_result = await db.execute(
        select(Person)
        .options(selectinload(Person.canvas_position), selectinload(Person.media))
        .where(Person.id.in_(uuid_list), Person.deleted_at.is_(None))
    )
    persons = persons_result.scalars().all()
    person_map = {str(p.id): p for p in persons}

    # Step 3: Build detailed scores
    matches: List[SearchMatch] = []

    for p in persons:
        pid = str(p.id)
        trgm_score = candidate_ids.get(pid, 0.0)
        all_reasons: List[str] = []
        component_scores: List[float] = []

        # Primary name score
        if req.name:
            name_score, name_reasons = compute_name_score(
                req.name, p.first_name, p.last_name, p.nicknames
            )
            component_scores.append(name_score)
            all_reasons.extend(name_reasons)

        # Nickname score
        if req.nickname:
            nick_score, nick_reasons = compute_name_score(
                req.nickname, p.first_name, p.last_name, p.nicknames
            )
            component_scores.append(nick_score * 0.85)
            all_reasons.extend(nick_reasons)

        # Blend trgm + local scores
        if component_scores:
            local_score = max(component_scores)
            confidence = local_score * 0.6 + trgm_score * 0.4
        else:
            confidence = trgm_score * 0.5

        # Family context boost: check parent/sibling names
        if (req.parent_names or req.sibling_names) and confidence > 0.1:
            context_boost = await _family_context_boost(db, p, req.parent_names, req.sibling_names)
            if context_boost > 0:
                all_reasons.append(f"Correspondance familiale ({context_boost:.0%})")
                confidence = min(confidence + context_boost * 0.3, 1.0)

        # City of origin boost
        if req.city_of_origin and p.city_of_origin:
            city_sim = jellyfish.jaro_winkler_similarity(
                normalize_name(req.city_of_origin), normalize_name(p.city_of_origin)
            )
            if city_sim > 0.8:
                confidence = min(confidence + 0.1, 1.0)
                all_reasons.append(f"Même ville d'origine ({p.city_of_origin})")

        if confidence >= 0.2:
            matches.append(SearchMatch(
                person=PersonResponse.model_validate(p),
                confidence=round(confidence, 3),
                match_reasons=all_reasons[:5],  # cap at 5 reasons
            ))

    # Sort by confidence descending
    matches.sort(key=lambda m: m.confidence, reverse=True)
    return matches[:20]  # return top 20


async def find_cross_tree_matches(
    db: AsyncSession,
    person: "Person",
    current_tree_id,
) -> list:
    """
    Recherche dans TOUS les autres arbres des fiches similaires à `person`.

    Stratégie :
    1. Récupère les parents/frères-sœurs de la personne dans son arbre (contexte)
    2. Lance search_persons() — qui cherche déjà cross-arbre (pas de filtre tree_id)
    3. Filtre pour exclure l'arbre courant
    4. Retourne la meilleure correspondance par arbre, avec le nom de l'arbre

    Seuil minimum : 0.35. Retourne au plus 5 arbres.
    """
    from app.models.relationship import Relationship as RelModel
    from app.models.family_tree import FamilyTree
    from app.schemas.person import CrossTreeMatch, SearchRequest as _SR

    # ── Contexte familial immédiat ────────────────────────────────────────
    rels_result = await db.execute(
        select(RelModel).where(
            RelModel.family_tree_id == current_tree_id,
            or_(RelModel.person_a_id == person.id, RelModel.person_b_id == person.id),
        )
    )
    rels = rels_result.scalars().all()

    parent_ids, sibling_ids = [], []
    for r in rels:
        if r.type == "parent" and r.person_b_id == person.id:
            parent_ids.append(r.person_a_id)
        elif r.type == "child" and r.person_a_id == person.id:
            parent_ids.append(r.person_b_id)
        elif r.type in ("sibling", "half_sibling", "step_sibling"):
            other = r.person_b_id if r.person_a_id == person.id else r.person_a_id
            sibling_ids.append(other)

    context_persons = {}
    all_ctx_ids = list(set(parent_ids + sibling_ids))
    if all_ctx_ids:
        ctx_rows = (await db.execute(
            select(Person).where(Person.id.in_(all_ctx_ids), Person.deleted_at.is_(None))
        )).scalars().all()
        context_persons = {p.id: p for p in ctx_rows}

    parent_names = [
        context_persons[pid].first_name
        for pid in parent_ids if pid in context_persons
    ]
    sibling_names = [
        context_persons[sid].first_name
        for sid in sibling_ids if sid in context_persons
    ]

    # ── Recherche cross-arbre (tolérance élargie) ─────────────────────────
    # On lance plusieurs passes (prénom, nom, surnoms) et on fusionne par meilleur score
    _CROSS_TRGM_THRESHOLD = 0.25
    _CROSS_MIN_CONFIDENCE = 0.35

    search_terms = [person.first_name]
    if person.last_name:
        search_terms.append(person.last_name)
    if person.nicknames:
        search_terms.extend(person.nicknames)

    all_matches: dict[str, "SearchMatch"] = {}  # person_id → best match

    for term in search_terms:
        req = _SR(
            name=term,
            parent_names=parent_names or None,
            sibling_names=sibling_names or None,
            city_of_origin=person.city_of_origin,
        )
        # Passe trigram avec seuil abaissé pour attraper les variantes phonétiques
        for m in await search_persons(db, req, trigram_threshold=_CROSS_TRGM_THRESHOLD):
            pid = str(m.person.id)
            if pid not in all_matches or all_matches[pid].confidence < m.confidence:
                all_matches[pid] = m

    matches = list(all_matches.values())

    # ── Filtre + regroupement par arbre ───────────────────────────────────
    str_tree = str(current_tree_id)
    pid_list = [
        m.person.family_tree_id
        for m in matches
        if m.confidence >= _CROSS_MIN_CONFIDENCE and str(m.person.family_tree_id) != str_tree
    ]
    if not pid_list:
        return []

    # Noms des arbres en une requête
    tree_ids_needed = list({str(ft) for ft in pid_list if ft})
    import uuid as _uuid
    tree_uuid_list = []
    for tid in tree_ids_needed:
        try:
            tree_uuid_list.append(_uuid.UUID(str(tid)))
        except ValueError:
            pass

    tree_names: dict = {}
    if tree_uuid_list:
        tn_rows = (await db.execute(
            select(FamilyTree.id, FamilyTree.name).where(FamilyTree.id.in_(tree_uuid_list))
        )).all()
        tree_names = {str(tid): name for tid, name in tn_rows}

    # Meilleure correspondance par arbre
    best_per_tree: dict[str, CrossTreeMatch] = {}
    for m in matches:
        if m.confidence < _CROSS_MIN_CONFIDENCE:
            continue
        tid = str(m.person.family_tree_id)
        if tid == str_tree:
            continue
        if tid in best_per_tree and best_per_tree[tid].confidence >= m.confidence:
            continue
        best_per_tree[tid] = CrossTreeMatch(
            tree_id=tid,
            tree_name=tree_names.get(tid, "Arbre"),
            person_id=str(m.person.id),
            first_name=m.person.first_name,
            last_name=m.person.last_name,
            birth_date=m.person.birth_date,
            confidence=round(m.confidence, 2),
            match_reasons=m.match_reasons[:3],
        )

    ordered = sorted(best_per_tree.values(), key=lambda x: x.confidence, reverse=True)
    return ordered[:5]


async def _family_context_boost(
    db: AsyncSession,
    person: Person,
    parent_names: Optional[List[str]],
    sibling_names: Optional[List[str]],
) -> float:
    """
    Returns 0.0-1.0 boost score if the person's known relatives match
    the provided parent/sibling names.
    """
    if not parent_names and not sibling_names:
        return 0.0

    # Get all relationships for this person
    rels_result = await db.execute(
        select(Relationship).where(
            or_(
                Relationship.person_a_id == person.id,
                Relationship.person_b_id == person.id,
            )
        )
    )
    rels = rels_result.scalars().all()
    if not rels:
        return 0.0

    # Collect related person IDs by type
    related_ids_by_type: Dict[str, List] = {
        "parent": [], "sibling": [], "child": [], "spouse": [], "uncle_aunt": [],
    }
    for r in rels:
        if r.person_a_id == person.id:
            related_ids_by_type.setdefault(r.type, []).append(r.person_b_id)
        else:
            rtype = r.type
            if rtype == "parent":
                rtype = "child"
            elif rtype == "child":
                rtype = "parent"
            elif rtype == "nephew_niece":
                rtype = "uncle_aunt"
            elif rtype == "uncle_aunt":
                rtype = "nephew_niece"
            related_ids_by_type.setdefault(rtype, []).append(r.person_a_id)

    parent_ids = related_ids_by_type["parent"]
    sibling_ids = related_ids_by_type["sibling"]

    # Expansion oncles/tantes : leurs conjoints sont aussi oncles/tantes
    uncle_aunt_ids_direct = set(related_ids_by_type.get("uncle_aunt", []))
    # Fetch spouses of uncles/aunts (need a second query)
    if uncle_aunt_ids_direct:
        ua_rels_result = await db.execute(
            select(Relationship).where(
                Relationship.type == "spouse",
                or_(
                    Relationship.person_a_id.in_(uncle_aunt_ids_direct),
                    Relationship.person_b_id.in_(uncle_aunt_ids_direct),
                ),
            )
        )
        for ua_rel in ua_rels_result.scalars().all():
            if ua_rel.person_a_id in uncle_aunt_ids_direct:
                uncle_aunt_ids_direct.add(ua_rel.person_b_id)
            else:
                uncle_aunt_ids_direct.add(ua_rel.person_a_id)
    uncle_aunt_ids = list(uncle_aunt_ids_direct)

    related_ids = list(set(parent_ids + sibling_ids + uncle_aunt_ids))
    if not related_ids:
        return 0.0

    related_result = await db.execute(
        select(Person).where(Person.id.in_(related_ids))
    )
    related_persons = related_result.scalars().all()

    total_queries = 0
    matched_queries = 0

    if parent_names:
        for qname in parent_names:
            total_queries += 1
            for rp in related_persons:
                if rp.id in parent_ids:
                    s, _ = compute_name_score(qname, rp.first_name, rp.last_name, rp.nicknames)
                    if s > 0.6:
                        matched_queries += 1
                        break

    if sibling_names:
        for qname in sibling_names:
            total_queries += 1
            for rp in related_persons:
                if rp.id in sibling_ids:
                    s, _ = compute_name_score(qname, rp.first_name, rp.last_name, rp.nicknames)
                    if s > 0.6:
                        matched_queries += 1
                        break

    if total_queries == 0:
        return 0.0
    return matched_queries / total_queries
