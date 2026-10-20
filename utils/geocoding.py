"""Géocodage (geopy + Nominatim) et trajet vers l'école : temps ET nombre de correspondances.

Sources du trajet, par ordre de priorité :
1. Google Directions API, mode `transit` (clé `api.google_transit_api_key` ou variable GOOGLE_TRANSIT_API_KEY) :
   durée totale + correspondances = étapes TRANSIT - 1 (les étapes de marche sont ignorées).
2. PRIM Île-de-France Mobilités (gratuit, variable PRIM_API_KEY) : même information, Île-de-France seulement.
3. OpenRouteService (durée seule, vélo) : pas de correspondances.
4. Estimation grossière depuis la distance à vol d'oiseau : pas de correspondances.
Sans source 1 ou 2, `correspondances` vaut None et `correspondances_verifiees` False : l'offre n'est jamais
rejetée pour cette seule raison.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime
from pathlib import Path

import requests
from geopy.exc import GeopyError
from geopy.geocoders import Nominatim

from .prim_idfm import PrimIdfm, prochain_jour_ouvre_9h

log = logging.getLogger("geocoding")
CACHE_FILE = Path("geocache.json")
Coord = tuple[float, float]
URL_DIRECTIONS = "https://maps.googleapis.com/maps/api/directions/json"


def haversine_km(a: Coord, b: Coord) -> float:
    """Distance orthodromique en km."""
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def estimer_minutes(km: float) -> int:
    """Estimation GROSSIÈRE en transports en commun : 10 min d'accès + ~4 min par km à vol d'oiseau."""
    return round(10 + 4 * km)


def extraire_directions(reponse: dict) -> dict | None:
    """Fonction PURE : réponse JSON Google Directions (mode transit) -> {minutes, correspondances}.

    correspondances = nombre d'étapes de transport en commun (bus, métro, train, tram) moins 1 ;
    les étapes WALKING sont ignorées. On garde l'itinéraire le plus rapide.
    """
    if reponse.get("status") != "OK" or not reponse.get("routes"):
        return None
    meilleur = None
    for route in reponse["routes"]:
        for leg in route.get("legs", []):
            etapes = leg.get("steps", [])
            transports = sum(1 for e in etapes if e.get("travel_mode") == "TRANSIT")
            duree = leg["duration"]["value"]
            if meilleur is None or duree < meilleur["secondes"]:
                meilleur = {"secondes": duree, "correspondances": max(transports - 1, 0)}
    if meilleur is None:
        return None
    return {"minutes": round(meilleur["secondes"] / 60), "correspondances": meilleur["correspondances"]}


def _resultat(km: float, minutes: int, estime: bool, corr: int | None, methode: str) -> dict:
    return {"distance_km": km, "temps_trajet_min": minutes, "temps_trajet_estime": estime,
            "correspondances": corr, "correspondances_verifiees": corr is not None, "methode_trajet": methode}


class Geocodeur:
    def __init__(self, ors_key: str | None = None, google_transit_key: str | None = None) -> None:
        self.geocoder = Nominatim(user_agent="recherche-logement-perso/2.0", timeout=15)
        # Clés : config.yaml sinon variables d'environnement (recommandé : la clé reste hors du projet)
        self.ors_key = ors_key or os.environ.get("ORS_API_KEY")
        self.google_key = google_transit_key or os.environ.get("GOOGLE_TRANSIT_API_KEY")
        self.prim = PrimIdfm()
        try:
            self.cache: dict[str, list[float] | None] = json.loads(CACHE_FILE.read_text(encoding="utf8"))
        except Exception:
            self.cache = {}

    def geocoder_adresse(self, adresse: str | None) -> Coord | None:
        if not adresse:
            return None
        if adresse in self.cache:
            v = self.cache[adresse]
            return (v[0], v[1]) if v else None
        res: Coord | None = None
        # 2e essai : on retire le premier segment (rue/nom) pour retomber sur la ville/code postal
        essais = [adresse] + ([", ".join(adresse.split(",")[1:]).strip()] if adresse.count(",") >= 1 else [])
        for q in essais:
            if not q:
                continue
            try:
                time.sleep(1.1)  # politique Nominatim : 1 requête/s
                loc = self.geocoder.geocode(q + ("" if "france" in q.lower() else ", France"))
                if loc:
                    res = (loc.latitude, loc.longitude)
                    break
            except GeopyError as e:
                log.warning("Géocodage échoué (%s) : %s", q, e)
        self.cache[adresse] = list(res) if res else None
        CACHE_FILE.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf8")
        return res

    def _google(self, origine: Coord, ecole: Coord) -> dict | None:
        arrivee = int(datetime.strptime(prochain_jour_ouvre_9h(), "%Y%m%dT%H%M%S").timestamp())
        try:
            r = requests.get(URL_DIRECTIONS, timeout=20, params={
                "origin": f"{origine[0]},{origine[1]}", "destination": f"{ecole[0]},{ecole[1]}",
                "mode": "transit", "arrival_time": arrivee, "alternatives": "true", "language": "fr",
                "key": self.google_key}).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("Google Directions injoignable : %s", e)
            return None
        if r.get("status") not in ("OK", "ZERO_RESULTS"):
            log.warning("Google Directions : %s %s", r.get("status"), r.get("error_message", ""))
        return extraire_directions(r)

    def trajet(self, origine: Coord, ecole: Coord) -> dict:
        """-> {distance_km, temps_trajet_min, temps_trajet_estime, correspondances, correspondances_verifiees, methode_trajet}"""
        km = haversine_km(origine, ecole)
        if self.google_key and (it := self._google(origine, ecole)):
            return _resultat(km, it["minutes"], False, it["correspondances"], "Google Directions (transports en commun)")
        if self.prim.disponible and (it := self.prim.itineraire(origine, ecole)):
            return _resultat(km, it["minutes"], False, it["correspondances"], "PRIM IDFM (transports en commun)")
        if self.ors_key:
            try:
                r = requests.post("https://api.openrouteservice.org/v2/directions/cycling-regular", timeout=20,
                                  headers={"Authorization": self.ors_key},
                                  json={"coordinates": [[origine[1], origine[0]], [ecole[1], ecole[0]]]}).json()
                return _resultat(km, round(r["routes"][0]["summary"]["duration"] / 60), True, None,
                                 "OpenRouteService (vélo, durée seule)")
            except Exception as e:
                log.warning("OpenRouteService échoué : %s", e)
        return _resultat(km, estimer_minutes(km), True, None, "Estimation approximative (vol d'oiseau)")
