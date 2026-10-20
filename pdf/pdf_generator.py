"""Rapport PDF (ReportLab) : critères, tableau du Top 10, fiches détaillées (avec image), aides & conseils."""
from __future__ import annotations

import io
import logging
from datetime import datetime
from xml.sax.saxutils import escape

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

log = logging.getLogger("pdf")
SS = getSampleStyleSheet()
PETIT = ParagraphStyle("petit", parent=SS["BodyText"], fontSize=8, leading=10)
CELLULE = ParagraphStyle("cellule", parent=SS["BodyText"], fontSize=8.5, leading=10.5)
BLEU = colors.HexColor("#0f2a52")
IMAGE_LARGEUR = 6 * cm
IMAGE_HAUTEUR_MAX = 4.5 * cm


def texte_apl(apl: bool | None) -> str:
    return {True: "Oui (indicatif)", False: "Non (indiqué)", None: "Non précisé"}[apl]


def texte_trajet(a: dict) -> str:
    if a.get("temps_trajet_min") is None:
        return "n/d"
    approx = "~" if a.get("temps_trajet_estime") else ""
    return f"{approx}{a['temps_trajet_min']} min ({a.get('distance_km', 0):.1f} km)"


def texte_correspondances(a: dict) -> str:
    corr = a.get("correspondances")
    if corr is None or not a.get("correspondances_verifiees", False):
        return "non vérifié"
    return "direct" if corr == 0 else str(corr)


def lien(url: str, libelle: str = "Voir l'annonce") -> str:
    u = escape(url or "", {'"': "&quot;"})
    return f'<link href="{u}" color="blue">{escape(libelle)}</link>' if url else "-"


def telecharger_image(url: str | None) -> Image | None:
    """Image redimensionnée, ou None au moindre échec : une image ne doit jamais bloquer le PDF."""
    if not url or not url.startswith("http"):
        return None
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        donnees = io.BytesIO(r.content)
        w, h = ImageReader(donnees).getSize()
        donnees.seek(0)
        ratio = min(IMAGE_LARGEUR / w, IMAGE_HAUTEUR_MAX / h)
        return Image(donnees, width=w * ratio, height=h * ratio)
    except Exception as e:
        log.warning("Image ignorée (%s) : %s", url, e)
        return None


def generer_pdf(top: list[dict], cfg: dict, chemin: str, stats: dict) -> None:
    doc = SimpleDocTemplate(chemin, pagesize=landscape(A4), leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm, title="Meilleures offres de logement")
    f, w = cfg["filtres"], cfg["poids_score"]
    s: list = [Paragraph("Meilleures offres de logement étudiant", SS["Title"]),
               Paragraph(f"Généré le {datetime.now():%d/%m/%Y à %H:%M}", SS["Normal"]), Spacer(1, 10),
               Paragraph("Critères de recherche", SS["Heading2"])]
    criteres = [
        f"École cible : {escape(cfg['ecole']['nom'])} – {escape(cfg['ecole']['adresse'])}",
        f"Budget : {f['prix_min']:.0f} – {f['prix_max']:.0f} €/mois",
        f"Temps de trajet maximum : {f['temps_trajet_max_minutes']} min · correspondances maximum : {f['correspondances_max']}",
        "Zone de recherche : " + escape(", ".join(cfg["recherche"]["villes"])),
        f"Pondération du score : prix {w['w_prix']} · distance {w['w_distance']} · confort {w['w_confort']} · APL {w['w_apl']}",
        f"Annonces collectées : {stats['brutes']} · retenues après filtres et dédoublonnage : {stats['retenues']}",
        "Sources : " + ", ".join(f"{k} ({v})" for k, v in stats["par_source"].items()),
    ]
    s += [Paragraph("• " + c, SS["BodyText"]) for c in criteres]
    s += [Spacer(1, 8), Paragraph(
        "<b>Notes :</b> l'éligibilité APL/CAF est <b>indicative</b> (détection de mots-clés ; « Non précisé » ne "
        "veut pas dire « Non ») : à vérifier avec le simulateur officiel de la "
        "<link href=\"https://www.caf.fr\" color=\"blue\">CAF</link>. Un temps précédé de « ~ » est une "
        "estimation approximative à vol d'oiseau, pas un horaire réel de transports. <b>Correspondances « non vérifié »</b> : "
        "aucune clé d'API de transports (Google Directions ou PRIM) n'a permis de les calculer ; l'offre est conservée "
        f"par défaut ({stats.get('non_verifiees', 0)} offre(s) concernée(s) au total).", PETIT), PageBreak()]

    # --- Tableau récapitulatif
    s.append(Paragraph(f"Top {len(top)} – tableau récapitulatif", SS["Heading1"]))
    lignes = [["#", "Prix", "Ville / Adresse", "Temps de trajet", "Correspondances", "APL/CAF", "Source", "Lien"]]
    for i, a in enumerate(top, 1):
        lieu = a["adresse_ou_ville"] or a.get("ville_recherche") or "n/d"
        lignes.append([str(i), f"{a['prix_mensuel']:.0f} €", Paragraph(escape(lieu), CELLULE),
                       Paragraph(escape(texte_trajet(a)), CELLULE), Paragraph(texte_correspondances(a), CELLULE),
                       Paragraph(texte_apl(a.get("apl")), CELLULE), a["source"], Paragraph(lien(a["url"]), CELLULE)])
    t = Table(lignes, colWidths=[0.9 * cm, 1.8 * cm, 7 * cm, 3.6 * cm, 2.8 * cm, 3 * cm, 2.3 * cm, 3.2 * cm], repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), BLEU), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                           ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5fa")]),
                           ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey)]))
    s += [t, PageBreak(), Paragraph("Fiches détaillées", SS["Heading1"])]

    # --- Fiches détaillées
    for i, a in enumerate(top, 1):
        charges = {True: "oui", False: "non", None: "non précisé"}[a.get("charges_incluses")]
        champs = [
            ("Prix", f"{a['prix_mensuel']:.0f} €/mois (charges comprises : {charges})"),
            ("Type", escape(a["type_logement"])), ("Adresse", escape(a["adresse_ou_ville"] or "n/d")),
            ("Surface", f"{a['surface_m2']:.0f} m²" if a.get("surface_m2") else "n/d"),
            ("Note", f"{a['note']}/5" if a.get("note") is not None else "n/d"),
            ("Trajet école", f"{escape(texte_trajet(a))} – {escape(a.get('methode_trajet', ''))}"),
            ("Correspondances", texte_correspondances(a)
             + ("" if a.get("correspondances_verifiees") else " (aucune clé d'API de transports)")),
            ("APL/CAF", texte_apl(a.get("apl")) + " (indicatif)"), ("Score", f"{a['score']:.3f}"),
            ("Source", a["source"]), ("Ville de recherche", escape(a.get("ville_recherche") or "n/d")),
            ("Descriptif", escape((a.get("description") or "n/d")[:400])), ("Lien", lien(a["url"], a["url"])),
        ]
        tbl = Table([[Paragraph(f"<b>{k}</b>", CELLULE), Paragraph(str(v), CELLULE)] for k, v in champs],
                    colWidths=[3.5 * cm, 14 * cm])
        tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
                                 ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f5fa"))]))
        img = telecharger_image(a.get("image_url"))
        corps = Table([[tbl, img]] if img else [[tbl]], colWidths=[18 * cm, 7 * cm] if img else [18 * cm])
        corps.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        s.append(KeepTogether([Paragraph(f"{i}. {escape(a['titre'] or 'Annonce')}", SS["Heading3"]), corps,
                               Spacer(1, 10)]))

    # --- Aides & conseils (contenu statique)
    s += [PageBreak(), Paragraph("Aides & Conseils", SS["Heading1"])]
    for titre, items in CONSEILS:
        s.append(Paragraph(titre, SS["Heading2"]))
        s += [Paragraph("• " + x, SS["BodyText"]) for x in items]
    doc.build(s)


CONSEILS = [
    ("Visale (garantie locative gratuite)", [
        "Garantie gratuite d'Action Logement pour les 18-30 ans (étudiants, selon conditions).",
        "Créer un compte sur <link href=\"https://www.visale.fr\" color=\"blue\">visale.fr</link> et obtenir le visa Visale.",
        "Le bailleur doit accepter Visale ; le loyer doit respecter le plafond (≈ 800 € à Paris, charges comprises).",
        "Remettre le visa au propriétaire avant la signature du bail.",
    ]),
    ("Demande APL / CAF", [
        "Se connecter sur <link href=\"https://www.caf.fr\" color=\"blue\">caf.fr</link> et faire une simulation d'aide au logement.",
        "Déposer la demande dès l'entrée dans les lieux : l'aide n'est pas rétroactive (droit ouvert à partir du mois suivant).",
        "Le logement doit être décent ; en colocation, le calcul porte sur la part de loyer.",
        "Mettre à jour sa déclaration de ressources quand la CAF le demande.",
    ]),
    ("Cohabitation intergénérationnelle (Colette)", [
        "Ces offres relèvent le plus souvent d'une <b>convention d'hébergement</b> et non d'un bail classique.",
        "L'éligibilité APL/CAF doit donc être vérifiée <b>au cas par cas directement avec la CAF</b> : ne pas la présumer.",
        "Le prix affiché peut s'appeler « participation » ou « contribution » : vérifier ce qu'il inclut (charges, services).",
    ]),
    ("Pièces du dossier de location", [
        "Pièce d'identité et certificat de scolarité (ECE Paris).",
        "Justificatifs du garant (3 derniers bulletins de salaire, dernier avis d'imposition) ou visa Visale.",
        "RIB et attestation d'assurance habitation (exigée avant la remise des clés).",
        "Pour la CAF : numéro d'allocataire ou de sécurité sociale, bail signé, RIB.",
        "Prudence : ne jamais verser d'argent avant visite ou signature du bail ; refuser les paiements hors plateforme.",
    ]),
]
