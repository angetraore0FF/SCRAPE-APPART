"""Classe de base des scrapers + utilitaires communs (robots.txt, retry/backoff, parsing)."""
from __future__ import annotations

import logging
import random
import re
import time
import unicodedata
import urllib.robotparser
from abc import ABC, abstractmethod
from collections import Counter
from urllib.parse import urlparse

import requests

log = logging.getLogger("scraper")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DELAI_MIN, DELAI_MAX = 2.0, 5.0  # secondes entre deux requêtes
MAX_TENTATIVES = 3
STATUTS_RETRY = {403, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- parsing pur
def parse_prix(texte: str | None) -> float | None:
    """'1 010 €' -> 1010.0 ; None si aucun prix."""
    if not texte:
        return None
    m = re.search(r"(\d[\d\s  .,]*)\s*€", texte) or re.search(r"€\s*(\d[\d\s.,]*)", texte)
    if not m:
        return None
    try:
        return float(re.sub(r"[\s  ]", "", m.group(1)).replace(",", "."))
    except ValueError:
        return None


def parse_surface(texte: str | None) -> float | None:
    """'21-21 m²' / '75m2' -> première valeur numérique en m²."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:-\s*\d+(?:[.,]\d+)?\s*)?m(?:²|2)", texte or "")
    return float(m.group(1).replace(",", ".")) if m else None


def normaliser_type(texte: str | None) -> str:
    """Vocabulaire de chaque site -> 'Colocation' | 'Studio' | 'Chambre' | 'Cohabitation intergénérationnelle'."""
    t = (texte or "").lower()
    if re.search(r"intergén|intergen|cohabitation", t):
        return "Cohabitation intergénérationnelle"
    if re.search(r"coloc|coliving|co-living", t):
        return "Colocation"
    if re.search(r"chambre|habitant|room", t):
        return "Chambre"
    return "Studio"  # studio, T1, appartement, résidence étudiante...


def normaliser_nom(texte: str | None) -> str:
    """Minuscules, sans accents, tirets/apostrophes -> espaces : comparaison robuste de noms de villes."""
    t = unicodedata.normalize("NFKD", (texte or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


def slugifier(ville: str) -> str:
    """'Issy-les-Moulineaux' -> 'issy-les-moulineaux', 'Sèvres' -> 'sevres' (slug d'URL)."""
    return normaliser_nom(ville).replace(" ", "-")


def extraire_id(url: str) -> str:
    """Identifiant externe = dernier segment significatif de l'URL."""
    chemin = urlparse(url).path.rstrip("/")
    return chemin.split("/")[-1] or url


def nouvelle_annonce(source: str, **kw) -> dict:
    """Annonce au format normalisé commun."""
    a = {"id_externe": "", "titre": "", "type_logement": "Studio", "prix_mensuel": 0.0,
         "charges_incluses": None, "adresse_ou_ville": "", "url": "", "surface_m2": None,
         "note": None, "description": "", "source": source, "apl": None, "image_url": None,
         "ville_recherche": ""}
    a.update(kw)
    if not a["id_externe"] and a["url"]:
        a["id_externe"] = extraire_id(a["url"])
    return a


# --------------------------------------------------------------------------- réseau
class RobotsChecker:
    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def autorise(self, url: str) -> bool:
        p = urlparse(url)
        origine = f"{p.scheme}://{p.netloc}"
        if origine not in self._cache:
            rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                r = requests.get(origine + "/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=15)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException as e:
                log.warning("robots.txt illisible pour %s (%s) : on suppose autorisé", origine, e)
                rp = None
            self._cache[origine] = rp
        rp = self._cache[origine]
        return True if rp is None else rp.can_fetch("*", url)


ROBOTS = RobotsChecker()


def pause_polie() -> None:
    time.sleep(random.uniform(DELAI_MIN, DELAI_MAX))


class BaseScraper(ABC):
    """Interface commune : `fetch_listings()` ne lève jamais vers l'orchestrateur via `executer()`."""

    nom = "base"

    def __init__(self) -> None:
        self.rejets: Counter[str] = Counter()  # raison -> nombre d'annonces rejetées
        self.trouvees = 0

    @abstractmethod
    def fetch_listings(self) -> list[dict]:
        """Retourne les annonces normalisées (voir `nouvelle_annonce`)."""

    def rejeter(self, raison: str) -> None:
        self.rejets[raison] += 1

    def bilan_ville(self, ville: str, trouvees: int, rejets: dict[str, int] | None = None) -> None:
        """Log par ville de recherche : annonces trouvées, rejetées et pourquoi ; alimente aussi le bilan global."""
        rejets = rejets or {}
        for raison, n in rejets.items():
            self.rejets[raison] += n
        total = sum(rejets.values())
        detail = (" (" + ", ".join(f"{k}: {v}" for k, v in rejets.items()) + ")") if total else ""
        log.info("[%s] %s : %d annonces trouvées, %d rejetées%s", self.nom, ville, trouvees, total, detail)

    def bilan(self) -> None:
        total_rej = sum(self.rejets.values())
        log.info("[%s] %d annonces trouvées, %d rejetées%s", self.nom, self.trouvees, total_rej,
                 (" (" + ", ".join(f"{k}: {v}" for k, v in self.rejets.items()) + ")") if total_rej else "")

    def get(self, url: str) -> requests.Response | None:
        """GET avec robots.txt, User-Agent réaliste et backoff exponentiel (3 tentatives max)."""
        if not ROBOTS.autorise(url):
            log.warning("[%s] interdit par robots.txt : %s", self.nom, url)
            return None
        for essai in range(MAX_TENTATIVES):
            try:
                r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"},
                                 timeout=30)
                if r.status_code in STATUTS_RETRY:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                return r
            except requests.RequestException as e:
                attente = (2 ** essai) * random.uniform(2, 4)
                log.warning("[%s] GET %s échec (%s), tentative %d/%d", self.nom, url, e, essai + 1, MAX_TENTATIVES)
                if essai < MAX_TENTATIVES - 1:
                    time.sleep(attente)
        log.error("[%s] abandon de %s après %d tentatives", self.nom, url, MAX_TENTATIVES)
        return None
