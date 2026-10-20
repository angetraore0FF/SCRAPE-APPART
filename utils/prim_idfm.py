"""Itinéraires transports en commun Île-de-France via l'API PRIM d'Île-de-France Mobilités (Navitia).

Gratuit, clé personnelle lue dans la variable d'environnement PRIM_API_KEY (jamais dans un fichier du projet).
Renvoie le temps de trajet et le NOMBRE DE CORRESPONDANCES du meilleur itinéraire arrivant à l'école
un jour de semaine à 9h00, ce qui permet de filtrer les trajets « simples ».

Les sources publiques divergent sur l'URL exacte et l'en-tête d'authentification : on essaie les variantes
connues au premier appel et on retient celle qui répond (voir CANDIDATS).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import requests

log = logging.getLogger("prim")
ENV_CLE = "PRIM_API_KEY"
HEURE_ARRIVEE = 9  # arrivée à l'école à 9h00
# (url, nom de l'en-tête) à tester dans l'ordre
CANDIDATS = [
    ("https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia/journeys", "apiKey"),
    ("https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia/journeys", "Authorization"),
    ("https://prim.iledefrance-mobilites.fr/marketplace/v2/journeys", "apiKey"),
    ("https://prim.iledefrance-mobilites.fr/marketplace/v2/journeys", "Authorization"),
]


def prochain_jour_ouvre_9h(maintenant: datetime | None = None) -> str:
    """Prochain jour de semaine (lundi-vendredi) à 9h00, au format Navitia YYYYMMDDTHHMMSS."""
    d = (maintenant or datetime.now()) + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=HEURE_ARRIVEE, minute=0, second=0, microsecond=0).strftime("%Y%m%dT%H%M%S")


def extraire_itineraire(reponse: dict) -> dict | None:
    """Fonction PURE : réponse JSON Navitia -> {minutes, correspondances} du meilleur itinéraire, ou None."""
    trajets = reponse.get("journeys") or []
    if not trajets:
        return None
    j = min(trajets, key=lambda x: x.get("duration", 10 ** 9))
    return {"minutes": round(j["duration"] / 60), "correspondances": int(j.get("nb_transfers", 0))}


class PrimIdfm:
    def __init__(self, cle: str | None = None) -> None:
        self.cle = cle or os.environ.get(ENV_CLE)
        self._candidat: tuple[str, str] | None = None  # variante qui a fonctionné

    @property
    def disponible(self) -> bool:
        return bool(self.cle)

    def itineraire(self, origine: tuple[float, float], ecole: tuple[float, float]) -> dict | None:
        """origine/ecole = (lat, lon). None si pas de clé, hors couverture ou erreur."""
        if not self.cle:
            return None
        params = {"from": f"{origine[1]};{origine[0]}", "to": f"{ecole[1]};{ecole[0]}",
                  "datetime": prochain_jour_ouvre_9h(), "datetime_represents": "arrival", "count": 3}
        for url, entete in ([self._candidat] if self._candidat else CANDIDATS):
            try:
                r = requests.get(url, params=params, headers={entete: self.cle}, timeout=30)
            except requests.RequestException as e:
                log.warning("PRIM : requête échouée (%s)", e)
                return None
            if r.status_code in (401, 403, 404) and not self._candidat:
                continue  # variante d'URL/en-tête incorrecte : on essaie la suivante
            if r.status_code != 200:
                log.warning("PRIM : HTTP %s (%s)", r.status_code, r.text[:120].replace("\n", " "))
                return None
            self._candidat = (url, entete)
            return extraire_itineraire(r.json())
        log.error("PRIM : clé refusée ou URL introuvable (vérifie PRIM_API_KEY et l'abonnement à l'API Calculateur)")
        return None
