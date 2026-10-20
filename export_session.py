"""Sauvegarde une session authentifiée (connexion MANUELLE) pour Studapart ou Colette.

    python export_session.py --site studapart
    python export_session.py --site colette

L'URL cible et le fichier de sortie viennent de config.yaml > sessions.<site>. Ce script ne lit ni ne saisit jamais
un mot de passe. Le fichier produit (storage_state_*.json) contient des cookies de session : ne jamais le commiter
ni le partager.

Note : Google refuse la connexion dans le Chromium de test de Playwright (la page tourne sans fin). On lance donc
le Chrome installé (même moteur Chromium) sans le drapeau d'automatisation ; repli sur Edge puis Chromium.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

log = logging.getLogger("export_session")
DOSSIER = Path(__file__).parent
OPTIONS = dict(headless=False, locale="fr-FR", no_viewport=True,
               ignore_default_args=["--enable-automation"], args=["--disable-blink-features=AutomationControlled"])


def charger_session(site: str, chemin_config: str = "config.yaml") -> tuple[str, Path]:
    """Lit (url, fichier d'état) dans config['sessions'][site] ; erreur claire si absent."""
    cfg = yaml.safe_load((DOSSIER / chemin_config).read_text(encoding="utf8")) or {}
    sessions = cfg.get("sessions") or {}
    if site not in sessions or not {"url", "state_file"} <= set(sessions[site]):
        raise SystemExit(f"config.yaml : sessions.{site}.url et sessions.{site}.state_file sont requis "
                         f"(sites connus : {', '.join(sessions) or 'aucun'})")
    return sessions[site]["url"], DOSSIER / sessions[site]["state_file"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, choices=["studapart", "colette"])
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    url, fichier = charger_session(args.site, args.config)
    profil = DOSSIER / f"chrome_profile_{args.site}"  # profil dédié par site, conservé entre deux lancements
    with sync_playwright() as p:
        contexte = None
        for canal in ("chrome", "msedge", None):
            try:
                contexte = p.chromium.launch_persistent_context(str(profil), **({"channel": canal} if canal else {}), **OPTIONS)
                break
            except Exception as e:
                log.info("Navigateur '%s' indisponible (%s), essai suivant...", canal or "chromium", str(e).splitlines()[0])
        if contexte is None:
            sys.exit("Aucun navigateur utilisable.")
        page = contexte.pages[0] if contexte.pages else contexte.new_page()
        page.goto(url)
        input(f"Veuillez vous connecter (ou vous inscrire si besoin) manuellement sur {args.site}. Une fois connecté "
              "et sur une page montrant des annonces, appuyez sur ENTRÉE dans ce terminal pour sauvegarder la session...")
        contexte.storage_state(path=str(fichier))
        log.info("Session sauvegardée dans %s", fichier.name)
        contexte.close()


if __name__ == "__main__":
    main()
