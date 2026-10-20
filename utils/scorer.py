"""Notation et classement des offres.

score = w_prix * (1 - prix_norm) + w_distance * (1 - dist_norm) + w_confort * confort_norm + w_apl * bonus_apl

- prix_norm, dist_norm : min-max sur l'ensemble des offres filtrées (0 = meilleur). La distance utilisée
  est le temps de trajet en minutes.
- confort_norm : moyenne pondérée (surface 0.6 / note 0.4) des valeurs normalisées ; une donnée absente
  vaut 0.5 (neutre). +0.1 si charges comprises (plafonné à 1.0).
- bonus_apl : 1.0 si éligible, 0.5 si non précisé, 0.0 si explicitement non éligible.
"""
from __future__ import annotations

POIDS_SURFACE = 0.6
POIDS_NOTE = 0.4
NEUTRE = 0.5


def _minmax(v: float, lo: float, hi: float) -> float:
    return 0.0 if hi == lo else (v - lo) / (hi - lo)


def bonus_apl(apl: bool | None) -> float:
    return 1.0 if apl is True else 0.0 if apl is False else 0.5


def noter(annonces: list[dict], poids: dict[str, float]) -> list[dict]:
    """Ajoute le champ `score` à chaque annonce et renvoie la liste triée par score décroissant."""
    if not annonces:
        return []
    prix = [a["prix_mensuel"] for a in annonces]
    temps = [a["temps_trajet_min"] for a in annonces if a.get("temps_trajet_min") is not None]
    surfaces = [a["surface_m2"] for a in annonces if a.get("surface_m2")]
    notes = [a["note"] for a in annonces if a.get("note") is not None]
    p_lo, p_hi = min(prix), max(prix)
    t_lo, t_hi = (min(temps), max(temps)) if temps else (0, 0)
    s_lo, s_hi = (min(surfaces), max(surfaces)) if surfaces else (0, 0)
    n_lo, n_hi = (min(notes), max(notes)) if notes else (0, 0)
    for a in annonces:
        prix_norm = _minmax(a["prix_mensuel"], p_lo, p_hi)
        dist_norm = _minmax(a["temps_trajet_min"], t_lo, t_hi) if a.get("temps_trajet_min") is not None else NEUTRE
        surf = _minmax(a["surface_m2"], s_lo, s_hi) if a.get("surface_m2") else NEUTRE
        note = _minmax(a["note"], n_lo, n_hi) if a.get("note") is not None else NEUTRE
        confort = POIDS_SURFACE * surf + POIDS_NOTE * note
        if a.get("charges_incluses") is True:
            confort = min(1.0, confort + 0.1)
        a["score"] = round(
            poids["w_prix"] * (1 - prix_norm) + poids["w_distance"] * (1 - dist_norm)
            + poids["w_confort"] * confort + poids["w_apl"] * bonus_apl(a.get("apl")), 4)
    return sorted(annonces, key=lambda x: x["score"], reverse=True)
