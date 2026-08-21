"""
Module partagé : calcul du résumé de fiabilité (concordant/écart/partiel/absent) d'une
comparaison Odicee <-> Prestataire, sans aucun appel Streamlit — utilisé à la fois par
4_Comparateur_Odicee_Prestataire.py (enregistrement automatique après consultation) et
6_Suivi_Fiabilite.py (tableau de bord + recalcul global).

Reprend délibérément la même logique de comparaison que le comparateur interactif (mêmes
seuils, mêmes exclusions Site/AH, mêmes champs cumulables) pour que les chiffres du tableau
de bord correspondent toujours à ce qu'on verrait en ouvrant le dossier dans le comparateur.
"""

import json
import re
import unicodedata
import difflib
from datetime import datetime

from utils import REGLES, decoder_valeur, champs_en104, CHAMPS_CUMULABLES

ROMAIN_VERS_ARABE = {"IV": "4", "V": "5", "VI": "6", "VII": "7", "VIII": "8"}

TYPES_ALTERNATIFS = {"Invoice": ["Invoice", "CeeInvoice", "FinalSettlement", "AcceptanceReport"]}

FIELD_MAPPING = {
    "BAR-EN-101": {
        "surface": "surfaceM2", "resistance_thermique": "thermalResistance",
        "marque_isolant": "brand", "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm", "date_visite_pro": "preVisitDate",
    },
    "BAR-EN-102": {
        "surface": "surfaceM2", "resistance_thermique": "thermalResistance",
        "marque_isolant": "brand", "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm", "date_visite_pro": "preVisitDate",
    },
    "BAR-EN-103": {
        "surface": "surfaceM2", "resistance_thermique": "thermalResistance",
        "marque_isolant": "brand", "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm", "date_visite_pro": "preVisitDate",
    },
    "BAR-EN-105": {
        "surface": "surfaceM2", "resistance_thermique_non_exported": "thermalResistance",
        "marque_isolant": "brand", "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm",
    },
    "BAR-EN-104": {
        "coefficient_surfacique": "uw", "facteur_solaire_sw": "sw",
        "marque_fenetre": "brand", "reference_fenetre": "productReference",
        "surface_fenetres": "surfaceM2", "nombre_de_fenetres_ou_portefenetres": "quantity",
        "marque_isolant": "brand", "reference_isolant": "productReference",
    },
    "BAR-TH-110": {
        "marque_radiateurs": "brand", "reference_radiateurs": "productReference",
        "nb_radiateurs": "quantity", "delta_temperature": "dtNomKelvin",
    },
    "BAR-TH-127": {
        "marque_caisson": "caissonsBrand", "reference_caisson": "caissonsReference",
        "marque_bouches_entree_air": "entreesAirBrand", "reference_bouches_entree_air": "entreesAirReference",
        "marque_bouches_extraction": "bouchesBrand", "reference_bouches_extraction": "bouchesReference",
        "surface_habitable": "surfaceHabitable", "puissance_individuelle": "weightedAbsorbedPower",
    },
}


def normalise_texte(v):
    if v is None:
        return ""
    s = str(v).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[,;:|+\-()/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalise_nombre(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    m = re.match(r"^-?\d+(\.\d+)?\s*[a-zA-Zé%²/°.]*$", s)
    if not m:
        return None
    try:
        return float(re.match(r"^-?\d+(\.\d+)?", s).group(0))
    except (ValueError, AttributeError):
        return None


def normalise_date(v):
    if v is None or v == "":
        return None
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def comparer(valeur_odicee, valeur_presta, tolerance=0.01):
    """Retourne (statut, detail) où statut ∈ {"ok", "ecart", "indetermine", "manquant"}."""
    if valeur_odicee in (None, "") and (valeur_presta in (None, "")):
        return "manquant", None
    if valeur_odicee in (None, "") or valeur_presta in (None, ""):
        return "manquant", None

    d_od, d_pr = normalise_date(valeur_odicee), normalise_date(valeur_presta)
    if d_od is not None and d_pr is not None:
        return ("ok", None) if d_od == d_pr else ("ecart", None)

    n_od, n_pr = normalise_nombre(valeur_odicee), normalise_nombre(valeur_presta)
    if n_od is not None and n_pr is not None:
        return ("ok", None) if abs(n_od - n_pr) <= tolerance else ("ecart", None)

    t_od, t_pr = normalise_texte(valeur_odicee), normalise_texte(valeur_presta)
    if t_od == t_pr:
        return "ok", None
    if t_od in t_pr or t_pr in t_od:
        return "indetermine", None
    ratio = difflib.SequenceMatcher(None, t_od, t_pr).ratio()
    if ratio >= 0.90:
        return "indetermine", None
    return "ecart", None


def _parse_table_values(champ_table):
    if not isinstance(champ_table, dict):
        return []
    raw = champ_table.get("values")
    if not raw:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []


def get_odicee_lots_bar(data):
    lots_par_fiche = {}
    for site in data.get("sites", []) or []:
        for lot in site.get("lotsTravaux", []) or []:
            fd = lot.get("formData", {}) or {}
            ref = str(fd.get("reference", "")).upper()
            if "BAR" not in ref:
                continue
            lots_par_fiche.setdefault(fd.get("reference", ""), []).append(lot)
    return lots_par_fiche


def get_presta_doc(report, doc_type):
    for doc in report.get("documents", []) or []:
        if doc.get("type") == doc_type:
            return doc
    return None


def get_presta_doc_alias(report, doc_type):
    for dt in TYPES_ALTERNATIFS.get(doc_type, [doc_type]):
        doc = get_presta_doc(report, dt)
        if doc:
            return doc
    return None


def get_presta_doc_par_regle(report, rule_id):
    for r in report.get("globalRules", []) or []:
        if r.get("ruleId") == rule_id and r.get("evidence"):
            nom_fichier = r["evidence"]
            for doc in report.get("documents", []) or []:
                if doc.get("fileName") == nom_fichier:
                    return doc
    return None


def get_presta_technical_value(report, doc_type, cle_presta):
    for dt in TYPES_ALTERNATIFS.get(doc_type, [doc_type]):
        for doc in report.get("documents", []) or []:
            if doc.get("type") != dt:
                continue
            tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
            if tf.get(cle_presta) not in (None, ""):
                return tf[cle_presta]
    return None


def trouver_professionnel_installateur(data, lot, siret_cible=None):
    prof = lot.get("professionnel") or {}
    titulaire = lot.get("professionnelTitulaireSigneQualite")
    titulaire = titulaire if isinstance(titulaire, dict) else {}
    sous_traitant = lot.get("professionnelSousTraitant")
    sous_traitant = sous_traitant if isinstance(sous_traitant, dict) else {}
    installateurs_dossier = []
    sirets_vus = set()
    for dp in data.get("dossierProfessionnels", []) or []:
        if dp.get("type") == "INSTALLATEUR":
            p = dp.get("professionnel") or {}
            if p.get("siret") and p["siret"] not in sirets_vus:
                sirets_vus.add(p["siret"])
                installateurs_dossier.append(p)
    candidats = [prof, titulaire, sous_traitant] + installateurs_dossier
    if siret_cible:
        for c in candidats:
            if c.get("siret") == siret_cible:
                return c
    if titulaire.get("siret"):
        return titulaire
    if len(installateurs_dossier) == 1:
        return installateurs_dossier[0]
    return prof


def calculer_fiabilite(data, presta):
    """Calcule le résumé (nb_concordant, nb_ecart, nb_partiel, nb_absent) pour une paire
    (JSON Odicee, JSON Prestataire). Retourne None si la fiche/le lot correspondant n'a pas
    été trouvé (dossiers non comparables)."""
    report = presta.get("report") or {}
    fiche_presta = str(report.get("barReference", "")).upper()

    lots_par_fiche = get_odicee_lots_bar(data)
    fiche_match = next((f for f in lots_par_fiche if f.upper() == fiche_presta), None)
    if not fiche_match:
        return None
    lot = lots_par_fiche[fiche_match][0]
    fd = lot.get("formData", {}) or {}
    ref_upper = fiche_match.upper()

    compte = {"ok": 0, "ecart": 0, "indetermine": 0, "manquant": 0}

    def _ajouter(statut):
        compte[statut if statut in compte else "manquant"] += 1

    # ── Identité (hors Site) ──
    doc_engagement = (
        get_presta_doc_par_regle(report, "DOSSIER_HAS_ENGAGEMENT")
        or get_presta_doc(report, "EngagementAct") or get_presta_doc(report, "PurchaseOrder")
        or get_presta_doc(report, "ServiceOrder") or get_presta_doc(report, "LetterOfCommand")
        or get_presta_doc(report, "Quote")
    )
    doc_realisation = (
        get_presta_doc_par_regle(report, "DOSSIER_HAS_COMPLETION")
        or get_presta_doc_alias(report, "Invoice")
    )

    date_eng_od = data.get("dateEngagementReelle")
    date_eng_od = datetime.fromtimestamp(date_eng_od / 1000).date() if date_eng_od else None
    date_eng_pr = (doc_engagement or {}).get("extractedFields", {}).get("documentDate") or \
                  (doc_engagement or {}).get("extractedFields", {}).get("signatureDate")
    _ajouter(comparer(date_eng_od, date_eng_pr)[0])

    date_real_od = data.get("dateRealisationReelle")
    date_real_od = datetime.fromtimestamp(date_real_od / 1000).date() if date_real_od else None
    date_real_pr = (doc_realisation or {}).get("extractedFields", {}).get("documentDate")
    _ajouter(comparer(date_real_od, date_real_pr)[0])

    adresse_od = " ".join(filter(None, [
        fd.get("adresse_travaux", ""), fd.get("code_postal", ""), fd.get("ville", "")
    ])) or None
    adresse_pr = (doc_realisation or {}).get("extractedFields", {}).get("worksAddress") or \
                 (doc_engagement or {}).get("extractedFields", {}).get("worksAddress")
    _ajouter(comparer(adresse_od, adresse_pr)[0])

    siret_pr = (doc_realisation or {}).get("extractedFields", {}).get("siret")
    titulaire = trouver_professionnel_installateur(data, lot, siret_pr)
    _ajouter(comparer(titulaire.get("siret"), siret_pr)[0])

    # ── Données techniques ──
    lots_meme_fiche = lots_par_fiche[fiche_match]

    if "BAR-TH-106" in ref_upper:
        lignes_table = _parse_table_values(fd.get("Puissance"))
        if lignes_table:
            r0 = lignes_table[0]
            paires = [
                (r0[0] if len(r0) > 0 else None, get_presta_technical_value(report, "Invoice", "boilerBrand")),
                (r0[3] if len(r0) > 3 else None, get_presta_technical_value(report, "Invoice", "etasPercent")),
                (r0[4] if len(r0) > 4 else None, get_presta_technical_value(report, "Invoice", "regulatorBrand")),
                (ROMAIN_VERS_ARABE.get(r0[5], r0[5]) if len(r0) > 5 else None,
                 get_presta_technical_value(report, "Invoice", "regulatorClass")),
            ]
        else:
            paires = [
                (fd.get("marque_chaudiere"), get_presta_technical_value(report, "Invoice", "boilerBrand")),
                (fd.get("reference_chaudiere"), get_presta_technical_value(report, "Invoice", "boilerReference")),
                (fd.get("efficacite_energetique"), get_presta_technical_value(report, "Invoice", "etasPercent")),
                (fd.get("marque_regulateur"), get_presta_technical_value(report, "Invoice", "regulatorBrand")),
                (fd.get("reference_regulateur"), get_presta_technical_value(report, "Invoice", "regulatorReference")),
                (fd.get("surface_habitable"), get_presta_technical_value(report, "Invoice", "surfaceHabitable")),
            ]
        for v_od, v_pr in paires:
            _ajouter(comparer(v_od, v_pr)[0])

    elif "BAR-TH-158" in ref_upper:
        lignes_od = _parse_table_values(fd.get("Equipements"))
        total_od = sum(normalise_nombre(r[3]) or 0 for r in lignes_od if len(r) > 3)
        total_pr = 0
        for doc in report.get("documents", []) or []:
            if doc.get("type") != "Invoice":
                continue
            tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
            total_pr += normalise_nombre(tf.get("quantity")) or 0
        _ajouter(comparer(total_od, total_pr)[0])

    else:
        if "BAR-EN-104" in ref_upper:
            regles_fiche = champs_en104(data.get("dateEngagementReelle"))
        else:
            regles_fiche = REGLES.get(fiche_match) or next(
                (v for k, v in REGLES.items() if k in ref_upper), None
            )
        mapping_fiche = FIELD_MAPPING.get(fiche_match) or next(
            (v for k, v in FIELD_MAPPING.items() if k in ref_upper), None
        )
        if regles_fiche and mapping_fiche:
            for cle_od, label, unite, critique in regles_fiche:
                if cle_od not in mapping_fiche:
                    continue
                cle_pr = mapping_fiche[cle_od]
                if cle_od in CHAMPS_CUMULABLES and len(lots_meme_fiche) > 1:
                    valeurs_lots = [normalise_nombre(l.get("formData", {}).get(cle_od)) for l in lots_meme_fiche]
                    valeur_od = sum(valeurs_lots) if all(v is not None for v in valeurs_lots) else fd.get(cle_od)
                else:
                    valeur_od = fd.get(cle_od)
                valeur_pr = get_presta_technical_value(report, "Invoice", cle_pr)
                _ajouter(comparer(valeur_od, valeur_pr)[0])

    return {
        "nb_concordant": compte["ok"],
        "nb_ecart": compte["ecart"],
        "nb_partiel": compte["indetermine"],
        "nb_absent": compte["manquant"],
    }
