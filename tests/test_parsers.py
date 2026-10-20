"""Tests hors réseau : python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import ConfigError, valider_config  # noqa: E402
from scrapers.cloe_scraper import parse_liste  # noqa: E402
from datetime import date  # noqa: E402
from scrapers.colette_scraper import extraire_prix, label_jour, nb_pages, parse_annonces as parse_colette  # noqa: E402
from scrapers.spotahome_scraper import parse_cartes  # noqa: E402
from scrapers.studapart_scraper import filtrer_par_villes, parse_annonces  # noqa: E402
from utils.geocoding import extraire_directions  # noqa: E402
from utils.apl_detector import detect_apl  # noqa: E402
from utils.dedup import dedupliquer  # noqa: E402
from utils.scorer import noter  # noqa: E402

CLOE = """<html><script type="application/ld+json">{"@type":"ItemList","itemListElement":[{"url":"https://www.cloe-edu.fr/fr/x-1"}]}</script>
<div><div class="p-4"><div><span class="rounded-md">Chambre</span><span class="font-extrabold">650<!-- -->€</span></div>
<h3 class="text-sm">Chambre coloc</h3><div><svg></svg><span><span>Paris</span><span> · 5 Rue X, 75020 Paris</span></span></div>
<span>12-12 m²</span></div></div></html>"""
SPOTA = """<div data-homecard-scroll="1"><a data-test="homecard-link" href="/fr/paris/for-rent:rooms/1"><span>Chambre</span><span>4.3 (35)</span>
<p>Chambre à louer dans un appartement à La Plaine, Paris</p></a><div data-test="homecard-content"><span>700 €</span></div></div>
<div data-homecard-scroll="2"><a data-test="homecard-link" href="/fr/paris/for-rent:rooms/2"><p>Sans prix</p></a><div data-test="homecard-content"></div></div>"""
STUDA = """<div><a href="/fr/property/abc">Colocation meublée</a><span>Colocation meublée À partir de 740€ cc / mois Châtillon (92320) 75m²</span></div>"""


class Tests(unittest.TestCase):
    def test_cloe(self):
        a, rej = parse_liste(CLOE, "https://x/liste")
        self.assertEqual((len(a), a[0]["prix_mensuel"], a[0]["type_logement"], a[0]["surface_m2"]), (1, 650.0, "Colocation", 12.0))
        self.assertEqual(a[0]["url"], "https://www.cloe-edu.fr/fr/x-1")

    def test_spotahome(self):
        a, rej = parse_cartes(SPOTA)
        self.assertEqual((len(a), a[0]["note"], a[0]["adresse_ou_ville"]), (1, 4.3, "La Plaine, Paris"))
        self.assertEqual(rej, {"prix illisible": 1})

    def test_studapart(self):
        a, _ = parse_annonces(STUDA)
        self.assertEqual((len(a), a[0]["prix_mensuel"], a[0]["charges_incluses"], a[0]["adresse_ou_ville"]),
                         (1, 740.0, True, "Châtillon, 92320"))

    def test_colette(self):
        html = """<nav><a href="/">Accueil</a><a href="/requests">Mes demandes</a></nav>
        <div>Chambres 1-2 sur 2 · page 1 / 3</div>
        <a href="/accommodations/d59febe4-d449-4e97-bd3d-793e8c78c0d1?startDate=2026-10-08&endDate=2026-11-08&origin=SEARCH">
          <span>5</span><h3>Montreuil (93100), chambre chez Salim M.</h3><p>Montreuil &bull; APL/ALS</p><p>600 &euro;/mois</p></a>
        <a href="/accommodations/8210f749-083f-4d0d-a386-c365d0efb77d?x=1"><h3>Paris 15e, chambre 1 chez Hélène B.</h3><p>555 €/mois</p></a>
        <a href="/accommodations?location=Paris">Lien de recherche</a>"""
        a, _ = parse_colette(html, "https://www.colette-cohabitation.com/accommodations?x")
        self.assertEqual([x["prix_mensuel"] for x in a], [600.0, 555.0])
        self.assertEqual((a[0]["type_logement"], a[0]["source"], a[0]["adresse_ou_ville"], a[0]["apl"]),
                         ("Cohabitation intergénérationnelle", "Colette", "Montreuil, 93100", True))
        self.assertEqual(a[0]["url"], "https://www.colette-cohabitation.com/accommodations/d59febe4-d449-4e97-bd3d-793e8c78c0d1")
        self.assertEqual(a[1]["adresse_ou_ville"], "Paris 15e")
        self.assertEqual(nb_pages(html), 3)
        self.assertEqual(label_jour(date(2026, 10, 15)), "jeudi 15 octobre 2026")
        self.assertEqual(extraire_prix("Loyer 1 200 € dont participation 450 €"), 1200.0)

    def test_apl(self):
        self.assertIs(detect_apl("Studio éligible CAF", ""), True)
        self.assertIs(detect_apl("Studio", "non éligible APL"), False)
        self.assertIsNone(detect_apl("Studio", "joli"))

    def test_dedup(self):
        base = {"url": "u1", "titre": "Belle chambre à Paris", "prix_mensuel": 650}
        self.assertEqual(len(dedupliquer([base, {**base, "url": "u2", "prix_mensuel": 655}])), 1)
        self.assertEqual(len(dedupliquer([base, {**base, "url": "u2", "prix_mensuel": 700}])), 2)

    def test_score(self):
        w = {"w_prix": .35, "w_distance": .35, "w_confort": .2, "w_apl": .1}
        a = {"prix_mensuel": 600, "temps_trajet_min": 20, "surface_m2": 20, "note": None, "apl": True, "charges_incluses": True}
        b = {"prix_mensuel": 700, "temps_trajet_min": 60, "surface_m2": 10, "note": None, "apl": None, "charges_incluses": None}
        self.assertEqual(noter([b, a], w)[0], a)

    def test_filtre_villes_studapart(self):
        a = [{"adresse_ou_ville": "Meudon, 92190"}, {"adresse_ou_ville": "Châtillon, 92320"},
             {"adresse_ou_ville": "Rue X, 75015 Paris"}]
        par_ville, hors = filtrer_par_villes(a, ["Paris", "Meudon", "Clamart"])
        self.assertEqual((len(par_ville["Meudon"]), len(par_ville["Paris"]), len(par_ville["Clamart"]), hors), (1, 1, 0, 1))
        self.assertEqual(par_ville["Meudon"][0]["ville_recherche"], "Meudon")

    def test_directions_correspondances(self):
        etape = lambda m: {"travel_mode": m}
        rep = {"status": "OK", "routes": [
            {"legs": [{"duration": {"value": 3000}, "steps": [etape("WALKING"), etape("TRANSIT"), etape("TRANSIT"), etape("TRANSIT")]}]},
            {"legs": [{"duration": {"value": 3600}, "steps": [etape("TRANSIT")]}]}]}
        self.assertEqual(extraire_directions(rep), {"minutes": 50, "correspondances": 2})  # 3 transports - 1, le plus rapide
        self.assertIsNone(extraire_directions({"status": "ZERO_RESULTS", "routes": []}))

    def test_config(self):
        with self.assertRaises(ConfigError):
            valider_config({"ecole": {"nom": "x"}})
        with self.assertRaises(ConfigError):  # villes vides
            import yaml
            cfg = yaml.safe_load(open("config.yaml", encoding="utf8"))
            cfg["recherche"]["villes"] = []
            valider_config(cfg)
        valider_config(yaml.safe_load(open("config.yaml", encoding="utf8")))  # la config par défaut est valide


if __name__ == "__main__":
    unittest.main()
