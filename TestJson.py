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

from core.utils_supervision import REGLES, decoder_valeur, seuil_r_en101, champs_en104

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
    odicee_rows = [
        {
            "Marque": r[0] if len(r) > 0 else None,
            "Référence": r[1] if len(r) > 1 else None,
            "N° certif NF": r[2] if len(r) > 2 else None,
            "Quantité": r[3] if len(r) > 3 else None,
            "Puissance (W)": r[4] if len(r) > 4 else None,
        }
        for r in lignes_odicee
    ]

    presta_rows = []
    for doc in report.get("documents", []) or []:
        if doc.get("type") != "Invoice":
            continue
        tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
        presta_rows.append({
            "Document": doc.get("fileName"),
            "Marque": tf.get("brand"),
            "Référence": tf.get("productReference"),
            "Quantité": tf.get("quantity"),
            "Puissance (W)": tf.get("powerW"),
        })

    total_od = sum(
        normalise_nombre(r["Quantité"]) or 0 for r in odicee_rows
    )
    total_pr = sum(
        normalise_nombre(r["Quantité"]) or 0 for r in presta_rows
    )
    return odicee_rows, presta_rows, total_od, total_pr

# Types de documents prestataire dont les technicalFields portent des valeurs "techniques"
# comparables au formData Odicee (on ignore VisaRequest/RgeCertificate qui n'en ont pas).
DOC_TYPES_TECHNIQUES = ["HonorAttestation", "Invoice"]
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


def get_presta_doc(report, doc_type):
    for doc in report.get("documents", []) or []:
        if doc.get("type") == doc_type:
            return doc
    return None


# ─────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────

st.title("🔀 Comparateur — Odicee vs Prestataire IA")
st.caption(
    "Importez le JSON Odicee du dossier et le JSON produit par le prestataire pour "
    "confronter les valeurs extraites champ par champ."
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
doc_engagement = get_presta_doc(report, "LetterOfCommand") or get_presta_doc(report, "VisaRequest")
doc_realisation = get_presta_doc(report, "Invoice")

rows_identite = []

date_eng_odicee = fmt_ts(data.get("dateEngagementReelle"))
date_eng_presta = (doc_engagement or {}).get("extractedFields", {}).get("documentDate") or \
                   (doc_engagement or {}).get("extractedFields", {}).get("signatureDate")
rows_identite.append(("Date d'engagement", date_eng_odicee, date_eng_presta))

date_real_odicee = fmt_ts(data.get("dateRealisationReelle"))
date_real_presta = (doc_realisation or {}).get("extractedFields", {}).get("documentDate")
rows_identite.append(("Date de réalisation", date_real_odicee, date_real_presta))

adresse_fd = " ".join(filter(None, [
    fd.get("adresse_travaux", ""), fd.get("code_postal", ""), fd.get("ville", "")
])) or None
adresse_presta = (doc_realisation or {}).get("extractedFields", {}).get("worksAddress")
rows_identite.append(("Adresse des travaux", adresse_fd, adresse_presta))

prof = lot.get("professionnel") or {}
siret_odicee = prof.get("siret")
siret_presta = (doc_realisation or {}).get("extractedFields", {}).get("siret")
rows_identite.append(("SIRET professionnel", siret_odicee, siret_presta))

lignes_html = []
for label, v_od, v_pr in rows_identite:
    statut, detail = comparer(v_od, v_pr, tolerance=0)
    lignes_html.append((badge(statut), label, v_od if v_od else "—", v_pr if v_pr else "—", detail or ""))

st.table(
    {
        "": [l[0] for l in lignes_html],
        "Champ": [l[1] for l in lignes_html],
        "Odicee": [l[2] for l in lignes_html],
        "Prestataire": [l[3] for l in lignes_html],
        "Détail écart": [l[4] for l in lignes_html],
    }
)

# ── Comparaison technique champ par champ ──
st.markdown("#### 🔧 Données techniques")

ref_upper = fiche_odicee_match.upper()

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
            lignes[LABEL_DOC_TYPE[dt]].append(v if v not in (None, "") else "—")
    st.table(lignes)
    st.caption(
        "🟢 valeurs concordantes · 🔴 écart net · 🟡 correspondance partielle · ⚪ absent d'un côté. "
        "Classe régulateur comparée en chiffre arabe (Odicee est en chiffre romain)."
    )

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
        st.markdown("**Odicee — tableau Equipements**")
        st.table(odicee_rows) if odicee_rows else st.caption("Aucune ligne.")
    with col_pr:
        st.markdown("**Prestataire — factures**")
        st.table(presta_rows) if presta_rows else st.caption("Aucune ligne.")

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
                    lignes[LABEL_DOC_TYPE[dt]].append(v if v not in (None, "") else "—")

            if lignes["Champ"]:
                st.table(lignes)
                st.caption(
                    "🟢 valeurs concordantes · 🔴 écart net · 🟡 correspondance partielle (à vérifier "
                    "visuellement, ex. texte tronqué/reformaté) · ⚪ champ absent d'un des deux côtés."
                )
            else:
                st.caption("Aucun champ mappé n'a de correspondance exploitable.")

    # Champs prestataire non mappés, pour visibilité (aide à compléter FIELD_MAPPING)
    if mapping_fiche and docs_presents:
        with st.expander("🔍 Champs technicalFields du prestataire non rapprochés"):
            cles_mappees = set(mapping_fiche.values())
            for dt in docs_presents:
                doc = get_presta_doc(report, dt)
                tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
                non_mappes = {k: v for k, v in tf.items() if k not in cles_mappees}
                if non_mappes:
                    st.markdown(f"**{LABEL_DOC_TYPE[dt]}** ({doc.get('fileName')})")
                    st.json(non_mappes)

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
