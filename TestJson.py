"""
Comparateur Odicee <-> Prestataire IA
======================================

Objectif : le prestataire IA génère un JSON d'analyse par dossier (extraction des
documents + vérification de règles), mais peine à comparer correctement ses valeurs
extraites avec le JSON Odicee de référence. Cette app fait ce rapprochement
champ par champ, en réutilisant le référentiel de champs de la supervision manuelle
(core/utils_supervision.py) plutôt que de le redéfinir.

Entrées :
  1. Le JSON Odicee du dossier (export brut, celui utilisé par 3_Supervision_Dossier.py)
  2. Le JSON produit par le prestataire (report.documents[].extractedFields...)

Sortie : un tableau champ par champ (valeur Odicee vs valeur(s) extraite(s) par
document prestataire), avec les écarts mis en évidence, + les vérifications
d'identité du dossier (n° dossier, fiche, adresse, dates, SIRET).

NOTE : la table FIELD_MAPPING ci-dessous n'est construite avec certitude que pour
BAR-EN-101 (seul exemple de JSON prestataire vu à ce jour). Les autres fiches sont
pré-remplies par analogie de nom de champ (cf. REGLES dans utils_supervision.py) mais
à valider dès qu'un JSON prestataire réel pour ces fiches sera disponible — elles sont
signalées comme telles dans l'UI.
"""

import streamlit as st
import json
import re
import unicodedata
from datetime import datetime
import pytz

import io
import zipfile
import pandas as pd

import fitz  # PyMuPDF
from PIL import Image

try:
    import pytesseract
    pytesseract.get_tesseract_version()
    OCR_DISPONIBLE = True
except ImportError:
    OCR_DISPONIBLE = False
except Exception:
    # pytesseract présent mais binaire système absent (ex: packages.txt manquant sur
    # Streamlit Cloud) — on désactive l'OCR plutôt que de planter l'app.
    OCR_DISPONIBLE = False

from utils import REGLES, decoder_valeur, seuil_r_en101, champs_en104

st.set_page_config(page_title="Comparateur Odicee / Prestataire", layout="wide")

PARIS_TZ = pytz.timezone("Europe/Paris")


# ─────────────────────────────────────────────
# MAPPING CHAMP ODICEE (formData) <-> CHAMP PRESTATAIRE (technicalFields)
# ─────────────────────────────────────────────
# Pour chaque fiche : { cle_odicee: cle_prestataire }
# Construit à partir de REGLES (utils_supervision.py) pour les libellés/unités/criticité,
# et complété ici avec le nom du champ tel qu'il ressort du JSON prestataire.

FIELD_MAPPING = {
    # Confirmé sur JSON prestataire réel (mêmes clés technicalFields sur EN-101/102/103/105).
    "BAR-EN-101": {
        "surface": "surfaceM2",
        "resistance_thermique": "thermalResistance",
        "marque_isolant": "brand",
        "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm",
        "date_visite_pro": "preVisitDate",
    },
    "BAR-EN-102": {
        "surface": "surfaceM2",
        "resistance_thermique": "thermalResistance",
        "marque_isolant": "brand",
        "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm",
        "date_visite_pro": "preVisitDate",
    },
    "BAR-EN-103": {
        "surface": "surfaceM2",
        "resistance_thermique": "thermalResistance",
        "marque_isolant": "brand",
        "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm",
        "date_visite_pro": "preVisitDate",
    },
    "BAR-EN-105": {
        "surface": "surfaceM2",
        "resistance_thermique_non_exported": "thermalResistance",
        "marque_isolant": "brand",
        "reference_isolant": "productReference",
        "epaisseur_isolant": "thicknessMm",
    },
    # EN-104 : type_fenetre non mappé (pas de correspondance fiable côté prestataire entre
    # "doubleWindow"/"installLocation" et le codage 0/1/2 Odicee) — à vérifier manuellement.
    "BAR-EN-104": {
        "coefficient_surfacique": "uw",
        "facteur_solaire_sw": "sw",
        "marque_fenetre": "brand",
        "reference_fenetre": "productReference",
        "surface_fenetres": "surfaceM2",
        "nombre_de_fenetres_ou_portefenetres": "quantity",
    },
    # BAR-TH-110 : "hasLowTempMention" (mention basse température) n'a pas d'équivalent
    # structuré côté Odicee — non comparable automatiquement.
    "BAR-TH-110": {
        "marque_radiateurs": "brand",
        "reference_radiateurs": "productReference",
        "nb_radiateurs": "quantity",
        "delta_temperature": "dtNomKelvin",
    },
    # BAR-TH-127 : seules les marques/références sont comparées automatiquement — les quantités
    # et puissances prestataire (caissonsQty, weightedAbsorbedPower...) recoupent des notions
    # calculées côté Odicee (puissance_individuelle/collective, unités différentes selon le
    # type d'installation) : à vérifier manuellement pour l'instant.
    "BAR-TH-127": {
        "marque_caisson": "caissonsBrand",
        "reference_caisson": "caissonsReference",
        "marque_bouches_entree_air": "entreesAirBrand",
        "reference_bouches_entree_air": "entreesAirReference",
        "marque_bouches_extraction": "bouchesBrand",
        "reference_bouches_extraction": "bouchesReference",
    },
    # BAR-TH-106 et BAR-TH-158 : structure en tableau (multi-équipements) côté Odicee, gérées
    # à part par comparer_th106() et comparer_th158() plus bas — volontairement absentes d'ici.
}

# Classe régulateur : côté Odicee toujours en chiffre romain (colonnes directes décodées via
# decoder_valeur, ou 6e colonne du tableau "Puissance"), côté prestataire en chiffre arabe.
ROMAIN_VERS_ARABE = {"IV": "4", "V": "5", "VI": "6", "VII": "7", "VIII": "8"}


def _parse_table_values(champ_table):
    """Parse un champ Odicee de type tableau multi-lignes (structure {'values': '[[...]]', ...}).
    Retourne la liste de lignes (chaque ligne = liste de valeurs), ou [] si non exploitable."""
    if not isinstance(champ_table, dict):
        return []
    raw = champ_table.get("values")
    if not raw:
        return []
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []


def comparer_th106(fd, report):
    """BAR-TH-106 : deux structures Odicee possibles.
    - type_logement == 1 (individuel) : colonnes directes (marque_chaudiere, etc.)
    - type_logement == 2 (collectif) : tableau 'Puissance', une ligne par type de chaudière
      = [marque+référence fusionnées, quantité, puissance kW, ETAS %, marque régulateur, classe régulateur].
    Retourne (lignes_comparaison, note) où note signale un cas à vérifier à la main (ex.
    plusieurs lignes dans le tableau, ce que la comparaison automatique ne fait pas)."""
    lignes_table = _parse_table_values(fd.get("Puissance"))
    note = None

    if lignes_table:
        if len(lignes_table) > 1:
            note = (
                f"⚠️ {len(lignes_table)} lignes dans le tableau Odicee 'Puissance' (plusieurs types "
                "de chaudières) — seule la 1ère ligne est comparée automatiquement ci-dessous, "
                "vérifiez les autres manuellement."
            )
        r0 = lignes_table[0]
        odicee_vals = {
            "boiler": r0[0] if len(r0) > 0 else None,
            "quantite": r0[1] if len(r0) > 1 else None,
            "puissance_kw": r0[2] if len(r0) > 2 else None,
            "etas": r0[3] if len(r0) > 3 else None,
            "regulateur": r0[4] if len(r0) > 4 else None,
            "classe": ROMAIN_VERS_ARABE.get(r0[5], r0[5]) if len(r0) > 5 else None,
        }
    else:
        odicee_vals = {
            "boiler": fd.get("marque_chaudiere"),
            "quantite": None,
            "puissance_kw": None,
            "etas": fd.get("efficacite_energetique"),
            "regulateur": fd.get("marque_regulateur"),
            "classe": ROMAIN_VERS_ARABE.get(
                decoder_valeur("BAR-TH-106", "classe_regulateur", fd.get("classe_regulateur")),
                fd.get("classe_regulateur"),
            ),
        }

    champs = [
        ("Marque/référence chaudière", "boiler", "boilerBrand"),
        ("Quantité chaudières", "quantite", "quantity"),
        ("Puissance nominale (kW)", "puissance_kw", "nominalPowerKw"),
        ("ETAS (%)", "etas", "etasPercent"),
        ("Marque régulateur", "regulateur", "regulatorBrand"),
        ("Classe régulateur", "classe", "regulatorClass"),
    ]

    lignes = []
    for label, cle_od, cle_pr in champs:
        valeur_od = odicee_vals.get(cle_od)
        valeurs_pr = {}
        for dt in DOC_TYPES_TECHNIQUES:
            v, _ = get_presta_technical_value(report, dt, cle_pr)
            valeurs_pr[dt] = v
        lignes.append((label, valeur_od, valeurs_pr))
    return lignes, note


def comparer_th158(fd, report):
    """BAR-TH-158 : tableau Odicee 'Equipements' (marque, référence, n° certif NF, quantité,
    puissance W) à recouper avec un ou plusieurs documents Invoice côté prestataire (une facture
    par type d'émetteur, dans cet exemple). Pas de correspondance ligne-à-ligne fiable (ordre non
    garanti) : on affiche les deux tableaux côte à côte + un contrôle de cohérence sur le total
    des quantités, à charge de l'utilisateur de rapprocher visuellement les lignes."""
    lignes_odicee = _parse_table_values(fd.get("Equipements"))
    odicee_rows_brut = [
        {
            "Marque": r[0] if len(r) > 0 else None,
            "Référence": r[1] if len(r) > 1 else None,
            "N° certif NF": r[2] if len(r) > 2 else None,
            "Quantité": r[3] if len(r) > 3 else None,
            "Puissance (W)": r[4] if len(r) > 4 else None,
        }
        for r in lignes_odicee
    ]

    presta_rows_brut = []
    for doc in report.get("documents", []) or []:
        if doc.get("type") != "Invoice":
            continue
        tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
        presta_rows_brut.append({
            "Document": doc.get("fileName"),
            "Marque": tf.get("brand"),
            "Référence": tf.get("productReference"),
            "Quantité": tf.get("quantity"),
            "Puissance (W)": tf.get("powerW"),
        })

    total_od = sum(
        normalise_nombre(r["Quantité"]) or 0 for r in odicee_rows_brut
    )
    total_pr = sum(
        normalise_nombre(r["Quantité"]) or 0 for r in presta_rows_brut
    )

    # Conversion systématique en texte pour l'affichage : évite les erreurs de sérialisation
    # de st.table/pyarrow quand une colonne mélange int/str/None entre les lignes (Odicee
    # renvoie des nombres bruts, le prestataire des chaînes — parfois absentes).
    def _en_texte(rows):
        return [{k: ("" if v is None else str(v)) for k, v in row.items()} for row in rows]

    odicee_rows = _en_texte(odicee_rows_brut)
    presta_rows = _en_texte(presta_rows_brut)
    return odicee_rows, presta_rows, total_od, total_pr

# Types de documents prestataire dont les technicalFields portent des valeurs "techniques"
# comparables au formData Odicee (on ignore VisaRequest/RgeCertificate qui n'en ont pas).
DOC_TYPES_TECHNIQUES = ["Invoice", "HonorAttestation"]
LABEL_DOC_TYPE = {
    "HonorAttestation": "AH (prestataire)",
    "Invoice": "Facture (prestataire)",
    "LetterOfCommand": "Bon de commande (prestataire)",
    "VisaRequest": "Visa (prestataire)",
    "RgeCertificate": "RGE (prestataire)",
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def fmt_ts(ts):
    if ts:
        return datetime.fromtimestamp(ts / 1000.0, PARIS_TZ).strftime("%d/%m/%Y")
    return None


def normalise_texte(v):
    if v is None:
        return ""
    s = str(v).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"\s+", " ", s)
    return s


def normalise_nombre(v):
    """Convertit en float uniquement si la valeur EST un nombre (avec unité éventuelle en
    fin de chaîne, ex: '7.65 m².K/W' ou '25 kW'). Ne fait PAS de comparaison numérique sur
    une référence produit/texte contenant un chiffre au milieu (ex: 'UK04', 'EL 000 UK04')
    — ces cas doivent rester une comparaison texte pour ne pas produire de faux écarts."""
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
    """Tente de parser une date sous plusieurs formats courants (JJ/MM/AAAA, AAAA-MM-JJ...).
    Retourne un objet date ou None si non reconnu."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def fmt_date_any(v):
    """Réaffiche en JJ/MM/AAAA toute valeur reconnue comme une date (le prestataire renvoie
    ses dates en AAAA-MM-JJ, format Odicee) ; renvoie la valeur telle quelle si ce n'en est
    pas une (texte, nombre...), pour rester sans effet sur les autres champs."""
    d = normalise_date(v)
    return d.strftime("%d/%m/%Y") if d else v


def date_str_vers_ts_ms(texte):
    """Inverse de fmt_ts : convertit une date affichée (JJ/MM/AAAA ou autre format reconnu par
    normalise_date) en timestamp millisecondes minuit Europe/Paris, pour réécriture dans le
    JSON Odicee (dateEngagementReelle / dateRealisationReelle). None si non reconnu."""
    d = normalise_date(texte)
    if not d:
        return None
    dt = PARIS_TZ.localize(datetime(d.year, d.month, d.day))
    return int(dt.timestamp() * 1000)


def comparer(valeur_odicee, valeur_presta, tolerance=0.01):
    """Retourne (statut, detail) où statut ∈ {"ok", "ecart", "indeterminé", "manquant"}."""
    if valeur_odicee in (None, "") and (valeur_presta in (None, "")):
        return "manquant", "Absent des deux côtés"
    if valeur_odicee in (None, "") or valeur_presta in (None, ""):
        return "manquant", "Absent d'un des deux côtés"

    d_od, d_pr = normalise_date(valeur_odicee), normalise_date(valeur_presta)
    if d_od is not None and d_pr is not None:
        return ("ok", None) if d_od == d_pr else ("ecart", f"{d_od} ≠ {d_pr}")

    n_od, n_pr = normalise_nombre(valeur_odicee), normalise_nombre(valeur_presta)
    if n_od is not None and n_pr is not None:
        if abs(n_od - n_pr) <= tolerance:
            return "ok", None
        return "ecart", f"{n_od} ≠ {n_pr}"

    t_od, t_pr = normalise_texte(valeur_odicee), normalise_texte(valeur_presta)
    if t_od == t_pr:
        return "ok", None
    if t_od in t_pr or t_pr in t_od:
        return "indetermine", "Correspondance partielle — à vérifier visuellement"
    return "ecart", f"« {valeur_odicee} » ≠ « {valeur_presta} »"


def badge(statut):
    return {
        "ok": "🟢",
        "ecart": "🔴",
        "indetermine": "🟡",
        "manquant": "⚪",
    }.get(statut, "⚪")


def get_odicee_lots_bar(data):
    """Reprend la même extraction que 3_Supervision_Dossier.py : tous les lots BAR
    de tous les sites, regroupés par référence de fiche."""
    lots_par_fiche = {}
    for site in data.get("sites", []) or []:
        num_site = site.get("numero", "")
        voie = site.get("nomVoie", "")
        cp = site.get("codePostal", "")
        ville = site.get("ville", "")
        adresse_site = " ".join(filter(None, [str(num_site), voie, cp, ville])) or "Site sans adresse"
        for lot in site.get("lotsTravaux", []) or []:
            fd = lot.get("formData", {}) or {}
            ref = str(fd.get("reference", "")).upper()
            if "BAR" not in ref:
                continue
            lots_par_fiche.setdefault(fd.get("reference", ""), []).append((lot, adresse_site))
    return lots_par_fiche


def get_presta_technical_value(report, doc_type, cle_presta):
    for doc in report.get("documents", []) or []:
        if doc.get("type") != doc_type:
            continue
        tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
        if cle_presta in tf:
            return tf[cle_presta], doc.get("fileName")
        ef = doc.get("extractedFields") or {}
        if cle_presta in ef:
            return ef[cle_presta], doc.get("fileName")
    return None, None


def caster_comme_original(valeur_originale, nouvelle_valeur_texte):
    """Convertit une valeur éditée (toujours du texte côté data_editor) dans le même type
    que la valeur Odicee d'origine, pour ne pas corrompre le JSON (ex: un nombre stocké en
    int ne doit pas devenir une chaîne après édition). Repli sur le texte tel quel si la
    conversion échoue."""
    if isinstance(valeur_originale, bool):
        return nouvelle_valeur_texte
    if isinstance(valeur_originale, int):
        try:
            return int(float(str(nouvelle_valeur_texte).replace(",", ".")))
        except (TypeError, ValueError):
            return nouvelle_valeur_texte
    if isinstance(valeur_originale, float):
        try:
            return float(str(nouvelle_valeur_texte).replace(",", "."))
        except (TypeError, ValueError):
            return nouvelle_valeur_texte
    return nouvelle_valeur_texte


def get_presta_doc(report, doc_type):
    for doc in report.get("documents", []) or []:
        if doc.get("type") == doc_type:
            return doc
    return None


def get_presta_doc_par_regle(report, rule_id):
    """Retrouve le document identifié par le prestataire lui-même comme preuve d'engagement
    ou de réalisation, via ses globalRules (DOSSIER_HAS_ENGAGEMENT / DOSSIER_HAS_COMPLETION)
    dont le champ 'evidence' contient le nom du fichier concerné. Plus fiable qu'une liste de
    types de documents à deviner : le document "preuve d'engagement" est tantôt un acte
    d'engagement (EngagementAct), un bon de commande (PurchaseOrder), un ordre de service
    (ServiceOrder)... selon le dossier — et ce n'est JAMAIS le VisaRequest, qui date la demande
    de contrôle et non l'engagement des travaux."""
    for r in report.get("globalRules", []) or []:
        if r.get("ruleId") == rule_id and r.get("evidence"):
            nom_fichier = r["evidence"]
            for doc in report.get("documents", []) or []:
                if doc.get("fileName") == nom_fichier:
                    return doc
    return None


# ─────────────────────────────────────────────
# SURLIGNAGE PDF (repris de 5_Surlignage_PDF.py)
# ─────────────────────────────────────────────

COULEUR_PRESTA = (1, 0.85, 0)       # jaune — trouvée uniquement côté prestataire
COULEUR_ODICEE_PDF = (0.9, 0.2, 0.2)  # rouge — trouvée uniquement côté Odicee
COULEUR_ACCORD = (0.2, 0.75, 0.3)   # vert  — trouvée aux deux endroits (même position)

CHAMPS_IGNORES_PDF = {"confidences", "rgeQualifications", "hasOwnerSignature", "hasOwnerStamp",
                       "ownerSignatureVision"}

DOC_TYPES_EXCLUS_SURLIGNAGE = {"HonorAttestation"}  # jamais surlignée : déclaration signée, pas une pièce probante


def valeurs_presta_document(doc_presta):
    """Aplatit extractedFields (+ technicalFields) d'un document prestataire en (label, valeur)
    exploitable pour la recherche, en écartant listes/dicts/booléens et valeurs trop courtes."""
    ef = doc_presta.get("extractedFields") or {}
    valeurs = []
    for cle, val in ef.items():
        if cle in CHAMPS_IGNORES_PDF:
            continue
        if cle == "technicalFields" and isinstance(val, dict):
            for cle2, val2 in val.items():
                if val2 not in (None, "") and not isinstance(val2, (dict, list, bool)):
                    valeurs.append((cle2, val2))
            continue
        if val not in (None, "") and not isinstance(val, (dict, list, bool)):
            valeurs.append((cle, val))
    return [(l, v) for l, v in valeurs if len(str(v).strip()) >= 3]


def valeurs_odicee_dossier_pdf(fd, lot):
    """Valeurs Odicee repérables sur un PDF : champs techniques du lot (formData) + identité
    du professionnel. Pas de notion de document associé côté Odicee — cherché sur tous les PDF."""
    valeurs = []
    EXCLUS = {
        "reference", "version", "sme", "titre", "count_html_block_A", "validate_requireds",
        "is_age_batiment_plus_que_deux_ans_auto_filled", "is_multiple_entry",
        "professionnel_titulaire_signe_qualite", "coefficient_zone_a",
    }
    for cle, val in fd.items():
        if cle in EXCLUS:
            continue
        if val not in (None, "") and not isinstance(val, (dict, list, bool)):
            valeurs.append((cle, val))
    prof = lot.get("professionnel") or {}
    if prof.get("siret"):
        valeurs.append(("SIRET professionnel", prof["siret"]))
    if prof.get("raisonSociale"):
        valeurs.append(("Raison sociale professionnel", prof["raisonSociale"]))
    return [(l, v) for l, v in valeurs if len(str(v).strip()) >= 3]


def tokenize_pdf(s):
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", s)


def chaines_recherche_texte(valeur):
    """Chaînes littérales pour page.search_for() : la valeur telle quelle + ses formats de
    date usuels."""
    chaines = [str(valeur).strip()]
    s = str(valeur).strip()
    for fmt_in in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt_in).date()
            chaines.append(d.strftime("%d/%m/%Y"))
            chaines.append(d.strftime("%d-%m-%Y"))
            break
        except ValueError:
            continue
    vues, uniques = set(), []
    for c in chaines:
        if c and c not in vues:
            vues.add(c)
            uniques.append(c)
    return uniques


def variantes_valeur_pdf(valeur):
    """Variantes de tokens pour l'OCR : la valeur telle quelle + représentations JJ/MM/AAAA."""
    variantes = [tokenize_pdf(valeur)]
    s = str(valeur).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            d = datetime.strptime(s, fmt).date()
            variantes.append([str(d.day), str(d.month), str(d.year)])
            variantes.append([f"{d.day:02d}", f"{d.month:02d}", str(d.year)])
        except ValueError:
            pass
    vues, uniques = set(), []
    for v in variantes:
        t = tuple(v)
        if t and t not in vues:
            vues.add(t)
            uniques.append(v)
    return uniques


@st.cache_data(show_spinner=False)
def ocr_page_words(pdf_bytes, page_index, zoom=3, lang="fra"):
    """OCR une page (mis en cache par PDF + index de page)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_index]
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    try:
        data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        data = pytesseract.image_to_data(img, lang="eng", output_type=pytesseract.Output.DICT)
    except pytesseract.pytesseract.TesseractNotFoundError:
        return []
    mots = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        if not txt:
            continue
        r = fitz.Rect(
            data["left"][i], data["top"][i],
            data["left"][i] + data["width"][i], data["top"][i] + data["height"][i],
        ) * (1 / zoom)
        mots.append({"text": txt, "rect": [r.x0, r.y0, r.x1, r.y1]})
    return mots


def trouver_bbox_ocr(mots, valeur):
    """Cherche une valeur dans les mots OCR (séquence de tokens contigus, ou correspondance
    de préfixe pour les identifiants longs mal lus comme un SIRET)."""
    flat = []
    for wi, w in enumerate(mots):
        for tok in tokenize_pdf(w["text"]):
            flat.append((tok, wi))
    toks_only = [t for t, _ in flat]

    for cand in variantes_valeur_pdf(valeur):
        n = len(cand)
        if n == 0:
            continue
        for start in range(len(toks_only) - n + 1):
            if toks_only[start:start + n] == cand:
                widx = {flat[start + k][1] for k in range(n)}
                rects = [fitz.Rect(mots[i]["rect"]) for i in widx]
                r0 = rects[0]
                for r in rects[1:]:
                    r0 |= r
                return r0

    val_tokens = tokenize_pdf(valeur)
    if len(val_tokens) == 1 and len(val_tokens[0]) >= 6:
        v = val_tokens[0]
        for tok, wi in flat:
            if len(tok) >= 6 and tok[:6] == v[:6]:
                return fitz.Rect(mots[wi]["rect"])
    return None


def _rects_pour_valeur(page, pdf_bytes, valeur, ocr_active):
    a_texte = len(page.get_text().strip()) > 20
    if a_texte:
        for chaine in chaines_recherche_texte(valeur):
            rects = page.search_for(chaine)
            if rects:
                return rects
        return []
    elif ocr_active and OCR_DISPONIBLE:
        mots = ocr_page_words(pdf_bytes, page.number)
        bbox = trouver_bbox_ocr(mots, valeur)
        return [bbox] if bbox else []
    return []


def _se_chevauchent(r1, r2, marge=3):
    r1e = fitz.Rect(r1.x0 - marge, r1.y0 - marge, r1.x1 + marge, r1.y1 + marge)
    return r1e.intersects(r2)


def surligner_pdf(pdf_bytes, valeurs_presta, valeurs_odicee, ocr_active=True):
    """PDF (bytes) surligné, coloré selon la ou les source(s) qui confirment une valeur au
    même endroit : 🟩 vert = Odicee + prestataire, 🟥 rouge = Odicee seul, 🟨 jaune = prestataire
    seul. valeurs_presta / valeurs_odicee : listes de (label, valeur)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    trouvees, non_trouvees = [], []
    valeurs_od_restantes = set(range(len(valeurs_odicee)))
    valeurs_pr_restantes = set(range(len(valeurs_presta)))

    for page in doc:
        od_hits = [
            (i, valeurs_odicee[i], r)
            for i in range(len(valeurs_odicee))
            for r in _rects_pour_valeur(page, pdf_bytes, valeurs_odicee[i][1], ocr_active)
        ]
        pr_hits = [
            (i, valeurs_presta[i], r)
            for i in range(len(valeurs_presta))
            for r in _rects_pour_valeur(page, pdf_bytes, valeurs_presta[i][1], ocr_active)
        ]

        pr_utilises = set()
        for idx_od, (label_od, val_od), r_od in od_hits:
            valeurs_od_restantes.discard(idx_od)
            correspond = None
            for j, (idx_pr, (label_pr, val_pr), r_pr) in enumerate(pr_hits):
                if j in pr_utilises:
                    continue
                if _se_chevauchent(r_od, r_pr):
                    correspond = j
                    break
            if correspond is not None:
                pr_utilises.add(correspond)
                valeurs_pr_restantes.discard(pr_hits[correspond][0])
                couleur = COULEUR_ACCORD
            else:
                couleur = COULEUR_ODICEE_PDF
            annot = page.add_highlight_annot(r_od)
            annot.set_colors(stroke=couleur)
            annot.set_opacity(0.4)
            annot.update()

        for j, (idx_pr, (label_pr, val_pr), r_pr) in enumerate(pr_hits):
            if j in pr_utilises:
                continue
            valeurs_pr_restantes.discard(idx_pr)
            annot = page.add_highlight_annot(r_pr)
            annot.set_colors(stroke=COULEUR_PRESTA)
            annot.set_opacity(0.4)
            annot.update()

    for i, (label, valeur) in enumerate(valeurs_odicee):
        (non_trouvees if i in valeurs_od_restantes else trouvees).append((label, valeur))
    for i, (label, valeur) in enumerate(valeurs_presta):
        (non_trouvees if i in valeurs_pr_restantes else trouvees).append((label, valeur))

    return doc.write(), trouvees, non_trouvees


def trouver_fichier_zip(zip_files, nom_cible):
    """Retrouve un fichier du ZIP correspondant au fileName du prestataire, en tolérant les
    écarts d'espaces/casse/tirets entre les deux systèmes."""
    def normaliser(n):
        return re.sub(r"[^a-z0-9]", "", n.lower())
    cible_norm = normaliser(nom_cible)
    for nom in zip_files:
        if normaliser(nom) == cible_norm:
            return nom
    for nom in zip_files:
        if normaliser(nom.rsplit(".", 1)[0]) in cible_norm or cible_norm in normaliser(nom):
            return nom
    return None


# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────

st.title("🔀 Comparateur — Odicee vs Prestataire IA")
st.caption(
    "Importez le JSON Odicee du dossier et le JSON produit par le prestataire pour "
    "confronter les valeurs extraites champ par champ."
)

st.sidebar.header("🔗 Raccourci API")
num_dossier_sidebar = st.sidebar.text_input("Numéro de dossier Odicee (ex: T239148)")
if num_dossier_sidebar:
    num_clean = re.sub(r"\D", "", num_dossier_sidebar)
    if num_clean:
        st.sidebar.markdown(
            f'<a href="https://odicee.edf.fr/api/dossiers/{num_clean}" target="_blank">➡️ JSON Odicee {num_clean}</a>',
            unsafe_allow_html=True,
        )
        st.sidebar.caption("Ctrl+S sur la page pour sauvegarder, puis importez ci-dessous.")

st.sidebar.markdown("---")
id_presta_sidebar = st.sidebar.text_input(
    "ID Prestataire (identifiant technique, pas le n° de dossier — ex: ce850dc69bf643b3ba4973f9aaf682d4)"
)
if id_presta_sidebar:
    st.sidebar.markdown(
        f'<a href="https://docminddev.promotelec-services.com/api/dossiers/{id_presta_sidebar.strip()}" target="_blank">➡️ JSON Prestataire</a>',
        unsafe_allow_html=True,
    )
    st.sidebar.caption(
        "Cet identifiant n'est pas déductible du n° de dossier — une fois le JSON prestataire "
        "chargé ci-dessous, le lien correspondant s'affiche automatiquement."
    )

col_up1, col_up2 = st.columns(2)
with col_up1:
    fichier_odicee = st.file_uploader("JSON Odicee (dossier)", type="json", key="odicee")
with col_up2:
    fichier_presta = st.file_uploader("JSON Prestataire (rapport d'analyse)", type="json", key="presta")

if not (fichier_odicee and fichier_presta):
    st.info("Chargez les deux fichiers pour lancer la comparaison.")
    st.stop()

try:
    data = json.load(fichier_odicee)
except Exception as e:
    st.error(f"JSON Odicee invalide : {e}")
    st.stop()

try:
    presta = json.load(fichier_presta)
except Exception as e:
    st.error(f"JSON Prestataire invalide : {e}")
    st.stop()

report = presta.get("report") or {}
documents_presta = report.get("documents", []) or []
fiche_presta = str(report.get("barReference", "")).upper()
filenumber_presta = str(presta.get("fileNumber") or report.get("fileNumber") or "")

# ── Vérification d'identité dossier ──
st.markdown("### 🪪 Identification du dossier")
dossier_id = str(data.get("id", ""))
prefixe = data.get("prefixe", "") or ""
id_odicee = f"{prefixe}{dossier_id}"
id_presta_clean = re.sub(r"^\D+", "", filenumber_presta)

c1, c2, c3 = st.columns(3)
c1.metric("N° dossier Odicee", id_odicee)
c2.metric("N° dossier Prestataire", filenumber_presta or "—")
match_id = dossier_id == id_presta_clean
c3.markdown(f"**Correspondance**\n\n{'🟢 OK' if match_id else '🔴 Écart — vérifier le rapprochement'}")

if dossier_id:
    url_odicee = f"https://odicee.edf.fr/api/dossiers/{dossier_id}"
    liens = [f'<a href="{url_odicee}" target="_blank">🔗 Ouvrir le JSON Odicee (API)</a>']
    id_technique_presta = presta.get("id")
    if id_technique_presta:
        url_presta = f"https://docminddev.promotelec-services.com/api/dossiers/{id_technique_presta}"
        liens.append(f'<a href="{url_presta}" target="_blank">🔗 Ouvrir le JSON Prestataire (API)</a>')
    st.markdown("&nbsp;&nbsp;·&nbsp;&nbsp;".join(liens), unsafe_allow_html=True)

if not match_id:
    st.warning(
        "⚠️ Le numéro de dossier du rapport prestataire ne correspond pas au JSON Odicee chargé. "
        "Vérifiez que vous comparez bien le même dossier avant d'interpréter les écarts ci-dessous."
    )

st.markdown("---")

# ── Sélection du lot Odicee correspondant à la fiche du rapport prestataire ──
lots_par_fiche = get_odicee_lots_bar(data)
fiche_odicee_match = next((f for f in lots_par_fiche if f.upper() == fiche_presta), None)

if not fiche_odicee_match:
    st.error(
        f"Aucun lot Odicee avec la fiche **{fiche_presta or '—'}** trouvé dans ce dossier. "
        f"Fiches disponibles côté Odicee : {', '.join(lots_par_fiche) or '—'}."
    )
    st.stop()

lots_sites = lots_par_fiche[fiche_odicee_match]
if len(lots_sites) > 1:
    adresses = [a for _, a in lots_sites]
    choix = st.selectbox("Plusieurs sites pour cette fiche — choisir celui à comparer :", adresses)
    lot, adresse_site = next((l, a) for l, a in lots_sites if a == choix)
else:
    lot, adresse_site = lots_sites[0]

fd = lot.get("formData", {}) or {}

st.markdown(f"### 📋 Fiche comparée : **{fiche_odicee_match}** — {adresse_site}")

# ── Comparaison des dates & identité chantier (niveau dossier) ──
st.markdown("#### 📅 Dates & identité chantier")
doc_engagement = (
    get_presta_doc_par_regle(report, "DOSSIER_HAS_ENGAGEMENT")
    or get_presta_doc(report, "EngagementAct")
    or get_presta_doc(report, "PurchaseOrder")
    or get_presta_doc(report, "ServiceOrder")
    or get_presta_doc(report, "LetterOfCommand")
)
doc_realisation = (
    get_presta_doc_par_regle(report, "DOSSIER_HAS_COMPLETION")
    or get_presta_doc(report, "Invoice")
)

rows_identite = []
modifications_odicee = []  # (fiche, cle_od, ancienne_valeur, nouvelle_valeur) — édité par l'utilisateur

date_eng_odicee = fmt_ts(data.get("dateEngagementReelle"))
date_eng_presta = (doc_engagement or {}).get("extractedFields", {}).get("documentDate") or \
                   (doc_engagement or {}).get("extractedFields", {}).get("signatureDate")
rows_identite.append(("Date d'engagement", date_eng_odicee, fmt_date_any(date_eng_presta)))

date_real_odicee = fmt_ts(data.get("dateRealisationReelle"))
date_real_presta = (doc_realisation or {}).get("extractedFields", {}).get("documentDate")
rows_identite.append(("Date de réalisation", date_real_odicee, fmt_date_any(date_real_presta)))

adresse_fd = " ".join(filter(None, [
    fd.get("adresse_travaux", ""), fd.get("code_postal", ""), fd.get("ville", "")
])) or None
adresse_presta = (doc_realisation or {}).get("extractedFields", {}).get("worksAddress")
rows_identite.append(("Adresse des travaux", adresse_fd, adresse_presta))

prof = lot.setdefault("professionnel", {})  # référence réelle dans `lot`/`data`, pas une copie
siret_odicee = prof.get("siret")
siret_presta = (doc_realisation or {}).get("extractedFields", {}).get("siret")
rows_identite.append(("SIRET professionnel", siret_odicee, siret_presta))

lignes_html = []
for label, v_od, v_pr in rows_identite:
    statut, detail = comparer(v_od, v_pr, tolerance=0)
    lignes_html.append((badge(statut), label, v_od if v_od else "—", v_pr if v_pr else "—", detail or ""))

df_identite = pd.DataFrame(
    {
        "": [l[0] for l in lignes_html],
        "Champ": [l[1] for l in lignes_html],
        "Odicee": [l[2] for l in lignes_html],
        "Prestataire": [l[3] for l in lignes_html],
        "Détail écart": [l[4] for l in lignes_html],
    }
)
df_identite_edite = st.data_editor(
    df_identite,
    disabled=["", "Champ", "Prestataire", "Détail écart"],
    column_config={"Odicee": st.column_config.TextColumn("Odicee ✏️")},
    hide_index=True,
    key=f"editeur_identite_{fiche_odicee_match}_{adresse_site}",
)
st.caption(
    "✏️ Colonne Odicee modifiable. Dates au format JJ/MM/AAAA. L'adresse remplace le champ "
    "« adresse des travaux » d'Odicee dans son intégralité (code postal/ville restent séparés "
    "et ne sont pas modifiables ici)."
)

for i, label in enumerate(df_identite_edite["Champ"]):
    valeur_orig = lignes_html[i][2]
    valeur_editee = df_identite_edite["Odicee"].iloc[i]
    if str(valeur_editee) == str(valeur_orig):
        continue
    if label == "Date d'engagement":
        ts = date_str_vers_ts_ms(valeur_editee)
        if ts is not None:
            data["dateEngagementReelle"] = ts
            modifications_odicee.append((fiche_odicee_match, "dateEngagementReelle", valeur_orig, valeur_editee))
        else:
            st.warning(f"Date d'engagement « {valeur_editee} » non reconnue (attendu JJ/MM/AAAA) — non enregistrée.")
    elif label == "Date de réalisation":
        ts = date_str_vers_ts_ms(valeur_editee)
        if ts is not None:
            data["dateRealisationReelle"] = ts
            modifications_odicee.append((fiche_odicee_match, "dateRealisationReelle", valeur_orig, valeur_editee))
        else:
            st.warning(f"Date de réalisation « {valeur_editee} » non reconnue (attendu JJ/MM/AAAA) — non enregistrée.")
    elif label == "Adresse des travaux":
        fd["adresse_travaux"] = valeur_editee
        modifications_odicee.append((fiche_odicee_match, "adresse_travaux", valeur_orig, valeur_editee))
    elif label == "SIRET professionnel":
        prof["siret"] = valeur_editee
        modifications_odicee.append((fiche_odicee_match, "professionnel.siret", valeur_orig, valeur_editee))

# ── Comparaison technique champ par champ ──
st.markdown("#### 🔧 Données techniques")

ref_upper = fiche_odicee_match.upper()
export_technique = None  # rempli par chaque branche ci-dessous, utilisé pour l'export Excel

if "BAR-TH-106" in ref_upper:
    lignes_th106, note_th106 = comparer_th106(fd, report)
    if note_th106:
        st.info(note_th106)
    docs_presents = [dt for dt in DOC_TYPES_TECHNIQUES if get_presta_doc(report, dt)]
    entete = ["", "Champ", "Odicee"] + [LABEL_DOC_TYPE[dt] for dt in docs_presents]
    lignes = {c: [] for c in entete}
    for label, valeur_od, valeurs_pr in lignes_th106:
        statuts = [comparer(valeur_od, valeurs_pr.get(dt))[0] for dt in docs_presents]
        ordre_gravite = {"ecart": 0, "indetermine": 1, "manquant": 2, "ok": 3}
        pire = min(statuts, key=lambda s: ordre_gravite[s]) if statuts else "manquant"
        lignes[""].append(badge(pire))
        lignes["Champ"].append(label)
        lignes["Odicee"].append(valeur_od if valeur_od not in (None, "") else "—")
        for dt in docs_presents:
            v = valeurs_pr.get(dt)
            lignes[LABEL_DOC_TYPE[dt]].append(fmt_date_any(v) if v not in (None, "") else "—")
    st.table(lignes)
    st.caption(
        "🟢 valeurs concordantes · 🔴 écart net · 🟡 correspondance partielle · ⚪ absent d'un côté. "
        "Classe régulateur comparée en chiffre arabe (Odicee est en chiffre romain)."
    )
    export_technique = {"type": "table", "titre": "Données techniques", "df": pd.DataFrame(lignes)}

elif "BAR-TH-158" in ref_upper:
    odicee_rows, presta_rows, total_od, total_pr = comparer_th158(fd, report)
    statut_total, detail_total = comparer(total_od, total_pr, tolerance=0)
    st.markdown(
        f"{badge(statut_total)} **Total quantité émetteurs** — Odicee : {total_od:g} · "
        f"Prestataire (somme des factures) : {total_pr:g}"
    )
    if statut_total != "ok":
        st.caption(detail_total or "")
    st.caption(
        "Pas de correspondance ligne-à-ligne automatique (l'ordre des lignes n'est pas garanti) — "
        "rapprochez visuellement marque/référence/quantité entre les deux tableaux ci-dessous."
    )
    col_od, col_pr = st.columns(2)
    with col_od:
        st.markdown("**Odicee — tableau Equipements ✏️**")
        try:
            if odicee_rows:
                df_od_th158 = st.data_editor(
                    pd.DataFrame(odicee_rows),
                    hide_index=True,
                    key=f"editeur_th158_{fiche_odicee_match}_{adresse_site}",
                )
                lignes_modifiees = df_od_th158.to_dict("records") != odicee_rows
                if lignes_modifiees and isinstance(fd.get("Equipements"), dict):
                    nouvelles_lignes = []
                    for r in df_od_th158.to_dict("records"):
                        qte = caster_comme_original(1, r.get("Quantité", ""))
                        puissance = caster_comme_original(1, r.get("Puissance (W)", ""))
                        nouvelles_lignes.append([
                            r.get("Marque", ""), r.get("Référence", ""), r.get("N° certif NF", ""),
                            qte, puissance,
                        ])
                    fd["Equipements"]["values"] = json.dumps(nouvelles_lignes, ensure_ascii=False)
                    modifications_odicee.append(
                        (fiche_odicee_match, "Equipements", odicee_rows, df_od_th158.to_dict("records"))
                    )
                    # Recalcule le total affiché plus haut avec les nouvelles quantités
                    total_od = sum(normalise_nombre(r.get("Quantité")) or 0 for r in df_od_th158.to_dict("records"))
                odicee_rows = df_od_th158.to_dict("records")
            else:
                st.caption("Aucune ligne.")
        except Exception as e:
            st.error(f"Affichage impossible pour ce tableau ({e}).")
            st.write(odicee_rows)
    with col_pr:
        st.markdown("**Prestataire — factures**")
        try:
            if presta_rows:
                st.table(presta_rows)
            else:
                st.caption("Aucune ligne.")
        except Exception as e:
            st.error(f"Affichage impossible pour ce tableau ({e}).")
            st.write(presta_rows)
    export_technique = {
        "type": "th158",
        "titre": "Equipements",
        "odicee_df": pd.DataFrame(odicee_rows),
        "presta_df": pd.DataFrame(presta_rows),
        "total_odicee": total_od,
        "total_presta": total_pr,
    }

else:
    if "BAR-EN-104" in ref_upper:
        regles_fiche = champs_en104(data.get("dateEngagementReelle"))
    else:
        regles_fiche = REGLES.get(fiche_odicee_match) or next(
            (v for k, v in REGLES.items() if k in ref_upper), None
        )

    mapping_fiche = FIELD_MAPPING.get(fiche_odicee_match) or next(
        (v for k, v in FIELD_MAPPING.items() if k in ref_upper), None
    )

    if not mapping_fiche:
        st.warning(
            f"Aucun mapping de champs défini pour **{fiche_odicee_match}** vers le format prestataire. "
            "Ajoutez-le dans FIELD_MAPPING une fois un JSON prestataire réel disponible pour cette fiche."
        )
    elif not regles_fiche:
        st.warning(f"Fiche **{fiche_odicee_match}** absente de REGLES (utils_supervision.py).")
    else:
        docs_presents = [dt for dt in DOC_TYPES_TECHNIQUES if get_presta_doc(report, dt)]
        if not docs_presents:
            st.warning("Aucun document AH/Facture exploitable dans le JSON prestataire.")
        else:
            entete = ["", "Champ", "Odicee"] + [LABEL_DOC_TYPE[dt] for dt in docs_presents]
            lignes = {c: [] for c in entete}
            # Parallèle à `lignes` : clé formData brute et indicateur "champ encodé" (valeur
            # affichée décodée par decoder_valeur, ex: type de pose 0/1 -> texte) pour chaque
            # ligne éditable — on ne permet pas la modification des champs encodés ici, le
            # risque de réinjecter un texte au lieu du code numérique attendu par Odicee est
            # trop élevé pour un simple champ texte.
            cles_od_lignes = []
            encode_lignes = []

            for cle_od, label, unite, critique in regles_fiche:
                if cle_od not in mapping_fiche:
                    continue
                cle_pr = mapping_fiche[cle_od]
                valeur_od = fd.get(cle_od)
                valeur_od_dec = decoder_valeur(fiche_odicee_match, cle_od, valeur_od)

                valeurs_pr = {}
                for dt in docs_presents:
                    v, _fname = get_presta_technical_value(report, dt, cle_pr)
                    valeurs_pr[dt] = v

                # Statut global de la ligne = pire statut parmi les documents comparés
                statuts = []
                for dt in docs_presents:
                    statut, _ = comparer(valeur_od, valeurs_pr[dt])
                    statuts.append(statut)
                ordre_gravite = {"ecart": 0, "indetermine": 1, "manquant": 2, "ok": 3}
                pire = min(statuts, key=lambda s: ordre_gravite[s]) if statuts else "manquant"

                lignes[""].append(badge(pire))
                lignes["Champ"].append(f"{label}" + (f" ({unite})" if unite else ""))
                lignes["Odicee"].append(
                    f"{valeur_od_dec}" if valeur_od_dec not in (None, "") else "—"
                )
                for dt in docs_presents:
                    v = valeurs_pr[dt]
                    lignes[LABEL_DOC_TYPE[dt]].append(fmt_date_any(v) if v not in (None, "") else "—")

                cles_od_lignes.append(cle_od)
                encode_lignes.append(str(valeur_od_dec) != str(valeur_od))

            if lignes["Champ"]:
                df_lignes = pd.DataFrame(lignes)
                colonnes_verrouillees = [c for c in df_lignes.columns if c != "Odicee"]
                # Les champs encodés (liste déroulante Odicee ex: type de pose) restent en
                # lecture seule : data_editor ne permet pas de désactiver une cellule isolée.
                df_lignes["_editable"] = [not e for e in encode_lignes]

                df_edite = st.data_editor(
                    df_lignes.drop(columns=["_editable"]),
                    disabled=colonnes_verrouillees + (["Odicee"] if all(encode_lignes) else []),
                    column_config={
                        "Odicee": st.column_config.TextColumn(
                            "Odicee ✏️" if not all(encode_lignes) else "Odicee"
                        )
                    },
                    hide_index=True,
                    key=f"editeur_technique_{fiche_odicee_match}_{adresse_site}",
                )
                st.caption(
                    "🟢 valeurs concordantes · 🔴 écart net · 🟡 correspondance partielle (à vérifier "
                    "visuellement, ex. texte tronqué/reformaté) · ⚪ champ absent d'un des deux côtés. "
                    "✏️ Colonne Odicee modifiable (les champs à liste déroulante — type de pose, "
                    "classe de régulateur... — restent en lecture seule ici)."
                )

                for i, cle_od in enumerate(cles_od_lignes):
                    if encode_lignes[i]:
                        continue
                    valeur_originale = fd.get(cle_od)
                    valeur_affichee_orig = lignes["Odicee"][i]
                    valeur_editee = df_edite["Odicee"].iloc[i]
                    if str(valeur_editee) != str(valeur_affichee_orig):
                        fd[cle_od] = caster_comme_original(valeur_originale, valeur_editee)
                        modifications_odicee.append((fiche_odicee_match, cle_od, valeur_affichee_orig, valeur_editee))

                export_technique = {"type": "table", "titre": "Données techniques", "df": df_edite}
            else:
                st.caption("Aucun champ mappé n'a de correspondance exploitable.")


# ── Règles de conformité déjà calculées par le prestataire (pour contexte) ──
with st.expander("📜 Règles de conformité du prestataire (pour information)"):
    global_rules = report.get("globalRules", []) or []
    non_conformes = [r for r in global_rules if r.get("status") != "Compliant"]
    if non_conformes:
        for r in non_conformes:
            icone = "🔴" if r.get("status") == "NonCompliant" else "🟡"
            st.markdown(f"{icone} **{r.get('ruleId')}** — {r.get('message')}")
    else:
        st.caption("Aucune non-conformité signalée par le prestataire.")


# ─────────────────────────────────────────────
# EXPORT DU RAPPORT (Excel)
# ─────────────────────────────────────────────

def construire_rapport_excel():
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        # ── Feuille unique "Comparaison" : identité dossier + administratif + technique ──
        df_admin = pd.DataFrame(
            [{"Champ": "Site", "Odicee": adresse_site, "Prestataire": "", "Détail écart": ""}]
            + [
                {"Champ": label, "Odicee": v_od, "Prestataire": v_pr, "Détail écart": detail}
                for _, label, v_od, v_pr, detail in lignes_html
            ]
        )

        ligne_courante = 0
        feuille = "Comparaison"
        pd.DataFrame(
            [{"Dossier": f"{id_odicee} (Odicee) / {filenumber_presta} (Prestataire)", "Fiche": fiche_odicee_match}]
        ).to_excel(writer, sheet_name=feuille, index=False, startrow=ligne_courante)
        ligne_courante += 3

        df_admin.to_excel(writer, sheet_name=feuille, index=False, startrow=ligne_courante)
        ligne_courante += len(df_admin) + 3

        if export_technique:
            if export_technique["type"] == "table":
                pd.DataFrame([{"Champ": "── Données techniques ──"}]).to_excel(
                    writer, sheet_name=feuille, index=False, header=False, startrow=ligne_courante
                )
                ligne_courante += 1
                export_technique["df"].to_excel(writer, sheet_name=feuille, index=False, startrow=ligne_courante)
            elif export_technique["type"] == "th158":
                pd.DataFrame([{"Champ": "── Equipements Odicee ──"}]).to_excel(
                    writer, sheet_name=feuille, index=False, header=False, startrow=ligne_courante
                )
                ligne_courante += 1
                export_technique["odicee_df"].to_excel(
                    writer, sheet_name=feuille, index=False, startrow=ligne_courante
                )
                ligne_courante += len(export_technique["odicee_df"]) + 3

                pd.DataFrame([{"Champ": "── Equipements Prestataire ──"}]).to_excel(
                    writer, sheet_name=feuille, index=False, header=False, startrow=ligne_courante
                )
                ligne_courante += 1
                export_technique["presta_df"].to_excel(
                    writer, sheet_name=feuille, index=False, startrow=ligne_courante
                )
                ligne_courante += len(export_technique["presta_df"]) + 3

                pd.DataFrame(
                    {
                        "Total": ["Odicee", "Prestataire"],
                        "Quantité": [export_technique["total_odicee"], export_technique["total_presta"]],
                    }
                ).to_excel(writer, sheet_name=feuille, index=False, startrow=ligne_courante)

        df_regles = pd.DataFrame(
            [
                {"Statut": r.get("status"), "Règle": r.get("ruleId"), "Message": r.get("message")}
                for r in non_conformes
            ]
        ) if non_conformes else pd.DataFrame(columns=["Statut", "Règle", "Message"])
        df_regles.to_excel(writer, sheet_name="Non-conformités prestataire", index=False)

    return buffer.getvalue()


colex1, colex2 = st.columns(2)
with colex1:
    st.download_button(
        "📥 Télécharger le rapport (Excel)",
        data=construire_rapport_excel(),
        file_name=f"comparaison_{id_odicee}_{fiche_odicee_match}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
with colex2:
    if modifications_odicee:
        st.download_button(
            "📥 Télécharger le JSON Odicee modifié",
            data=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name=f"{id_odicee}_modifie.json",
            mime="application/json",
        )
    else:
        st.caption("Aucune modification apportée aux valeurs Odicee — rien à réexporter en JSON.")

if modifications_odicee:
    with st.expander(f"✏️ {len(modifications_odicee)} modification(s) apportée(s) aux valeurs Odicee"):
        for fiche_mod, cle_mod, avant, apres in modifications_odicee:
            st.caption(f"**{cle_mod}** ({fiche_mod}) : {avant} → {apres}")


# ─────────────────────────────────────────────
# SURLIGNAGE PDF (optionnel — même analyse que ci-dessus, appliquée aux PDF du dossier)
# ─────────────────────────────────────────────

st.markdown("---")
st.markdown("## 🖍️ Surlignage PDF (optionnel)")
st.caption(
    "Déposez le ZIP des pièces jointes de ce dossier pour surligner, directement sur les PDF, "
    "les valeurs Odicee et prestataire déjà comparées ci-dessus — "
    "🟩 trouvée aux deux endroits · 🟥 Odicee seul · 🟨 prestataire seul."
)

fichier_zip_surlignage = st.file_uploader("ZIP des PDF du dossier", type="zip", key="zip_surlignage")

if fichier_zip_surlignage:
    if not OCR_DISPONIBLE:
        st.warning(
            "⚠️ Tesseract OCR n'est pas disponible sur ce serveur — seuls les PDF avec calque "
            "texte pourront être surlignés (les PDF scannés seront ignorés).\n\n"
            "**Sur Streamlit Community Cloud** : ajoutez un fichier `packages.txt` à la racine "
            "du repo contenant `tesseract-ocr` et `tesseract-ocr-fra`, puis redémarrez l'app "
            "(Manage app → Reboot).\n\n"
            "**Sur un serveur classique** : `apt install tesseract-ocr tesseract-ocr-fra`."
        )

    try:
        zf = zipfile.ZipFile(fichier_zip_surlignage)
        pdfs_zip = {n: zf.read(n) for n in zf.namelist() if n.lower().endswith(".pdf")}
    except Exception as e:
        st.error(f"ZIP invalide : {e}")
        pdfs_zip = None

    if pdfs_zip is not None:
        documents_surlignables = [
            d for d in documents_presta if d.get("type") not in DOC_TYPES_EXCLUS_SURLIGNAGE
        ]
        if not documents_surlignables:
            st.warning("Aucun document surlignable dans ce rapport (l'attestation sur l'honneur est exclue).")
        else:
            valeurs_odicee_pdf = valeurs_odicee_dossier_pdf(fd, lot)

            noms_docs_pdf = [d.get("fileName") for d in documents_surlignables if d.get("fileName")]
            doc_choisi_nom_pdf = st.selectbox("Document à surligner :", noms_docs_pdf, key="doc_surlignage")
            doc_choisi_pdf = next(d for d in documents_surlignables if d.get("fileName") == doc_choisi_nom_pdf)

            nom_fichier_zip_pdf = trouver_fichier_zip(pdfs_zip, doc_choisi_nom_pdf)
            if not nom_fichier_zip_pdf:
                st.error(
                    f"Le fichier **{doc_choisi_nom_pdf}** annoncé par le prestataire est introuvable "
                    f"dans le ZIP. Fichiers disponibles : {', '.join(pdfs_zip) or '—'}"
                )
            else:
                valeurs_presta_pdf = valeurs_presta_document(doc_choisi_pdf)

                with st.expander(f"🟨 {len(valeurs_presta_pdf)} valeur(s) prestataire recherchée(s) sur ce document"):
                    st.write({l: v for l, v in valeurs_presta_pdf})
                with st.expander(f"🟥 {len(valeurs_odicee_pdf)} valeur(s) Odicee recherchée(s) (tous documents)"):
                    st.write({l: v for l, v in valeurs_odicee_pdf})

                pdf_bytes_sel = pdfs_zip[nom_fichier_zip_pdf]
                nb_pages_sel = fitz.open(stream=pdf_bytes_sel, filetype="pdf").page_count
                a_calque_texte_sel = any(
                    len(fitz.open(stream=pdf_bytes_sel, filetype="pdf")[i].get_text().strip()) > 20
                    for i in range(nb_pages_sel)
                )
                if not a_calque_texte_sel:
                    st.caption(
                        f"📄 Document scanné ({nb_pages_sel} page(s)) — traitement par OCR, "
                        "peut prendre quelques secondes."
                    )

                with st.spinner("Surlignage en cours..."):
                    pdf_annote_sel, trouvees_sel, non_trouvees_sel = surligner_pdf(
                        pdf_bytes_sel, valeurs_presta_pdf, valeurs_odicee_pdf
                    )

                cc1, cc2 = st.columns(2)
                cc1.metric("Valeurs localisées", len(trouvees_sel))
                cc2.metric("Valeurs non localisées", len(non_trouvees_sel))
                if non_trouvees_sel:
                    with st.expander("⚪ Valeurs non localisées sur ce document"):
                        for l, v in non_trouvees_sel:
                            st.caption(f"{l} : {v}")

                st.download_button(
                    "📥 Télécharger ce PDF surligné",
                    data=pdf_annote_sel,
                    file_name=f"surligne_{doc_choisi_nom_pdf}",
                    mime="application/pdf",
                )

                st.caption(
                    "Génère un ZIP avec chaque document du dossier surligné (hors attestation "
                    "sur l'honneur). Peut prendre du temps si plusieurs PDF sont scannés."
                )
                if st.button("Générer le ZIP de tous les documents surlignés"):
                    zip_buffer_sel = io.BytesIO()
                    with st.spinner("Surlignage de tous les documents en cours..."):
                        with zipfile.ZipFile(zip_buffer_sel, "w", zipfile.ZIP_DEFLATED) as zout:
                            barre_sel = st.progress(0.0)
                            for i, doc_p in enumerate(documents_surlignables):
                                nom_p = doc_p.get("fileName")
                                nom_zip_p = trouver_fichier_zip(pdfs_zip, nom_p)
                                if not nom_zip_p:
                                    st.caption(f"⚠️ {nom_p} introuvable dans le ZIP — ignoré.")
                                    continue
                                pdf_bytes_p = pdfs_zip[nom_zip_p]
                                valeurs_presta_p = valeurs_presta_document(doc_p)
                                pdf_annote_p, _, _ = surligner_pdf(pdf_bytes_p, valeurs_presta_p, valeurs_odicee_pdf)
                                zout.writestr(f"surligne_{nom_p}", pdf_annote_p)
                                barre_sel.progress((i + 1) / len(documents_surlignables))
                    st.download_button(
                        "📥 Télécharger le ZIP",
                        data=zip_buffer_sel.getvalue(),
                        file_name=f"surlignage_{id_odicee}.zip",
                        mime="application/zip",
                    )

                st.markdown("---")
                doc_rendu_sel = fitz.open(stream=pdf_annote_sel, filetype="pdf")
                for i in range(doc_rendu_sel.page_count):
                    pix_sel = doc_rendu_sel[i].get_pixmap(matrix=fitz.Matrix(1.8, 1.8))
                    st.image(
                        pix_sel.tobytes("png"),
                        caption=f"Page {i + 1}/{doc_rendu_sel.page_count}",
                        use_container_width=True,
                    )
