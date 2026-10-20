"""Scraper omneseducation.studapart.com (portail SSO Omnes Education).

Aucun mot de passe n'est géré : la session vient de storage_state.json (créé par `python export_session.py --site studapart`).
Structure vérifiée sur une session réelle : chaque annonce est un lien /fr/residence/<id> ou /fr/property/<uuid> ;
le texte de la carte contient « <titre> À partir de 830€ cc / mois <Ville> (<cp>) <équipements> ... ».
Le filtre de recherche (budget, ville) est celui enregistré dans la session Studapart.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from utils.apl_detector import detect_apl

from .base_scraper import (ROBOTS, USER_AGENT, BaseScraper, nouvelle_annonce, normaliser_type, parse_surface,
                           pause_polie, normaliser_nom)

log = logging.getLogger("scraper.studapart")

# --- Sélecteurs CSS / constantes ---
URL_RECHERCHE = "https://omneseducation.studapart.com/fr/search/"
SEL_LIEN_ANNONCE = "a[href^='/fr/residence/'], a[href^='/fr/property/']"
SEL_FORM_LOGIN = "input[type='password']"
MARQUEURS_LOGIN = ("login", "sso", "saml", "cas/")
SEL_IMAGE = "img"
TIMEOUT_MS = 30000  # la page de recherche est une application dynamique, parfois lente à afficher les cartes
MESSAGE_SESSION = "Session Studapart expirée. Veuillez exécuter 'python export_session.py --site studapart'."

RE_PRIX = re.compile(r"(\d[\d\s]*)\s*€\s*(cc|hc)?", re.I)
RE_VILLE = re.compile(r"([A-Za-zÀ-ÿ'’\- ]+?)\s*\((\d{5})\)")
BRUIT_TITRE = re.compile(r"^(?:\s*(?:Réduction|360°|Réservable à la chambre|Pro))+\s*", re.I)


class SessionExpiree(Exception):
    pass


def parse_annonces(html: str, base_url: str = URL_RECHERCHE) -> tuple[list[dict], dict[str, int]]:
    """Fonction PURE : HTML de la page de recherche -> (annonces normalisées, rejets par raison)."""
    soup = BeautifulSoup(html, "lxml")
    annonces: list[dict] = []
    rejets: dict[str, int] = {}
    vus: set[str] = set()
    for lien in soup.select(SEL_LIEN_ANNONCE):
        href = lien["href"]
        if href in vus:
            continue
        vus.add(href)
        # le texte de la carte est dans un ancêtre (le lien seul n'a pas toujours le prix)
        noeud = lien
        for _ in range(4):
            if "€" in noeud.get_text() or noeud.parent is None:
                break
            noeud = noeud.parent
        texte = re.sub(r"\s+", " ", noeud.get_text(" ")).strip()
        m_prix = RE_PRIX.search(texte)
        if not m_prix:
            rejets["prix illisible"] = rejets.get("prix illisible", 0) + 1
            continue
        titre = BRUIT_TITRE.sub("", texte.split("À partir de")[0]).strip() or texte[:80]
        m_ville = RE_VILLE.search(texte.split("mois", 1)[-1])
        adresse = f"{m_ville.group(1).strip()}, {m_ville.group(2)}" if m_ville else ""
        type_brut = "résidence" if href.startswith("/fr/residence/") else titre
        img = noeud.select_one(SEL_IMAGE)
        annonces.append(nouvelle_annonce(
            "Studapart", titre=titre, type_logement=normaliser_type(type_brut),
            prix_mensuel=float(re.sub(r"\s", "", m_prix.group(1))),
            charges_incluses=True if (m_prix.group(2) or "").lower() == "cc" else None,
            adresse_ou_ville=adresse, url=urljoin(base_url, href), surface_m2=parse_surface(texte),
            description=texte, apl=detect_apl(titre, texte), image_url=img.get("src") if img else None))
    return annonces, rejets


def ville_correspond(adresse: str, ville: str) -> bool:
    """Fonction PURE : l'adresse (« Châtillon, 92320 ») appartient-elle à la ville demandée ?"""
    a, v = f" {normaliser_nom(adresse)} ", normaliser_nom(ville)
    if f" {v} " in a:
        return True
    return v == "paris" and bool(re.search(r"\b75\d{3}\b", adresse))  # Paris = codes postaux 750xx


def filtrer_par_villes(annonces: list[dict], villes: list[str]) -> tuple[dict[str, list[dict]], int]:
    """Fonction PURE : répartit les annonces par ville demandée. Retourne ({ville: annonces}, nb hors zone)."""
    par_ville: dict[str, list[dict]] = {v: [] for v in villes}
    hors_zone = 0
    for a in annonces:
        v = next((v for v in villes if ville_correspond(a["adresse_ou_ville"], v)), None)
        if v is None:
            hors_zone += 1
            continue
        a["ville_recherche"] = v
        par_ville[v].append(a)
    return par_ville, hors_zone


class StudapartScraper(BaseScraper):
    nom = "Studapart"

    def __init__(self, storage_state: str = "storage_state_studapart.json", villes: list[str] | None = None,
                 headless: bool = True) -> None:
        super().__init__()
        self.storage_state, self.headless = storage_state, headless
        self.villes = villes or []  # vide = pas de filtre de zone

    def fetch_listings(self) -> list[dict]:
        if not Path(self.storage_state).exists():
            log.warning("[Studapart] %s introuvable. %s", self.storage_state, MESSAGE_SESSION)
            return []
        if not ROBOTS.autorise(URL_RECHERCHE):
            log.warning("[Studapart] robots.txt interdit %s", URL_RECHERCHE)
            return []
        with sync_playwright() as p:
            navigateur = p.chromium.launch(headless=self.headless)
            try:
                ctx = navigateur.new_context(storage_state=self.storage_state, user_agent=USER_AGENT, locale="fr-FR")
                page = ctx.new_page()
                page.goto(URL_RECHERCHE, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                self._verifier_session(page)
                try:
                    page.wait_for_selector(SEL_LIEN_ANNONCE, timeout=TIMEOUT_MS)
                except PWTimeout:
                    self._verifier_session(page)
                    log.warning("[Studapart] aucune carte après %d s, nouvel essai après rechargement", TIMEOUT_MS // 1000)
                    page.reload(wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector(SEL_LIEN_ANNONCE, timeout=TIMEOUT_MS)
                    except PWTimeout:
                        self._verifier_session(page)
                        log.error("[Studapart] aucune annonce affichée (filtre de recherche trop restrictif ?)")
                        return []
                pause_polie()
                annonces, rejets = parse_annonces(page.content(), page.url)
            except SessionExpiree:
                log.error("[Studapart] %s", MESSAGE_SESSION)  # arrêt propre, l'orchestrateur continue
                return []
            finally:
                navigateur.close()
        if not self.villes:
            self.bilan_ville("(toutes)", len(annonces), rejets)
            self.trouvees = len(annonces)
            return annonces
        # Le filtre de localisation de l'interface ne propose pas de suggestion de ville (vérifié) : on récupère
        # tous les résultats de la recherche enregistrée et on filtre côté client sur les villes demandées.
        par_ville, hors_zone = filtrer_par_villes(annonces, self.villes)
        for ville, liste in par_ville.items():
            if not liste:
                log.warning("[Studapart] %s : aucune annonce dans les résultats de la recherche enregistrée", ville)
            self.bilan_ville(ville, len(liste), rejets if ville == self.villes[0] else None)
        if hors_zone:
            self.bilan_ville("(hors villes demandées)", 0, {"hors villes demandées": hors_zone})
        retenues = [a for liste in par_ville.values() for a in liste]
        self.trouvees = len(retenues)
        return retenues

    @staticmethod
    def _verifier_session(page) -> None:
        if any(m in page.url.lower() for m in MARQUEURS_LOGIN) or page.locator(SEL_FORM_LOGIN).count() > 0:
            raise SessionExpiree(MESSAGE_SESSION)
