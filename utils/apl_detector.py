"""Détection de l'éligibilité APL/CAF par mots-clés (titre + description).

Retourne True (mention positive), False (mention explicitement négative) ou None (non précisé).
Jamais False par défaut en l'absence de mention.
"""
from __future__ import annotations

import re

POSITIVE = [
    r"éligible\s+(?:à\s+l[' ]?)?(?:caf|apl)", r"eligible\s+(?:caf|apl)", r"logement\s+conventionn[ée]",
    r"conventionn[ée]\s+apl", r"aides?\s+au\s+logement", r"apl\s+possible", r"ouvre\s+droit\s+(?:aux\s+aides|à\s+l[' ]?apl)",
    r"(?<!\w)apl(?!\w)",
]
NEGATIVE = [
    r"non\s+(?:éligible|eligible)\s+(?:à\s+l[' ]?)?(?:caf|apl)", r"pas\s+(?:éligible|eligible)\s+(?:à\s+l[' ]?)?(?:caf|apl)",
    r"(?:sans|pas\s+d[e'])\s*apl", r"non\s+conventionn[ée]", r"apl\s+non\s+possible", r"n[' ]ouvre\s+pas\s+droit",
]
_POS = [re.compile(p, re.I) for p in POSITIVE]
_NEG = [re.compile(p, re.I) for p in NEGATIVE]


def detect_apl(titre: str | None, description: str | None) -> bool | None:
    text = f"{titre or ''} {description or ''}"
    if any(r.search(text) for r in _NEG):  # le négatif prime : « non éligible APL » contient « APL »
        return False
    if any(r.search(text) for r in _POS):
        return True
    return None
