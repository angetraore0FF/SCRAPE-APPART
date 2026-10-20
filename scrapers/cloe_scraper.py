"""Scraper cloe-edu.fr : rendu serveur Next.js -> requests + BeautifulSoup.

Constats vérifiés : les cartes de la liste contiennent type, prix, titre, adresse, surface mais pas de lien ;
les URLs viennent du JSON-LD (ItemList) de la page, dans l'ordre (10 par page). La pagination se fait avec
`?page=N` (au-delà de la dernière page le site renvoie la dernière : on s'arrête quand rien de neuf).
Les fiches détaillées répondaient 404 : le fallback est tenté une fois puis désactivé si indisponible.
"""
from __future__ import annotations

import hashlib
import json
import logging

from bs4 import BeautifulSoup

from utils.apl_detector import detect_apl

from .base_scraper import (BaseScraper, nouvelle_annonce, normaliser_type, parse_prix, parse_surface,
                           pause_polie, slugifier)

log = logging.getLogger("scraper.cloe")

# --- Sélecteurs CSS / constantes (à corriger ici si le site change) ---
URL_VILLE = "https://www.cloe-edu.fr/fr/logement-etudiant/{slug}"
SEL_TITRE_CARTE = "h3.text-sm"
SEL_PRIX = "span.font-extrabold"
SEL_BADGES = "span.rounded-md"
SEL_LOCALISATION = "svg + span"
SEL_IMAGE = "img"
SEL_JSONLD = "script[type='application/ld+json']"
SEL_DESCRIPTION_FICHE = "meta[name='description']"
MAX_PAGES = 40
DESCRIPTION_MIN = 120  # en dessous, on tente la fiche détaillée


def parse_liste(html: str, url_liste: str) -> tuple[list[dict], dict[str, int]]:
    """Fonction PURE : HTML d'une page de liste -> (annonces normalisées, rejets par raison)."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for sc in soup.select(SEL_JSONLD):
        try:
            d = json.loads(sc.string or "")
        except json.JSONDecodeError:
            continue
        if d.get("@type") == "ItemList":
            urls = [i.get("url", "") for i in d.get("itemListElement", [])]
    annonces: list[dict] = []
    rejets: dict[str, int] = {}
    idx_carte = 0
    for h3 in soup.select(SEL_TITRE_CARTE):
        carte = h3.parent
        prix_el = carte.select_one(SEL_PRIX)
        if prix_el is None:  # titre hors carte (pied de page...)
            continue
        i, idx_carte = idx_carte, idx_carte + 1
        prix = parse_prix(prix_el.get_text())
        if prix is None:
            rejets["prix illisible"] = rejets.get("prix illisible", 0) + 1
            continue
        titre = h3.get_text(" ", strip=True)
        loc = carte.select_one(SEL_LOCALISATION)
        adresse = loc.get_text(" ", strip=True).replace(" · ", ", ") if loc else ""
        badges = " ".join(b.get_text(strip=True) for b in carte.select(SEL_BADGES))
        texte = carte.get_text(" ", strip=True)
        img = (carte.parent or carte).select_one(SEL_IMAGE)
        url = urls[i] if i < len(urls) and urls[i] else ""
        if not url:  # pas de lien individuel : ancre unique (évite de fusionner à tort au dédoublonnage)
            url = f"{url_liste}#{hashlib.md5(f'{titre}{prix}{adresse}'.encode()).hexdigest()[:10]}"
        annonces.append(nouvelle_annonce(
            "Cloe", titre=titre, type_logement=normaliser_type(f"{titre} {badges}"), prix_mensuel=prix,
            charges_incluses=None, adresse_ou_ville=adresse, url=url, surface_m2=parse_surface(texte),
            description=texte, apl=detect_apl(titre, texte), image_url=img.get("src") if img else None))
    return annonces, rejets


class CloeScraper(BaseScraper):
    nom = "Cloe"

    def __init__(self, villes: list[str] | None = None, max_pages: int = MAX_PAGES) -> None:
        super().__init__()
        self.villes = villes or ["Paris"]  # noms de villes ; URL = /fr/logement-etudiant/{slug de la ville}
        self.max_pages = max_pages
        self._fiches_ok: bool | None = None  # None = pas encore testé

    def fetch_listings(self) -> list[dict]:
        resultats: list[dict] = []
        vus: set[tuple] = set()  # partagé entre villes : une annonce présente dans deux listes n'est gardée qu'une fois
        for ville in self.villes:
            try:
                nouvelles, rejets = self._scraper_ville(ville, vus)
            except Exception as e:  # une ville en échec n'arrête pas les suivantes
                log.warning("[Cloe] %s ignorée : %s", ville, e)
                continue
            if not nouvelles:
                log.warning("[Cloe] %s : ville introuvable (404) ou sans annonce", ville)
            self.bilan_ville(ville, len(nouvelles), rejets)
            resultats.extend(nouvelles)
        self.trouvees = len(resultats)
        return resultats

    def _scraper_ville(self, ville: str, vus: set[tuple]) -> tuple[list[dict], dict[str, int]]:
        base = URL_VILLE.format(slug=slugifier(ville))
        out: list[dict] = []
        rejets_ville: dict[str, int] = {}
        for page in range(1, self.max_pages + 1):
            url = base if page == 1 else f"{base}?page={page}"
            r = self.get(url)
            if r is None or r.status_code != 200:
                if page == 1:
                    log.warning("[Cloe] %s : HTTP %s", ville, r.status_code if r is not None else "erreur")
                break
            annonces, rejets = parse_liste(r.text, url)
            for k, v in rejets.items():
                rejets_ville[k] = rejets_ville.get(k, 0) + v
            nouvelles = [a for a in annonces if (a["titre"], a["prix_mensuel"], a["adresse_ou_ville"]) not in vus]
            if not nouvelles:
                break  # liste vide, ou fin de pagination (le site renvoie alors la dernière page)
            vus.update((a["titre"], a["prix_mensuel"], a["adresse_ou_ville"]) for a in nouvelles)
            for a in nouvelles:
                a["ville_recherche"] = ville
                self._completer_depuis_fiche(a)
            out.extend(nouvelles)
            pause_polie()
        return out, rejets_ville

    def _completer_depuis_fiche(self, a: dict) -> None:
        """Fallback : description complète depuis la fiche si la liste est trop pauvre (si la fiche existe)."""
        if self._fiches_ok is False or "#" in a["url"] or len(a["description"]) >= DESCRIPTION_MIN:
            return
        r = self.get(a["url"])
        if r is None or r.status_code != 200:
            if self._fiches_ok is None:
                log.warning("[Cloe] fiches détaillées indisponibles (HTTP %s) : fallback désactivé",
                            r.status_code if r else "erreur")
            self._fiches_ok = False
            return
        self._fiches_ok = True
        meta = BeautifulSoup(r.text, "lxml").select_one(SEL_DESCRIPTION_FICHE)
        if meta and meta.get("content"):
            a["description"] = meta["content"]
            a["apl"] = detect_apl(a["titre"], a["description"])
        pause_polie()
