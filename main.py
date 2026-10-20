"""Orchestrateur : scrape (4 sites) -> cache JSON -> filtres -> dédoublonnage -> trajet -> score -> PDF.

  python main.py                  # utilise annonces_brutes.json s'il existe, sinon scrape
  python main.py --force-rescrape # ignore le cache existant et re-scrape
  python main.py --no-scrape      # régénère le PDF depuis annonces_brutes.json, sans scraper
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from pdf.pdf_generator import generer_pdf
from scrapers.base_scraper import BaseScraper
from scrapers.cloe_scraper import CloeScraper
from scrapers.colette_scraper import ColetteScraper
from scrapers.spotahome_scraper import SpotahomeScraper
from scrapers.studapart_scraper import StudapartScraper
from utils.dedup import dedupliquer
from utils.geocoding import Geocodeur
from utils.scorer import noter

log = logging.getLogger("main")

CLES_REQUISES = [
    "ecole.nom", "ecole.adresse",
    "recherche.villes",
    "filtres.prix_min", "filtres.prix_max", "filtres.temps_trajet_max_minutes", "filtres.correspondances_max",
    "poids_score.w_prix", "poids_score.w_distance", "poids_score.w_confort", "poids_score.w_apl",
    "api.google_transit_api_key", "api.ors_api_key",
    "sessions.studapart.url", "sessions.studapart.state_file",
    "sessions.colette.url", "sessions.colette.state_file",
    "fichiers.cache_json", "fichiers.output_pdf", "fichiers.log_file",
]
TOP_N = 10


class ConfigError(Exception):
    pass


def valider_config(cfg: Any) -> None:
    """Vérifie que toutes les clés attendues existent ; lève une erreur claire listant celles qui manquent."""
    if not isinstance(cfg, dict):
        raise ConfigError("config.yaml est vide ou n'est pas un mapping YAML")
    manquantes = []
    for chemin in CLES_REQUISES:
        noeud: Any = cfg
        for morceau in chemin.split("."):
            if not isinstance(noeud, dict) or morceau not in noeud:
                manquantes.append(chemin)
                break
            noeud = noeud[morceau]
    if manquantes:
        raise ConfigError("Clés manquantes dans config.yaml : " + ", ".join(manquantes))
    f = cfg["filtres"]
    if f["prix_min"] > f["prix_max"]:
        raise ConfigError("filtres.prix_min doit être <= filtres.prix_max")
    villes = cfg["recherche"]["villes"]
    if not isinstance(villes, list) or not villes or not all(isinstance(v, str) and v.strip() for v in villes):
        raise ConfigError("recherche.villes doit être une liste non vide de noms de villes")


def configurer_logs(fichier: str, debug: bool = False) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    racine = logging.getLogger()
    racine.setLevel(logging.DEBUG if debug else logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(fichier, encoding="utf8")):
        h.setFormatter(fmt)
        racine.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


def scraper_tout(cfg: dict, sources: list[str]) -> tuple[list[dict], set[str]]:
    """Lance chaque scraper demandé dans son propre try/except. Retourne (annonces, sources réussies)."""
    villes = cfg["recherche"]["villes"]  # base de recherche ; le calcul de trajet décide ensuite des offres retenues
    scrapers: list[BaseScraper] = [
        CloeScraper(villes),
        StudapartScraper(cfg["sessions"]["studapart"]["state_file"], villes),
        SpotahomeScraper(villes),
        ColetteScraper(cfg["sessions"]["colette"]["state_file"], villes),
    ]
    annonces: list[dict] = []
    reussies: set[str] = set()
    for sc in scrapers:
        if sc.nom.lower() not in sources:
            continue
        try:
            res = sc.fetch_listings()
            annonces.extend(res)
            if res:
                reussies.add(sc.nom)
        except Exception as e:  # un scraper défaillant n'arrête pas les autres
            log.error("[%s] échec : %s", sc.nom, e, exc_info=log.isEnabledFor(logging.DEBUG))
        sc.bilan()
    return annonces, reussies


def charger_cache(chemin: Path) -> list[dict]:
    try:
        return json.loads(chemin.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return []


def enrichir_trajets(annonces: list[dict], cfg: dict) -> None:
    geo = Geocodeur(cfg["api"]["ors_api_key"], cfg["api"]["google_transit_api_key"])
    ecole = geo.geocoder_adresse(cfg["ecole"]["adresse"])
    if ecole is None:
        log.error("Adresse de l'école non géocodable : aucun temps de trajet calculé")
        return
    for a in annonces:
        try:
            pt = geo.geocoder_adresse(a["adresse_ou_ville"])
            if pt:
                a.update(geo.trajet(pt, ecole))
        except Exception as e:
            log.warning("Trajet non calculé (%s) : %s", a["url"], e)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--no-scrape", action="store_true", help="régénère le PDF depuis annonces_brutes.json")
    ap.add_argument("--force-rescrape", action="store_true", help="ignore le cache existant")
    ap.add_argument("--sources", nargs="+", default=["cloe", "studapart", "spotahome", "colette"],
                    choices=["cloe", "studapart", "spotahome", "colette"], help="sites à scraper (les autres gardent leur cache)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    if args.no_scrape and args.force_rescrape:
        ap.error("--no-scrape et --force-rescrape sont incompatibles")

    try:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf8"))
        valider_config(cfg)
    except (OSError, yaml.YAMLError, ConfigError) as e:
        print(f"Erreur de configuration : {e}", file=sys.stderr)
        return 2
    configurer_logs(cfg["fichiers"]["log_file"], args.debug)
    cache = Path(cfg["fichiers"]["cache_json"])

    if args.no_scrape or (cache.exists() and not args.force_rescrape):
        brutes = charger_cache(cache)
        if not brutes:
            log.error("Cache %s absent ou vide : lance sans --no-scrape", cache)
            return 1
        log.info("%d annonces lues depuis le cache %s", len(brutes), cache)
    else:
        nouvelles, reussies = scraper_tout(cfg, args.sources)
        # une source non lancée ou en échec garde ses anciennes données plutôt que d'être écrasée par du vide
        anciennes = [a for a in charger_cache(cache) if a["source"] not in reussies]
        brutes = nouvelles + anciennes
        cache.write_text(json.dumps(brutes, ensure_ascii=False, indent=1), encoding="utf8")
        log.info("%d annonces brutes sauvegardées dans %s", len(brutes), cache)

    f = cfg["filtres"]
    dans_budget = [a for a in brutes if f["prix_min"] <= a["prix_mensuel"] <= f["prix_max"]]
    log.info("Filtre prix %s-%s € : %d -> %d", f["prix_min"], f["prix_max"], len(brutes), len(dans_budget))
    uniques = dedupliquer(dans_budget)
    enrichir_trajets(uniques, cfg)
    for a in uniques:  # valeurs par défaut si aucun trajet n'a pu être calculé
        a.setdefault("correspondances", None)
        a.setdefault("correspondances_verifiees", False)
    inconnus = [a for a in uniques if a.get("temps_trajet_min") is None]
    # Filtre d'acceptation : trajet <= max ET (correspondances <= max OU correspondances inconnues)
    retenues = [a for a in uniques
                if (a.get("temps_trajet_min") is None or a["temps_trajet_min"] <= f["temps_trajet_max_minutes"])
                and (a["correspondances"] is None or a["correspondances"] <= f["correspondances_max"])]
    non_verifiees = sum(1 for a in retenues if not a["correspondances_verifiees"])
    log.info("Filtre trajet <= %d min et correspondances <= %d : %d -> %d (%d sans trajet calculable, %d "
             "correspondances non vérifiées : conservées)", f["temps_trajet_max_minutes"], f["correspondances_max"],
             len(uniques), len(retenues), len(inconnus), non_verifiees)
    if not retenues:
        log.error("Aucune offre ne correspond aux critères : PDF non généré.")
        return 1
    classees = noter(retenues, cfg["poids_score"])
    stats = {"brutes": len(brutes), "retenues": len(retenues), "par_source": Counter(a["source"] for a in retenues), "non_verifiees": non_verifiees}
    generer_pdf(classees[:TOP_N], cfg, cfg["fichiers"]["output_pdf"], stats)
    log.info("PDF généré : %s", Path(cfg["fichiers"]["output_pdf"]).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
