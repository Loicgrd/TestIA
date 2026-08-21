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
import difflib
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

from utils import REGLES, decoder_valeur, seuil_r_en101, champs_en104, CHAMPS_CUMULABLES

try:
    from supabase import create_client
    SUPABASE_DISPONIBLE = True
except ImportError:
    SUPABASE_DISPONIBLE = False


def normaliser_numero_dossier(brut):
    """Normalise un numéro de dossier vers une clé canonique — uniquement les chiffres, sans
    préfixe — quel que soit son format d'origine (avec/sans "T", "CP", "CPC"..., suffixe de
    version "V2", espaces...). On ne garde volontairement PAS le préfixe : il varie selon le
    type de dossier (T, CP, CPC...) et le prestataire l'omet parfois complètement — tenter de
    le deviner ou le forcer produirait de fausses correspondances. Les chiffres seuls suffisent
    à relier de façon fiable un dossier Odicee à son analyse Prestataire dans Supabase."""
    if not brut:
        return brut
    premier_mot = str(brut).strip().split(" ")[0]  # ignore un éventuel suffixe "V2" etc.
    chiffres = re.sub(r"\D", "", premier_mot)
    return chiffres if chiffres else premier_mot


@st.cache_resource
def get_supabase_client():
    """Connexion Supabase (persistance des JSON Odicee entre sessions). Retourne None si les
    secrets ne sont pas configurés (SUPABASE_URL / SUPABASE_KEY) ou si le package n'est pas
    installé — l'app continue de fonctionner sans persistance dans ce cas."""
    if not SUPABASE_DISPONIBLE:
        return None
    try:
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
    except Exception:
        return None
    try:
        return create_client(url, key)
    except Exception:
        return None


def sauvegarder_dossier_odicee(data, fiche=None):
    """Enregistre (ou met à jour) le JSON Odicee dans Supabase, indexé par numéro de dossier.
    Retourne (succès, message) — message explicite en cas d'échec (secrets absents, RLS,
    erreur réseau...) plutôt qu'un échec silencieux impossible à diagnostiquer."""
    client = get_supabase_client()
    if not client:
        return False, "Supabase non configuré (secrets absents/invalides ou package non installé)."
    numero = normaliser_numero_dossier(f"{data.get('prefixe', '') or ''}{data.get('id', '')}")
    if not numero:
        return False, "Impossible de déterminer le numéro de dossier (champs 'id'/'prefixe' absents du JSON)."
    try:
        client.table("dossiers_odicee").upsert({
            "numero_dossier": numero,
            "fiche": fiche,
            "donnees": data,
            "date_maj": datetime.now().isoformat(),
        }).execute()
        return True, None
    except Exception as e:
        return False, str(e)


@st.cache_data(ttl=60, show_spinner=False)
def lister_dossiers_odicee():
    """Liste (numéro, fiche, date de dernière sauvegarde) des dossiers déjà enregistrés,
    triés du plus récent au plus ancien. Cache 60s pour éviter une requête à chaque interaction."""
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = (
            client.table("dossiers_odicee")
            .select("numero_dossier, fiche, date_maj")
            .order("date_maj", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def charger_dossier_odicee(numero_dossier):
    """Recharge le JSON complet d'un dossier précédemment enregistré. Retourne (donnees, message
    d'erreur ou None) pour pouvoir diagnostiquer un échec plutôt que de le masquer."""
    client = get_supabase_client()
    if not client:
        return None, "Supabase non configuré (secrets absents/invalides ou package non installé)."
    try:
        res = client.table("dossiers_odicee").select("donnees").eq("numero_dossier", numero_dossier).single().execute()
        return (res.data["donnees"] if res.data else None), None
    except Exception as e:
        return None, str(e)


def sauvegarder_analyse_prestataire(presta, numero_dossier, fiche):
    """Ajoute un enregistrement d'historique pour cette analyse prestataire — contrairement à
    l'Odicee, on garde toutes les versions pour pouvoir comparer l'évolution dans le temps
    (ex : le prestataire s'améliore-t-il après une correction signalée ?). Si la dernière
    version enregistrée est rigoureusement identique, on ne crée pas de doublon.
    Retourne (succès, message) — message vaut "identique" si rien n'a été réenregistré."""
    client = get_supabase_client()
    if not client:
        return False, "Supabase non configuré (secrets absents/invalides ou package non installé)."
    if not numero_dossier:
        return False, "Numéro de dossier prestataire introuvable (champ 'fileNumber' absent du JSON)."
    report = presta.get("report") or {}
    try:
        dernier = (
            client.table("dossiers_prestataire")
            .select("donnees")
            .eq("numero_dossier", numero_dossier)
            .order("date_ajout", desc=True)
            .limit(1)
            .execute()
        )
        if dernier.data and dernier.data[0]["donnees"] == presta:
            return True, "identique"
        client.table("dossiers_prestataire").insert({
            "numero_dossier": numero_dossier,
            "fiche": fiche,
            "reliability_score": report.get("reliabilityScore"),
            "overall_status": report.get("overallStatus"),
            "donnees": presta,
            "date_analyse": presta.get("analyzedAt"),
        }).execute()
        return True, None
    except Exception as e:
        return False, str(e)


@st.cache_data(ttl=60, show_spinner=False)
def lister_historique_prestataire(numero_dossier):
    """Historique des analyses prestataire pour ce dossier, du plus récent au plus ancien."""
    client = get_supabase_client()
    if not client or not numero_dossier:
        return []
    try:
        res = (
            client.table("dossiers_prestataire")
            .select("id, reliability_score, overall_status, date_analyse, date_ajout")
            .eq("numero_dossier", numero_dossier)
            .order("date_ajout", desc=True)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner=False)
def lister_toutes_analyses_prestataire():
    """Toutes les analyses prestataire enregistrées, tous dossiers confondus, les plus
    récentes en premier — pour le sélecteur de rechargement en haut de page."""
    client = get_supabase_client()
    if not client:
        return []
    try:
        res = (
            client.table("dossiers_prestataire")
            .select("id, numero_dossier, fiche, reliability_score, overall_status, date_analyse, date_ajout")
            .order("date_ajout", desc=True)
            .limit(200)
            .execute()
        )
        return res.data or []
    except Exception:
        return []


def supprimer_dossier_odicee(numero_dossier):
    """Supprime un dossier Odicee enregistré. Retourne (succès, message)."""
    client = get_supabase_client()
    if not client:
        return False, "Supabase non configuré."
    try:
        client.table("dossiers_odicee").delete().eq("numero_dossier", numero_dossier).execute()
        return True, None
    except Exception as e:
        return False, str(e)


def supprimer_analyse_prestataire(id_analyse):
    """Supprime une version d'analyse prestataire enregistrée. Retourne (succès, message)."""
    client = get_supabase_client()
    if not client:
        return False, "Supabase non configuré."
    try:
        client.table("dossiers_prestataire").delete().eq("id", id_analyse).execute()
        return True, None
    except Exception as e:
        return False, str(e)


@st.cache_data(ttl=30, show_spinner=False)
def lister_tous_numeros_connus():
    """Union des numéros de dossier connus côté Odicee et côté Prestataire, pour la recherche
    unifiée en haut de page."""
    client = get_supabase_client()
    if not client:
        return []
    numeros = set()
    try:
        res_od = client.table("dossiers_odicee").select("numero_dossier").execute()
        numeros.update(r["numero_dossier"] for r in (res_od.data or []))
    except Exception:
        pass
    try:
        res_pr = client.table("dossiers_prestataire").select("numero_dossier").execute()
        numeros.update(r["numero_dossier"] for r in (res_pr.data or []))
    except Exception:
        pass
    return sorted(numeros)


def charger_analyse_prestataire(id_analyse):
    """Recharge le JSON complet d'une analyse prestataire archivée, par son id. Retourne
    (donnees, message d'erreur ou None)."""
    client = get_supabase_client()
    if not client:
        return None, "Supabase non configuré (secrets absents/invalides ou package non installé)."
    try:
        res = client.table("dossiers_prestataire").select("donnees").eq("id", id_analyse).single().execute()
        return (res.data["donnees"] if res.data else None), None
    except Exception as e:
        return None, str(e)


def comparer_deux_analyses_prestataire(ancien_presta, nouveau_presta):
    """Compare les technicalFields des documents entre deux analyses prestataire du même
    dossier (ex: avant/après correction), champ par champ. Retourne une liste de
    (champ, valeur_ancienne, valeur_nouvelle, a_change)."""
    def _aplatir(presta):
        plat = {}
        for doc in (presta.get("report") or {}).get("documents", []) or []:
            tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
            for cle, val in tf.items():
                if val not in (None, ""):
                    plat.setdefault(cle, val)  # première occurrence nom-champ toutes docs confondus
        return plat

    anc, nouv = _aplatir(ancien_presta), _aplatir(nouveau_presta)
    tous_champs = sorted(set(anc) | set(nouv))
    lignes = []
    for champ in tous_champs:
        v_anc, v_nouv = anc.get(champ), nouv.get(champ)
        lignes.append((champ, v_anc if v_anc is not None else "—", v_nouv if v_nouv is not None else "—", v_anc != v_nouv))
    return lignes

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
        # Variante des clés Odicee avant le 01/01/2024 (cf. champs_en104 dans utils.py) — même
        # champ prestataire des deux côtés, seule la clé formData change selon la date d'engagement.
        "marque_isolant": "brand",
        "reference_isolant": "productReference",
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
    # BAR-TH-127 : marques/références + surface habitable + puissance individuelle (WThC, même
    # unité que weightedAbsorbedPower côté prestataire — vérifié). puissance_collective reste
    # exclue : exprimée en WThC/m3/h côté Odicee, une unité différente de weightedAbsorbedPower,
    # la comparer directement produirait un faux résultat plutôt qu'une vraie non-concordance.
    "BAR-TH-127": {
        "marque_caisson": "caissonsBrand",
        "reference_caisson": "caissonsReference",
        "marque_bouches_entree_air": "entreesAirBrand",
        "reference_bouches_entree_air": "entreesAirReference",
        "marque_bouches_extraction": "bouchesBrand",
        "reference_bouches_extraction": "bouchesReference",
        "surface_habitable": "surfaceHabitable",
        "puissance_individuelle": "weightedAbsorbedPower",
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
    - type_logement == 1 (individuel) : colonnes directes. Marque et référence sont deux clés
      Odicee SÉPARÉES (marque_chaudiere / reference_chaudiere, marque_regulateur /
      reference_regulateur) — chacune comparée à son équivalent prestataire (boilerBrand /
      boilerReference, regulatorBrand / regulatorReference).
    - type_logement == 2 (collectif) : tableau 'Puissance', une ligne par type de chaudière
      = [marque+référence FUSIONNÉES par Odicee lui-même, quantité, puissance kW, ETAS %,
      marque+référence régulateur FUSIONNÉES, classe régulateur]. Pas de séparation possible
      côté Odicee dans ce cas — comparé à la concaténation marque+référence du prestataire.
    Retourne (lignes_comparaison, note, editable) où note signale un cas à vérifier à la main
    (ex. plusieurs lignes dans le tableau, ce que la comparaison automatique ne fait pas)."""
    lignes_table = _parse_table_values(fd.get("Puissance"))
    note = None

    def _fusionner(marque, reference):
        return " ".join(filter(None, [str(marque or "").strip(), str(reference or "").strip()])).strip() or None

    if lignes_table:
        if len(lignes_table) > 1:
            note = (
                f"⚠️ {len(lignes_table)} lignes dans le tableau Odicee 'Puissance' (plusieurs types "
                "de chaudières) — seule la 1ère ligne est comparée automatiquement ci-dessous, "
                "vérifiez les autres manuellement."
            )
        r0 = lignes_table[0]
        odicee_vals = {
            "chaudiere_fusion": r0[0] if len(r0) > 0 else None,
            "quantite": r0[1] if len(r0) > 1 else None,
            "puissance_kw": r0[2] if len(r0) > 2 else None,
            "etas": r0[3] if len(r0) > 3 else None,
            "regulateur_fusion": r0[4] if len(r0) > 4 else None,
            "classe": ROMAIN_VERS_ARABE.get(r0[5], r0[5]) if len(r0) > 5 else None,
        }
        champs = [
            ("Marque/référence chaudière", "chaudiere_fusion", "boilerBrand+boilerReference", None),
            ("Quantité chaudières", "quantite", "quantity", "nb_equipements"),
            ("ETAS (%)", "etas", "etasPercent", "efficacite_energetique"),
            ("Marque/référence régulateur", "regulateur_fusion", "regulatorBrand+regulatorReference", None),
            ("Classe régulateur", "classe", "regulatorClass", None),
        ]
    else:
        # nb_equipements n'est réellement renseigné par Odicee que lorsque le lot est en saisie
        # multiple (plusieurs chaudières identiques) ; sur un lot simple (1 logement,
        # is_multiple_entry=0) c'est une valeur par défaut (0) non significative.
        # puissance_thermique_nominale n'est PAS une puissance en kW comparable à celle du
        # prestataire : c'est une case Oui/Non ("Puissance ≤ 70 kW ?") encodée en 0/1 côté
        # Odicee individuel — jamais comparée ici pour éviter un faux écart trompeur.
        saisie_multiple = fd.get("is_multiple_entry") == 1
        odicee_vals = {
            "marque_chaudiere": fd.get("marque_chaudiere"),
            "reference_chaudiere": fd.get("reference_chaudiere"),
            "quantite": fd.get("nb_equipements") if saisie_multiple else None,
            "puissance_kw": None,
            "etas": fd.get("efficacite_energetique"),
            "marque_regulateur": fd.get("marque_regulateur"),
            "reference_regulateur": fd.get("reference_regulateur"),
            "surface_habitable": fd.get("surface_habitable"),
            "classe": ROMAIN_VERS_ARABE.get(
                decoder_valeur("BAR-TH-106", "classe_regulateur", fd.get("classe_regulateur")),
                fd.get("classe_regulateur"),
            ),
        }
        champs = [
            ("Marque chaudière", "marque_chaudiere", "boilerBrand", "marque_chaudiere"),
            ("Référence chaudière", "reference_chaudiere", "boilerReference", "reference_chaudiere"),
            ("Quantité chaudières", "quantite", "quantity", "nb_equipements"),
            ("ETAS (%)", "etas", "etasPercent", "efficacite_energetique"),
            ("Marque régulateur", "marque_regulateur", "regulatorBrand", "marque_regulateur"),
            ("Référence régulateur", "reference_regulateur", "regulatorReference", "reference_regulateur"),
            ("Classe régulateur", "classe", "regulatorClass", None),
            ("Surface habitable (m²)", "surface_habitable", "surfaceHabitable", "surface_habitable"),
        ]
        if saisie_multiple:
            note = (
                "ℹ️ Puissance non comparée : Odicee ne stocke ici qu'un seuil Oui/Non "
                "(« ≤ 70 kW ? »), pas une valeur en kW comparable à celle du prestataire."
            )
        else:
            note = (
                "ℹ️ Lot en saisie simple (1 logement, une seule chaudière) : quantité non "
                "comparée (implicitement 1 unité). Puissance non comparée non plus : Odicee ne "
                "stocke qu'un seuil Oui/Non (« ≤ 70 kW ? »), pas une valeur en kW."
            )

    # Édition manuelle possible uniquement dans le cas "individuel" (colonnes directes) : le cas
    # "collectif" (tableau Puissance multi-lignes) n'est pas réinjectable simplement en JSON ici.
    editable = not lignes_table

    lignes = []
    for label, cle_od, cle_pr, cle_ecriture in champs:
        valeur_od = odicee_vals.get(cle_od)
        valeurs_pr = {}
        for dt in DOC_TYPES_TECHNIQUES:
            if "+" in cle_pr:
                # Cas collectif : Odicee fusionne marque+référence, on fusionne pareillement les
                # deux champs prestataire correspondants pour une comparaison équitable.
                cle_pr_marque, cle_pr_ref = cle_pr.split("+")
                v_marque, _ = get_presta_technical_value(report, dt, cle_pr_marque)
                v_ref, _ = get_presta_technical_value(report, dt, cle_pr_ref)
                v = _fusionner(v_marque, v_ref)
            else:
                v, _ = get_presta_technical_value(report, dt, cle_pr)
            valeurs_pr[dt] = v
        lignes.append((label, valeur_od, valeurs_pr, cle_ecriture if editable else None))
    return lignes, note, editable


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
    # Ponctuation courante (virgule, tiret, +, |, parenthèses...) traitée comme un espace : une
    # adresse "109, rue X - 62800 Y" doit matcher "109 rue X 62800 Y", une référence "A + B"
    # doit matcher "A | B" — ce ne sont que des variantes de mise en forme, pas des écarts réels.
    s = re.sub(r"[,;:|+\-()/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
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
    # Tolérance sur les micro-écarts de mise en forme (espace manquant/en trop, caractère isolé
    # différent) qui ne sont pas de vrais écarts de valeur, ex: "PRK 32" vs "PRK32".
    ratio = difflib.SequenceMatcher(None, t_od, t_pr).ratio()
    if ratio >= 0.90:
        return "indetermine", f"Quasi identique ({ratio*100:.0f}% de similarité) — à vérifier visuellement"
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
    de tous les sites, regroupés par référence de fiche. Le libellé de sélection intègre le
    complément d'adresse du lot (ex: "BAT A-B-C-D") quand il existe, pour distinguer deux lots
    de la même fiche situés à la même adresse de site (cas fréquent : un même complexe
    immobilier découpé en plusieurs bâtiments, chacun son propre lot de travaux)."""
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
            complement = fd.get("complement_adresse")
            libelle = f"{adresse_site} — {complement}" if complement else adresse_site
            lots_par_fiche.setdefault(fd.get("reference", ""), []).append((lot, libelle))
    return lots_par_fiche


# Certains dossiers utilisent un type de document alternatif pour la preuve de réalisation
# (ex: "CeeInvoice" au lieu de "Invoice", "FinalSettlement" — décompte général définitif — sur
# les marchés publics, ou "AcceptanceReport" — PV de réception/levée de réserves — quand il n'y
# a pas de facture classique) — parfois même en présence de plusieurs, l'un étant vide côté
# extraction (ex: PVLR vs PVaR). On essaie chaque type candidat dans l'ordre et on garde la
# première valeur non vide trouvée, plutôt que de s'arrêter sur un premier document dont
# l'extraction a échoué.
TYPES_ALTERNATIFS = {"Invoice": ["Invoice", "CeeInvoice", "FinalSettlement", "AcceptanceReport"]}


def get_presta_doc_alias(report, doc_type):
    """Comme get_presta_doc, mais tient compte des types de document alternatifs
    (TYPES_ALTERNATIFS) — pour savoir si "une facture (ou équivalent)" existe dans le dossier,
    peu importe son type exact côté prestataire."""
    for dt in TYPES_ALTERNATIFS.get(doc_type, [doc_type]):
        doc = get_presta_doc(report, dt)
        if doc:
            return doc
    return None


def get_presta_technical_value(report, doc_type, cle_presta):
    for dt in TYPES_ALTERNATIFS.get(doc_type, [doc_type]):
        for doc in report.get("documents", []) or []:
            if doc.get("type") != dt:
                continue
            tf = (doc.get("extractedFields") or {}).get("technicalFields") or {}
            if tf.get(cle_presta) not in (None, ""):
                return tf[cle_presta], doc.get("fileName")
            ef = doc.get("extractedFields") or {}
            if ef.get(cle_presta) not in (None, ""):
                return ef[cle_presta], doc.get("fileName")
    return None, None


def trouver_professionnel_installateur(data, lot, siret_cible=None):
    """Retrouve le professionnel qui a réellement réalisé les travaux (SIRET à comparer aux
    documents prestataire), PAS le maître d'œuvre ni un mandataire/apporteur d'affaire signataire
    de la partie C — ce rôle se loge tantôt dans lot.professionnel, tantôt dans
    lot.professionnelTitulaireSigneQualite selon les dossiers (aucun des deux n'est fiable à
    lui seul : sur certains dossiers professionnel=maître d'œuvre et Titulaire=installateur ;
    sur d'autres c'est l'inverse, Titulaire pointant vers un sous-traitant RGE différent de
    l'entreprise qui a facturé).

    Stratégie : si le SIRET de la facture prestataire est connu, on cherche EN PRIORITÉ lequel
    des candidats Odicee (professionnel du lot, professionnelTitulaireSigneQualite,
    professionnelSousTraitant, dossierProfessionnels de type INSTALLATEUR) correspond réellement
    à ce SIRET — c'est la seule façon de lever l'ambiguïté sans deviner. À défaut de
    correspondance (ou si aucun SIRET cible n'est fourni), on retombe sur professionnelTitulaireSigneQualite
    puis un unique dossierProfessionnels INSTALLATEUR, puis lot.professionnel en dernier recours,
    avec un avertissement si le résultat reste incertain.

    Retourne (dict_professionnel, avertissement_ou_None)."""
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
                return c, None

    if titulaire.get("siret"):
        return titulaire, None
    if len(installateurs_dossier) == 1:
        return installateurs_dossier[0], None
    if len(installateurs_dossier) > 1:
        return prof, (
            f"⚠️ {len(installateurs_dossier)} installateurs différents identifiés au niveau du "
            "dossier, aucun ne correspondant au SIRET de la facture — le SIRET affiché "
            "(professionnel du lot) est incertain. À vérifier manuellement."
        )
    return prof, None


def caster_comme_original(valeur_originale, nouvelle_valeur_texte):
    """Convertit une valeur éditée (toujours du texte côté data_editor) dans le même type
    que la valeur Odicee d'origine, pour ne pas corrompre le JSON (ex: un nombre stocké en
    int ne doit pas devenir une chaîne après édition). Repli sur le texte tel quel si la
    conversion échoue. Si le texte saisi contient une décimale (ex: puissance exacte en kW
    tapée à la main sur un champ normalement entier/booléen côté Odicee), la décimale est
    conservée plutôt que tronquée — l'intention de l'utilisateur prime sur le type d'origine."""
    if isinstance(valeur_originale, bool):
        return nouvelle_valeur_texte
    texte = str(nouvelle_valeur_texte).replace(",", ".")
    if isinstance(valeur_originale, int):
        if "." in texte:
            try:
                return float(texte)
            except (TypeError, ValueError):
                return nouvelle_valeur_texte
        try:
            return int(float(texte))
        except (TypeError, ValueError):
            return nouvelle_valeur_texte
    if isinstance(valeur_originale, float):
        try:
            return float(texte)
        except (TypeError, ValueError):
            return nouvelle_valeur_texte
    return nouvelle_valeur_texte


def get_presta_doc(report, doc_type):
    for doc in report.get("documents", []) or []:
        if doc.get("type") == doc_type:
            return doc
    return None


def get_presta_works_address(report, doc_realisation, doc_engagement):
    """L'adresse des travaux n'est pas toujours renseignée sur la facture (OCR manqué, mention
    absente...) alors qu'elle l'est souvent sur le bon de commande / acte d'engagement. On
    cherche dans l'ordre : document de réalisation, document d'engagement, puis n'importe quel
    autre document du dossier — pour ne pas afficher '—' alors que l'info existe ailleurs.
    Retourne (adresse, nom_du_fichier_source) ou (None, None)."""
    for doc in (doc_realisation, doc_engagement):
        if doc and (doc.get("extractedFields") or {}).get("worksAddress"):
            return doc["extractedFields"]["worksAddress"], doc.get("fileName")
    for doc in report.get("documents", []) or []:
        wa = (doc.get("extractedFields") or {}).get("worksAddress")
        if wa:
            return wa, doc.get("fileName")
    return None, None


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


def valeurs_odicee_dossier_pdf(fd, lot, data, report=None):
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
    siret_facture = None
    if report:
        doc_fact = get_presta_doc(report, "Invoice")
        siret_facture = (doc_fact or {}).get("extractedFields", {}).get("siret")
    titulaire, _avertissement = trouver_professionnel_installateur(data, lot, siret_facture)
    if titulaire.get("siret"):
        valeurs.append(("SIRET professionnel", titulaire["siret"]))
    if titulaire.get("raisonSociale"):
        valeurs.append(("Raison sociale professionnel", titulaire["raisonSociale"]))
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

supabase_ok = SUPABASE_DISPONIBLE and get_supabase_client()

data_rechargee = None
presta_rechargee = None

if supabase_ok:
    try:
        st.markdown("### 🔍 Retrouver un dossier déjà travaillé")

        def _libelle_version(a):
            date_aff = (a.get("date_analyse") or a.get("date_ajout") or "")[:10]
            fiab_aff = f" · fiabilité {a['reliability_score']*100:.0f}%" if a.get("reliability_score") is not None else ""
            return f"{date_aff}{fiab_aff}"

        numeros_connus = lister_tous_numeros_connus()
        numero_recherche = st.selectbox(
            "Numéro de dossier — tapez pour rechercher",
            ["—"] + numeros_connus,
            key="recherche_numero_unifiee",
        )

        if numero_recherche != "—":
            col_r1, col_r2 = st.columns(2)

            # ── Odicee : unique, chargement automatique ──
            with col_r1:
                data_auto, err_od = charger_dossier_odicee(numero_recherche)
                if err_od:
                    st.warning(f"Odicee : {err_od}")
                elif data_auto:
                    data_rechargee = data_auto
                    c1, c2 = st.columns([5, 1])
                    c1.caption(f"✅ Odicee {numero_recherche} chargé automatiquement.")
                    if c2.button("🗑️", key="suppr_od_unifie", help="Supprimer ce dossier Odicee"):
                        ok, err = supprimer_dossier_odicee(numero_recherche)
                        if ok:
                            lister_dossiers_odicee.clear()
                            lister_tous_numeros_connus.clear()
                            st.rerun()
                        else:
                            st.error(f"⚠️ {err}")
                else:
                    st.caption("Aucun JSON Odicee enregistré pour ce dossier — déposez-le ci-dessous.")

            # ── Prestataire : dernière version par défaut, sélecteur si historique ──
            with col_r2:
                historique = lister_historique_prestataire(numero_recherche)
                if historique:
                    version_choisie = historique[0]
                    if len(historique) > 1:
                        options_versions = [f"Dernière — {_libelle_version(historique[0])}"] + [
                            f"Antérieure — {_libelle_version(v)}" for v in historique[1:]
                        ]
                        choix_version = st.selectbox(
                            f"{len(historique)} versions prestataire", options_versions, key="version_pr_unifiee"
                        )
                        version_choisie = historique[options_versions.index(choix_version)]
                    presta_auto, err_pr = charger_analyse_prestataire(version_choisie["id"])
                    if err_pr:
                        st.warning(f"Prestataire : {err_pr}")
                    elif presta_auto:
                        presta_rechargee = presta_auto
                        c1, c2 = st.columns([5, 1])
                        c1.caption(f"✅ Prestataire chargé ({_libelle_version(version_choisie)}).")
                        if c2.button("🗑️", key="suppr_pr_unifie", help="Supprimer cette version prestataire"):
                            ok, err = supprimer_analyse_prestataire(version_choisie["id"])
                            if ok:
                                lister_toutes_analyses_prestataire.clear()
                                lister_tous_numeros_connus.clear()
                                lister_historique_prestataire.clear()
                                st.rerun()
                            else:
                                st.error(f"⚠️ {err}")
                else:
                    st.caption("Aucune analyse prestataire enregistrée pour ce dossier — déposez-la ci-dessous.")

            st.caption(
                "Pour retester ce dossier : le comparatif ci-dessus reflète la dernière analyse "
                "connue — déposez juste le **nouveau** JSON prestataire ci-dessous, il remplace "
                "automatiquement la version chargée pour cette comparaison (et s'ajoute à l'historique)."
            )
    except Exception as _e_recherche:
        st.error(
            f"⚠️ La recherche de dossiers enregistrés a rencontré un problème et a été "
            f"désactivée pour cette page (l'upload manuel ci-dessous reste disponible) : {_e_recherche}"
        )
        st.session_state.pop("recherche_numero_unifiee", None)

    st.markdown("---")

col_up1, col_up2 = st.columns(2)
with col_up1:
    fichier_odicee = st.file_uploader("JSON Odicee (dossier)", type="json", key="odicee")
with col_up2:
    fichier_presta = st.file_uploader("JSON Prestataire (rapport d'analyse)", type="json", key="presta")

if not (fichier_odicee or data_rechargee) or not (fichier_presta or presta_rechargee):
    st.info(
        "Chargez le JSON Odicee et le JSON prestataire (ou recherchez un dossier déjà "
        "enregistré ci-dessus) pour lancer la comparaison."
    )
    st.stop()

if fichier_odicee:
    try:
        data = json.load(fichier_odicee)
    except Exception as e:
        st.error(f"JSON Odicee invalide : {e}")
        st.stop()
else:
    data = data_rechargee

if fichier_presta:
    try:
        presta = json.load(fichier_presta)
    except Exception as e:
        st.error(f"JSON Prestataire invalide : {e}")
        st.stop()
else:
    presta = presta_rechargee

report = presta.get("report") or {}
documents_presta = report.get("documents", []) or []
fiche_presta = str(report.get("barReference", "")).upper()
filenumber_presta = str(presta.get("fileNumber") or report.get("fileNumber") or "")
# Le prestataire n'inclut pas toujours le préfixe "T" (ex: "176444" au lieu de "T176444") et
# ajoute parfois un suffixe de version (ex: "T155418 V2") — normalisation vers la même clé
# canonique que côté Odicee, pour que les deux se retrouvent dans le même historique.
numero_dossier_presta = normaliser_numero_dossier(filenumber_presta)

if fichier_presta and filenumber_presta:
    # Historique conservé (insert), sauf si rigoureusement identique à la dernière version
    # enregistrée (évite les doublons quand on redépose le même fichier par erreur).
    ok_presta, msg_presta = sauvegarder_analyse_prestataire(presta, numero_dossier_presta, fiche_presta)
    if ok_presta and msg_presta == "identique":
        st.toast(f"ℹ️ Identique à la dernière version enregistrée pour {numero_dossier_presta} — pas de doublon créé.", icon="ℹ️")
        lister_toutes_analyses_prestataire.clear()
    elif ok_presta:
        st.toast(f"✅ Analyse prestataire {filenumber_presta} enregistrée dans l'historique.", icon="💾")
        lister_toutes_analyses_prestataire.clear()
        lister_historique_prestataire.clear()
        lister_tous_numeros_connus.clear()
    else:
        st.warning(f"⚠️ Échec de l'enregistrement de l'analyse prestataire dans Supabase : {msg_presta}")

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
    url_odicee_appli = f"https://odicee.edf.fr/dossiers/{dossier_id}"
    liens = [
        f'<a href="{url_odicee}" target="_blank">🔗 Ouvrir le JSON Odicee (API)</a>',
        f'<a href="{url_odicee_appli}" target="_blank">🔗 Ouvrir le dossier Odicee</a>',
    ]
    id_technique_presta = presta.get("id")
    if id_technique_presta:
        url_presta = f"https://docminddev.promotelec-services.com/api/dossiers/{id_technique_presta}"
        url_presta_appli = f"https://docminddev.promotelec-services.com/dossier/{id_technique_presta}"
        liens.append(f'<a href="{url_presta}" target="_blank">🔗 Ouvrir le JSON Prestataire (API)</a>')
        liens.append(f'<a href="{url_presta_appli}" target="_blank">🔗 Ouvrir le dossier Prestataire</a>')
    st.markdown("&nbsp;&nbsp;·&nbsp;&nbsp;".join(liens), unsafe_allow_html=True)

if not match_id:
    st.warning(
        "⚠️ Le numéro de dossier du rapport prestataire ne correspond pas au JSON Odicee chargé. "
        "Vérifiez que vous comparez bien le même dossier avant d'interpréter les écarts ci-dessous."
    )

# ── Historique des analyses prestataire pour ce dossier ──
if SUPABASE_DISPONIBLE and get_supabase_client() and filenumber_presta:
    historique = lister_historique_prestataire(filenumber_presta)
    if len(historique) > 1:
        with st.expander(f"🕓 Historique des analyses prestataire pour {filenumber_presta} ({len(historique)} versions)"):
            df_hist = pd.DataFrame([
                {
                    "Date d'analyse": (h.get("date_analyse") or h.get("date_ajout") or "")[:19].replace("T", " "),
                    "Fiabilité": f"{h['reliability_score']*100:.0f}%" if h.get("reliability_score") is not None else "—",
                    "Statut global": h.get("overall_status") or "—",
                }
                for h in historique
            ])
            st.table(df_hist)

            versions_labels = [
                f"{(h.get('date_analyse') or h.get('date_ajout') or '')[:19].replace('T', ' ')} "
                f"(fiabilité {h['reliability_score']*100:.0f}%)" if h.get("reliability_score") is not None
                else (h.get("date_analyse") or h.get("date_ajout") or "")[:19].replace("T", " ")
                for h in historique
            ]
            choix_ancien = st.selectbox(
                "Comparer la version actuelle à une version antérieure :",
                ["—"] + versions_labels[1:],  # la plus récente (index 0) = celle chargée maintenant
            )
            if choix_ancien != "—":
                idx_choisi = versions_labels.index(choix_ancien)
                ancien_presta, erreur_ancien = charger_analyse_prestataire(historique[idx_choisi]["id"])
                if erreur_ancien:
                    st.error(f"⚠️ Échec du rechargement de cette version : {erreur_ancien}")
                elif ancien_presta:
                    diff = comparer_deux_analyses_prestataire(ancien_presta, presta)
                    diff_changes = [d for d in diff if d[3]]
                    if diff_changes:
                        st.markdown(f"**{len(diff_changes)} champ(s) modifié(s) depuis cette version :**")
                        st.table(pd.DataFrame(
                            [{"Champ": c, "Ancienne valeur": va, "Nouvelle valeur": vn} for c, va, vn, _ in diff_changes]
                        ))
                    else:
                        st.caption("Aucune différence détectée sur les champs techniques extraits entre ces deux versions.")

st.markdown("---")

# ── Sélection du lot Odicee correspondant à la fiche du rapport prestataire ──
lots_par_fiche = get_odicee_lots_bar(data)
fiche_odicee_match = next((f for f in lots_par_fiche if f.upper() == fiche_presta), None)

if fichier_odicee and lots_par_fiche:
    # Sauvegarde dans Supabase à chaque nouvel upload (upsert : écrase la version précédente
    # du même dossier). Message explicite en cas d'échec pour pouvoir diagnostiquer.
    ok_odicee, msg_odicee = sauvegarder_dossier_odicee(data, fiche=next(iter(lots_par_fiche), None))
    if ok_odicee:
        st.toast(f"✅ Dossier Odicee {id_odicee} enregistré.", icon="💾")
        lister_dossiers_odicee.clear()
        lister_tous_numeros_connus.clear()
    else:
        st.warning(f"⚠️ Échec de l'enregistrement du dossier Odicee dans Supabase : {msg_odicee}")

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
    # "Quote" (devis) n'est pas idéal comme preuve d'engagement (c'est une offre, pas un accord
    # signé) mais certains dossiers n'ont que ça — mieux vaut l'utiliser en dernier recours que
    # de ne rien détecter du tout.
    or get_presta_doc(report, "Quote")
)
doc_realisation = (
    get_presta_doc_par_regle(report, "DOSSIER_HAS_COMPLETION")
    or get_presta_doc_alias(report, "Invoice")
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
adresse_presta, doc_adresse_presta = get_presta_works_address(report, doc_realisation, doc_engagement)
rows_identite.append(("Adresse des travaux", adresse_fd, adresse_presta))

prof = lot.setdefault("professionnel", {})  # référence réelle dans `lot`/`data`, pas une copie
# `professionnel` est tantôt le maître d'œuvre, tantôt un mandataire/apporteur d'affaire — jamais
# fiable à l'aveugle. Le SIRET à comparer aux documents prestataire (facture/RGE) est celui du
# professionnel ayant réellement réalisé les travaux : voir trouver_professionnel_installateur(),
# qui confronte chaque candidat Odicee au SIRET de la facture pour lever l'ambiguïté.
siret_presta = (doc_realisation or {}).get("extractedFields", {}).get("siret")
titulaire, avertissement_installateur = trouver_professionnel_installateur(data, lot, siret_presta)
siret_odicee = titulaire.get("siret")
rows_identite.append(("SIRET professionnel", siret_odicee, siret_presta))

# Sous-traitant : n'affiché que si au moins un côté en a un (dossier avec DC4 / RGE distinct
# du titulaire), pour ne pas ajouter une ligne "—/—" sur les dossiers sans sous-traitance.
sous_traitant_od = lot.get("professionnelSousTraitant")
sous_traitant_od = sous_traitant_od if isinstance(sous_traitant_od, dict) else {}
siret_sous_traitant_od = sous_traitant_od.get("siret")

doc_rge = get_presta_doc(report, "RgeCertificate")
siret_rge = (doc_rge or {}).get("extractedFields", {}).get("siret")
# Le RGE n'est un "sous-traitant côté prestataire" que s'il diffère du titulaire (sinon c'est
# simplement le certificat RGE du titulaire lui-même, rien d'anormal).
siret_sous_traitant_presta = siret_rge if (siret_rge and siret_rge != siret_odicee) else None

if siret_sous_traitant_od or siret_sous_traitant_presta:
    rows_identite.append(("SIRET sous-traitant", siret_sous_traitant_od, siret_sous_traitant_presta))

lignes_html = []
for label, v_od, v_pr in rows_identite:
    statut, detail = comparer(v_od, v_pr, tolerance=0)
    lignes_html.append((badge(statut), label, f"{v_od}" if v_od else "—", f"{v_pr}" if v_pr else "—", detail or ""))

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
if doc_adresse_presta and doc_realisation and doc_adresse_presta != doc_realisation.get("fileName"):
    st.caption(f"ℹ️ Adresse prestataire trouvée sur **{doc_adresse_presta}** (absente de la facture).")
if avertissement_installateur:
    st.warning(avertissement_installateur)

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
        titulaire["siret"] = valeur_editee
        modifications_odicee.append((fiche_odicee_match, "professionnelTitulaireSigneQualite.siret", valeur_orig, valeur_editee))
    elif label == "SIRET sous-traitant":
        st_dict = lot.setdefault("professionnelSousTraitant", {})
        st_dict["siret"] = valeur_editee
        modifications_odicee.append((fiche_odicee_match, "professionnelSousTraitant.siret", valeur_orig, valeur_editee))

# ── Comparaison technique champ par champ ──
st.markdown("#### 🔧 Données techniques")

ref_upper = fiche_odicee_match.upper()
export_technique = None  # rempli par chaque branche ci-dessous, utilisé pour l'export Excel

if "BAR-TH-106" in ref_upper:
    lignes_th106, note_th106, editable_th106 = comparer_th106(fd, report)
    if note_th106:
        st.info(note_th106)
    docs_presents = [dt for dt in DOC_TYPES_TECHNIQUES if get_presta_doc_alias(report, dt)]
    entete = ["", "Champ", "Odicee"] + [LABEL_DOC_TYPE[dt] for dt in docs_presents]
    lignes = {c: [] for c in entete}
    cles_ecriture_th106 = []
    for label, valeur_od, valeurs_pr, cle_ecriture in lignes_th106:
        # Le badge de statut ne reflète que l'écart Odicee <-> Facture ; l'AH n'est qu'une
        # information affichée en plus, elle ne doit pas influencer la conclusion (déclaration
        # signée par le bénéficiaire, pas une pièce probante recoupable comme une facture).
        statut_badge, _ = comparer(valeur_od, valeurs_pr.get("Invoice"))
        lignes[""].append(badge(statut_badge))
        lignes["Champ"].append(label)
        lignes["Odicee"].append(f"{valeur_od}" if valeur_od not in (None, "") else "—")
        for dt in docs_presents:
            v = valeurs_pr.get(dt)
            lignes[LABEL_DOC_TYPE[dt]].append(fmt_date_any(v) if v not in (None, "") else "—")
        cles_ecriture_th106.append(cle_ecriture)

    if editable_th106:
        df_th106 = pd.DataFrame(lignes)
        colonnes_verrouillees_th106 = [c for c in df_th106.columns if c != "Odicee"]
        df_th106_edite = st.data_editor(
            df_th106,
            disabled=colonnes_verrouillees_th106,
            column_config={"Odicee": st.column_config.TextColumn("Odicee ✏️")},
            hide_index=True,
            key=f"editeur_th106_{fiche_odicee_match}_{adresse_site}",
        )
        st.caption(
            "🟢 valeurs concordantes · 🔴 écart net · 🟡 correspondance partielle · ⚪ absent d'un côté "
            "(Odicee vs Facture uniquement — l'AH est affichée à titre informatif et n'influence pas "
            "la couleur). Classe régulateur comparée en chiffre arabe (Odicee est en chiffre romain). "
            "✏️ Colonne Odicee modifiable, sauf marque/référence chaudière et classe régulateur "
            "(champs composites/à liste déroulante, non réinjectables tels quels ici)."
        )
        for i, cle_ecriture in enumerate(cles_ecriture_th106):
            if not cle_ecriture:
                continue
            valeur_orig_affichee = lignes["Odicee"][i]
            valeur_editee = df_th106_edite["Odicee"].iloc[i]
            if str(valeur_editee) != str(valeur_orig_affichee):
                fd[cle_ecriture] = caster_comme_original(fd.get(cle_ecriture), valeur_editee)
                modifications_odicee.append((fiche_odicee_match, cle_ecriture, valeur_orig_affichee, valeur_editee))
        export_technique = {"type": "table", "titre": "Données techniques", "df": df_th106_edite}
    else:
        st.table(lignes)
        st.caption(
            "🟢 valeurs concordantes · 🔴 écart net · 🟡 correspondance partielle · ⚪ absent d'un côté "
            "(Odicee vs Facture uniquement — l'AH est affichée à titre informatif et n'influence pas "
            "la couleur). Classe régulateur comparée en chiffre arabe (Odicee est en chiffre romain). "
            "Tableau non modifiable ici (cas « collectif », structure multi-chaudières)."
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
        docs_presents = [dt for dt in DOC_TYPES_TECHNIQUES if get_presta_doc_alias(report, dt)]
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

                # Champ cumulable (ex: surface) sur une fiche à plusieurs lots (plusieurs
                # bâtiments d'un même complexe) : la facture prestataire donne souvent un total
                # combiné plutôt qu'un chiffre par bâtiment — on doit alors sommer Odicee sur
                # tous les lots de la fiche pour comparer des totaux équivalents, sinon chaque
                # lot ressort systématiquement en faux écart net face au total facturé.
                est_cumule = False
                if cle_od in CHAMPS_CUMULABLES and len(lots_sites) > 1:
                    valeurs_lots = [normalise_nombre(l.get("formData", {}).get(cle_od)) for l, _ in lots_sites]
                    if all(v is not None for v in valeurs_lots):
                        valeur_od = sum(valeurs_lots)
                        valeur_od_dec = valeur_od
                        est_cumule = True
                if not est_cumule:
                    valeur_od = fd.get(cle_od)
                    valeur_od_dec = decoder_valeur(fiche_odicee_match, cle_od, valeur_od)

                valeurs_pr = {}
                for dt in docs_presents:
                    v, _fname = get_presta_technical_value(report, dt, cle_pr)
                    valeurs_pr[dt] = v

                # Statut du badge = comparaison Odicee <-> Facture uniquement ; l'AH reste
                # affichée à titre d'information mais n'influence pas la conclusion (déclaration
                # signée par le bénéficiaire, pas une pièce probante recoupable comme une facture).
                statut_badge, _ = comparer(valeur_od, valeurs_pr.get("Invoice"))

                lignes[""].append(badge(statut_badge))
                lignes["Champ"].append(
                    f"{label}" + (f" ({unite})" if unite else "")
                    + (" [cumulé, tous lots]" if est_cumule else "")
                )
                lignes["Odicee"].append(
                    f"{valeur_od_dec}" if valeur_od_dec not in (None, "") else "—"
                )
                for dt in docs_presents:
                    v = valeurs_pr[dt]
                    lignes[LABEL_DOC_TYPE[dt]].append(fmt_date_any(v) if v not in (None, "") else "—")

                cles_od_lignes.append(cle_od)
                # Un total cumulé ne se réinjecte pas proprement dans le formData d'un seul lot :
                # non éditable, comme les champs encodés (même mécanisme de verrouillage).
                encode_lignes.append(est_cumule or str(valeur_od_dec) != str(valeur_od))

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
                    "visuellement, ex. texte tronqué/reformaté) · ⚪ champ absent d'un des deux côtés — "
                    "comparaison Odicee vs Facture uniquement (l'AH est affichée à titre informatif et "
                    "n'influence pas la couleur). "
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
        file_name=f"{datetime.now().strftime('%Y-%m-%d')}_{id_odicee}_{fiche_odicee_match}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
with colex2:
    if modifications_odicee:
        cdl1, cdl2 = st.columns(2)
        with cdl1:
            st.download_button(
                "📥 Télécharger le JSON modifié",
                data=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                file_name=f"{id_odicee}_modifie.json",
                mime="application/json",
                use_container_width=True,
            )
        with cdl2:
            if supabase_ok:
                if st.button("💾 Mettre à jour dans Supabase", use_container_width=True):
                    ok_maj, msg_maj = sauvegarder_dossier_odicee(data, fiche=fiche_odicee_match)
                    if ok_maj:
                        lister_dossiers_odicee.clear()
                        lister_tous_numeros_connus.clear()
                        st.success(f"✅ Dossier Odicee {id_odicee} mis à jour dans Supabase avec les modifications.")
                    else:
                        st.error(f"⚠️ Échec de la mise à jour : {msg_maj}")
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
            valeurs_odicee_pdf = valeurs_odicee_dossier_pdf(fd, lot, data, report)

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
