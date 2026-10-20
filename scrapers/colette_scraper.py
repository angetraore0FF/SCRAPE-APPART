"""Scraper colette-cohabitation.com (cohabitation intergénérationnelle) : Playwright + session authentifiée.

Tout le contenu est derrière une inscription : la session vient de storage_state_colette.json (créé par
`python export_session.py --site colette`).

Structure vérifiée sur une session réelle : la page d'accueil authentifiée contient un formulaire « Où / Quand /
Budget / Rechercher » (séjour d'au moins 30 jours obligatoire). La recherche mène à
`/accommodations?location=<ville|placeId>&lat&lng&startDate&endDate&minBudget&maxBudget`, qui liste des cartes
(lien `/accommodations/<uuid>`) du type « Villejuif, chambre chez Jean D. · Villejuif · APL/ALS · 680 €/mois ».
Le scraper se contente de LIRE : il ne clique jamais sur « contacter » ni n'envoie de demande.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from utils.apl_detector import detect_apl

from .base_scraper import (ROBOTS, USER_AGENT, BaseScraper, nouvelle_annonce, parse_surface, pause_polie,
                           slugifier)

log = logging.getLogger("scraper.colette")

# --- Sélecteurs / constantes (à ajuster si le site change) ---
URL_ACCUEIL = "https://www.colette-cohabitation.com/"
TYPE_LOGEMENT = "Cohabitation intergénérationnelle"
RE_HREF_ANNONCE = re.compile(r"^/accommodations/[0-9a-f][0-9a-f-]{7,}", re.I)
RE_LIEN_ANNONCES = re.compile(r"rechercher|annonces?|logements?|se loger|trouver|chambres?", re.I)  # repli
RE_PRIX_MOT = re.compile(r"(?:participation|contribution|loyer|prix)\D{0,25}?(\d[\d\s]*)\s*€", re.I)
RE_PRIX = re.compile(r"(\d[\d\s]*)\s*€")
RE_CP = re.compile(r"\((\d{5})\)")
RE_PAGES = re.compile(r"page\s+(\d+)\s*/\s*(\d+)", re.I)
MARQUEURS_LOGIN = ("login", "signin", "signup", "connexion", "inscription", "sso")
SEL_FORM_LOGIN = "input[type='password']"
TEXTE_CHAMP_OU = "Recherche une adresse"
SEL_IMAGE = "img"
DUREE_SEJOUR_JOURS = 31  # le site exige au moins 30 jours
TIMEOUT_MS = 20000
MAX_PAGES = 5
MESSAGE_SESSION = "Session Colette expirée. Veuillez exécuter 'python export_session.py --site colette'."
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
        "novembre", "décembre"]


class SessionExpiree(Exception):
    pass


def label_jour(d: date) -> str:
    """Fonction PURE : libellé aria du bouton calendrier, ex. 'jeudi 15 octobre 2026'."""
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def trouver_lien_annonces(html: str) -> str | None:
    """Fonction PURE (repli) : href du premier lien de navigation menant aux annonces, ou None."""
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        if RE_LIEN_ANNONCES.search(a.get_text(" ", strip=True)) and not re.search(r"login|signup|logout|blog|help", a["href"]):
            return a["href"]
    return None


def extraire_prix(texte: str) -> float | None:
    """Fonction PURE : prix mensuel, en privilégiant « participation » / « contribution » / « loyer »."""
    m = RE_PRIX_MOT.search(texte) or RE_PRIX.search(texte)
    return float(re.sub(r"\s", "", m.group(1))) if m else None


def _ville_depuis_titre(titre: str) -> str:
    """« Montreuil (93100), chambre chez X » -> « Montreuil, 93100 » ; « Paris 15e, chambre 1 ... » -> « Paris 15e »."""
    debut = titre.split(",")[0].strip()
    cp = RE_CP.search(debut)
    return f"{RE_CP.sub('', debut).strip()}, {cp.group(1)}" if cp else debut


def parse_annonces(html: str, base_url: str = URL_ACCUEIL) -> tuple[list[dict], dict[str, int]]:
    """Fonction PURE : HTML d'une page de résultats -> (annonces normalisées, rejets par raison)."""
    soup = BeautifulSoup(html, "lxml")
    annonces: list[dict] = []
    rejets: dict[str, int] = {}
    vus: set[str] = set()
    for lien in soup.find_all("a", href=RE_HREF_ANNONCE):
        url = urljoin(base_url, lien["href"].split("?")[0])  # sans les dates de recherche : URL stable
        if url in vus:
            continue
        vus.add(url)
        lignes = [x.strip() for x in lien.get_text("\n").splitlines() if x.strip()]
        texte = re.sub(r"\s+", " ", lien.get_text(" ")).strip()
        prix = extraire_prix(texte)
        if prix is None:
            rejets["prix illisible"] = rejets.get("prix illisible", 0) + 1
            continue
        titre = next((x for x in lignes if "," in x and "€" not in x), next((x for x in lignes if "€" not in x and len(x) > 3), texte[:80]))
        img = lien.select_one(SEL_IMAGE)
        annonces.append(nouvelle_annonce(
            "Colette", titre=titre, type_logement=TYPE_LOGEMENT, prix_mensuel=prix, charges_incluses=None,
            adresse_ou_ville=_ville_depuis_titre(titre), url=url, surface_m2=parse_surface(texte),
            description=texte, apl=detect_apl(titre, texte), image_url=img.get("src") if img else None))
    return annonces, rejets


def nb_pages(html: str) -> int:
    """Fonction PURE : nombre total de pages de résultats (« page 1 / 3 »), 1 si absent."""
    m = RE_PAGES.search(re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" ")))
    return int(m.group(2)) if m else 1


class ColetteScraper(BaseScraper):
    nom = "Colette"

    def __init__(self, storage_state: str = "storage_state_colette.json", villes: list[str] | None = None,
                 headless: bool = True, max_pages: int = MAX_PAGES) -> None:
        super().__init__()
        self.storage_state, self.headless, self.max_pages = storage_state, headless, max_pages
        self.villes = villes or ["Paris"]

    def fetch_listings(self) -> list[dict]:
        if not Path(self.storage_state).exists():
            log.warning("[Colette] %s introuvable. %s", self.storage_state, MESSAGE_SESSION)
            return []
        if not ROBOTS.autorise(URL_ACCUEIL):
            log.warning("[Colette] robots.txt interdit %s", URL_ACCUEIL)
            return []
        resultats: list[dict] = []
        vus: set[str] = set()
        with sync_playwright() as p:
            navigateur = p.chromium.launch(headless=self.headless)
            try:
                ctx = navigateur.new_context(storage_state=self.storage_state, user_agent=USER_AGENT, locale="fr-FR",
                                             viewport={"width": 1400, "height": 1000})
                for ville in self.villes:
                    page = ctx.new_page()
                    try:  # une ville en échec : capture, warning, ville suivante
                        nouvelles, rejets = self._scraper_ville(page, ville, vus)
                    except SessionExpiree:
                        log.error("[Colette] %s", MESSAGE_SESSION)  # arrêt propre, l'orchestrateur continue
                        break
                    except Exception as e:
                        self._debug(page, ville)
                        log.warning("[Colette] %s ignorée : %s", ville, e)
                        continue
                    finally:
                        page.close()
                    resultats.extend(nouvelles)
                    self.bilan_ville(ville, len(nouvelles), rejets)
            finally:
                navigateur.close()
        self.trouvees = len(resultats)
        return resultats

    def _scraper_ville(self, page, ville: str, vus: set[str]) -> tuple[list[dict], dict[str, int]]:
        self._rechercher(page, ville)
        log.info("[Colette] page des annonces atteinte (%s) : %s", ville, page.url.split("&")[0])
        out: list[dict] = []
        rejets_ville: dict[str, int] = {}
        total = 1
        for n in range(1, self.max_pages + 1):
            try:
                page.wait_for_selector("a[href^='/accommodations/']", timeout=TIMEOUT_MS)
            except PWTimeout:
                if n == 1:
                    log.warning("[Colette] %s : aucune chambre affichée", ville)
                break
            html = page.content()
            total = nb_pages(html)
            annonces, rejets = parse_annonces(html, page.url)
            for k, v in rejets.items():
                rejets_ville[k] = rejets_ville.get(k, 0) + v
            nouvelles = [a for a in annonces if a["url"] not in vus]
            vus.update(a["url"] for a in nouvelles)
            for a in nouvelles:
                a["ville_recherche"] = ville
            out.extend(nouvelles)
            if n >= total or not nouvelles:
                break
            pause_polie()  # page suivante : paramètre d'URL `page` (non vérifié : un seul résultat de 16 chambres jusqu'ici)
            sep = "&" if "?" in page.url else "?"
            page.goto(re.sub(r"[?&]page=\d+", "", page.url) + f"{sep}page={n + 1}", wait_until="domcontentloaded", timeout=45000)
        if total > self.max_pages:
            log.warning("[Colette] %s : %d pages de résultats, %d lues seulement", ville, total, self.max_pages)
        return out, rejets_ville

    def _rechercher(self, page, ville: str) -> None:
        """Accueil -> Où (suggestion) + Quand (>= 30 jours) -> Rechercher ; l'URL atteinte est journalisée."""
        page.goto(URL_ACCUEIL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        self._verifier_session(page)
        page.get_by_text(TEXTE_CHAMP_OU).first.click()
        page.wait_for_timeout(800)
        page.keyboard.type(ville, delay=70)
        page.wait_for_timeout(2500)
        suggestion = page.get_by_text(re.compile(rf"^{re.escape(ville)}\b", re.I)).last
        if not suggestion.count():
            raise RuntimeError(f"aucune suggestion d'adresse pour « {ville} »")
        suggestion.click()
        page.wait_for_timeout(1000)
        debut = date.today() + timedelta(days=1)
        for jour in (debut, debut + timedelta(days=DUREE_SEJOUR_JOURS)):
            bouton = page.locator(f'button[aria-label="{label_jour(jour)}"]').first
            if not bouton.count():  # mois suivant non affiché : on avance le calendrier
                page.get_by_role("button", name=re.compile(r"suivant|next|mois", re.I)).last.click()
                page.wait_for_timeout(500)
            bouton.click()
            page.wait_for_timeout(500)
        page.get_by_role("button", name=re.compile("Rechercher")).first.click()
        try:
            page.wait_for_url(re.compile(r"/accommodations\?"), timeout=TIMEOUT_MS, wait_until="commit")
        except PWTimeout:
            self._verifier_session(page)
            raise RuntimeError(f"la recherche « {ville} » n'a pas abouti (URL : {page.url})")

    @staticmethod
    def _verifier_session(page) -> None:
        if any(m in page.url.lower() for m in MARQUEURS_LOGIN) or page.locator(SEL_FORM_LOGIN).count() > 0:
            raise SessionExpiree(MESSAGE_SESSION)

    @staticmethod
    def _debug(page, ville: str) -> None:
        try:
            page.screenshot(path=f"colette_debug_{slugifier(ville)}.png", full_page=True)
        except Exception as e:
            log.debug("capture impossible : %s", e)
