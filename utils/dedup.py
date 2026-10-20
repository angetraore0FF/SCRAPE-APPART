"""Déduplication : même URL, OU titre normalisé similaire (>= 0.9) ET prix à moins de 10 € d'écart."""
from __future__ import annotations

import logging
import re
import unicodedata

from rapidfuzz import fuzz

log = logging.getLogger("dedup")
SEUIL_SIMILARITE = 90.0  # rapidfuzz.fuzz.ratio est sur 0-100 (équivaut à 0.9)
ECART_PRIX_MAX = 10.0


def normaliser_titre(titre: str) -> str:
    """Minuscules, sans accents, sans ponctuation, espaces compactés."""
    t = unicodedata.normalize("NFKD", (titre or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def est_doublon(a: dict, b: dict) -> bool:
    if a["url"] and a["url"] == b["url"]:
        return True
    if abs(a["prix_mensuel"] - b["prix_mensuel"]) >= ECART_PRIX_MAX:
        return False
    return fuzz.ratio(normaliser_titre(a["titre"]), normaliser_titre(b["titre"])) >= SEUIL_SIMILARITE


def dedupliquer(annonces: list[dict]) -> list[dict]:
    """Garde la première occurrence de chaque annonce (ordre d'entrée conservé)."""
    uniques: list[dict] = []
    for a in annonces:
        if any(est_doublon(a, u) for u in uniques):
            continue
        uniques.append(a)
    log.info("Dédoublonnage : %d -> %d annonces", len(annonces), len(uniques))
    return uniques
