"""Scraper spotahome.com : Playwright.

Le pattern d'URL de recherche n'est pas codé en dur : on part de la page d'accueil, on saisit « Paris » dans
le champ de recherche, on valide, puis on lit `page.url` pour obtenir l'URL réelle (vérifié : /fr/s/paris--france).
La pagination suit le lien « Suivant » de chaque page. robots.txt interdit ?budget / ?poi : aucun filtre
n'est passé par l'URL, le filtre de prix est fait ensuite par l'orchestrateur.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from utils.apl_detector import detect_apl

from .base_scraper import (ROBOTS, USER_AGENT, BaseScraper, nouvelle_annonce, normaliser_type, parse_prix,
                           parse_surface, pause_polie, slugifier)

log = logging.getLogger("scraper.spotahome")

# --- Sélecteurs CSS / constantes ---
URL_ACCUEIL = "https://www.spotahome.com/fr"
SEL_CHAMP_RECHERCHE = '[data-test="city-selector-autocomplete"]'
SEL_SUGGESTION = "[role='option'], [data-test*='suggestion'], [data-test*='autocomplete'] li"
SEL_CARTE = "[data-homecard-scroll]"
SEL_LIEN = "a[data-test='homecard-link']"
SEL_INFOS = "a[data-test='homecard-link'] span"
SEL_TITRE = "a[data-test='homecard-link'] p"
SEL_CONTENU = "[data-test='homecard-content']"
SEL_OVERLAY = "[data-test='homecard-description-overlay']"
SEL_COMMODITES = "[data-test='homecard-description-overlay'] span[role='img']"
SEL_IMAGE = "[data-test='homecard-image'] img"
SEL_SUIVANT = "a:has-text('Suivant')"
BOUTON_COOKIES_REFUS = re.compile(r"refuser", re.I)  # option la plus protectrice de la vie privée
TIMEOUT_CARTE_MS = 15000
MAX_PAGES = 15
MESSAGE_VILLE_ABSENTE = re.compile(r"pas encore pr[ée]sent", re.I)


class SpotahomeIndisponible(Exception):
    pass


def parse_cartes(html: str, ville: str = "Paris") -> tuple[list[dict], dict[str, int]]:
    """Fonction PURE : HTML d'une page de résultats -> (annonces normalisées, rejets par raison)."""
    soup = BeautifulSoup(html, "lxml")
    annonces: list[dict] = []
    rejets: dict[str, int] = {}

    def rejet(raison: str) -> None:
        rejets[raison] = rejets.get(raison, 0) + 1

    for carte in soup.select(SEL_CARTE):
        lien = carte.select_one(SEL_LIEN)
        if lien is None or not lien.get("href"):
            rejet("lien manquant")
            continue
        titre_el = carte.select_one(SEL_TITRE)
        titre = titre_el.get_text(" ", strip=True) if titre_el else ""
        contenu = carte.select_one(SEL_CONTENU)
        prix = parse_prix(contenu.get_text(" ", strip=True) if contenu else "")
        if prix is None:
            rejet("prix illisible")
            continue
        infos = [s.get_text(strip=True) for s in carte.select(SEL_INFOS)]
        note = next((float(m.group(1)) for s in infos if (m := re.match(r"(\d(?:\.\d)?)\s*\(", s))), None)
        overlay = carte.select_one(SEL_OVERLAY)
        description = overlay.get_text(" ", strip=True) if overlay else ""
        commodites = [(s.get("aria-label") or "") for s in carte.select(SEL_COMMODITES)]
        queue = titre.rsplit(" à ", 1)[-1].strip() if " à " in titre else ""
        adresse = queue if ville.lower() in queue.lower() else (f"{queue}, {ville}" if queue else ville)
        img = carte.select_one(SEL_IMAGE)
        annonces.append(nouvelle_annonce(
            "Spotahome", titre=titre, type_logement=normaliser_type(f"{infos[0] if infos else ''} {titre}"),
            prix_mensuel=prix, charges_incluses=None, adresse_ou_ville=adresse,
            url=urljoin(URL_ACCUEIL, lien["href"]), surface_m2=parse_surface(f"{titre} {description}"),
            note=note, description=f"{description} {' '.join(commodites)}".strip(), apl=detect_apl(titre, description),
            image_url=(img.get("src") or img.get("data-src")) if img else None))
    return annonces, rejets


class SpotahomeScraper(BaseScraper):
    nom = "Spotahome"

    def __init__(self, villes: list[str] | None = None, max_pages: int = MAX_PAGES, headless: bool = True) -> None:
        super().__init__()
        self.villes = villes or ["Paris"]
        self.max_pages, self.headless = max_pages, headless

    def fetch_listings(self) -> list[dict]:
        if not ROBOTS.autorise(URL_ACCUEIL):
            raise SpotahomeIndisponible("robots.txt interdit l'accès (accueil)")
        resultats: list[dict] = []
        vus: set[str] = set()
        with sync_playwright() as p:
            navigateur = p.chromium.launch(headless=self.headless)
            try:
                contexte = navigateur.new_context(user_agent=USER_AGENT, locale="fr-FR")
                for ville in self.villes:
                    page = contexte.new_page()  # page neuve par ville : pas d'état résiduel de la recherche précédente
                    try:  # un échec sur une ville : capture, warning, ville suivante
                        nouvelles, rejets = self._scraper_ville(page, ville, vus)
                    except Exception as e:
                        self._debug(page, ville)
                        log.warning("[Spotahome] %s ignorée : %s", ville, e)
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
        out: list[dict] = []
        rejets_ville: dict[str, int] = {}
        for n in range(1, self.max_pages + 1):
            self._attendre_cartes(page)
            annonces, rejets = parse_cartes(page.content(), ville)
            for k, v in rejets.items():
                rejets_ville[k] = rejets_ville.get(k, 0) + v
            nouvelles = [a for a in annonces if a["url"] not in vus]
            if not nouvelles:
                break
            vus.update(a["url"] for a in nouvelles)
            for a in nouvelles:
                a["ville_recherche"] = ville
            out.extend(nouvelles)
            suivant = page.locator(SEL_SUIVANT).first
            href = suivant.get_attribute("href") if suivant.count() else None
            if not href:
                break
            pause_polie()
            page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=45000)
        return out, rejets_ville

    def _rechercher(self, page, ville: str) -> None:
        """Accueil -> saisie de la ville -> validation ; l'URL résultante est lue, pas supposée."""
        page.goto(URL_ACCUEIL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.get_by_role("button", name=BOUTON_COOKIES_REFUS).first.click(timeout=4000)
        except PWTimeout:
            pass
        champ = page.locator(SEL_CHAMP_RECHERCHE).first
        champ.click()
        champ.fill(ville)
        page.wait_for_timeout(2000)
        if MESSAGE_VILLE_ABSENTE.search(page.inner_text("body")):
            raise SpotahomeIndisponible(f"Spotahome n'est pas présent à {ville}")
        champ.press("Enter")
        try:  # "commit" : on n'attend pas le chargement complet de la page, seulement le changement d'URL
            page.wait_for_url(re.compile(r"/s/"), timeout=15000, wait_until="commit")
        except PWTimeout:
            try:  # 2e essai : première suggestion de l'autocomplétion
                page.locator(SEL_SUGGESTION).first.click(timeout=3000)
                page.wait_for_url(re.compile(r"/s/"), timeout=15000, wait_until="commit")
            except PWTimeout:
                raise SpotahomeIndisponible(f"la recherche « {ville} » n'a pas abouti (URL : {page.url})")
        log.info("[Spotahome] URL de recherche réelle (%s) : %s", ville, page.url)
        if not ROBOTS.autorise(page.url):
            raise SpotahomeIndisponible(f"robots.txt interdit {page.url}")

    def _attendre_cartes(self, page) -> None:
        try:
            page.wait_for_selector(SEL_CARTE, timeout=TIMEOUT_CARTE_MS)
        except PWTimeout:
            raise SpotahomeIndisponible(f"aucune carte ({SEL_CARTE}) après {TIMEOUT_CARTE_MS // 1000}s")

    @staticmethod
    def _debug(page, ville: str) -> None:
        fichier = f"spotahome_debug_{slugifier(ville)}.png"
        try:
            page.screenshot(path=fichier, full_page=True)
            log.info("[Spotahome] capture enregistrée : %s", fichier)
        except Exception as e:
            log.debug("capture impossible : %s", e)
