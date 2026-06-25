import streamlit as st
import json
import pandas as pd
from datetime import datetime
import io
import re
import pytz

st.set_page_config(page_title="Extracteur JSON - Fiches BAR (CEE)", layout="wide")

# ==========================================
# BARRE LATÉRALE : RACCOURCI DE TÉLÉCHARGEMENT
# ==========================================
st.sidebar.header("🔗 Raccourci Odicee")
st.sidebar.write("Générez rapidement le lien d'un dossier pour extraire son JSON :")
num_dossier = st.sidebar.text_input("Numéro de dossier (ex: T123272, CP123456...)")

if num_dossier:
    num_dossier_clean = re.sub(r'\D', '', num_dossier)
    if num_dossier_clean:
        lien = f"https://odicee.edf.fr/api/dossiers/{num_dossier_clean}"
        st.sidebar.markdown(f"**[➡️ Ouvrir le JSON du dossier {num_dossier_clean}]({lien})**")
        st.sidebar.caption("Astuce : Sur la nouvelle page, faites *Ctrl + S* pour sauvegarder le fichier, puis importez-le au centre de cette page.")

# ==========================================
# CORPS DE L'APPLICATION
# ==========================================
st.title("📄 Extracteur de données JSON - Dossiers CEE")
st.write("Importez votre fichier JSON pour extraire automatiquement les valeurs des fiches BAR par type d'opération.")

uploaded_file = st.file_uploader("Choisissez un fichier JSON", type="json")

def format_timestamp(ts):
    if ts:
        paris_tz = pytz.timezone('Europe/Paris')
        dt = datetime.fromtimestamp(ts / 1000.0, paris_tz)
        return dt.strftime('%d/%m/%Y')
    return None

# ==========================================
# MAPPINGS PAR FICHE
# ==========================================

SEUIL_BAR_EN_104_V2 = datetime(2024, 1, 1, tzinfo=pytz.timezone('Europe/Paris'))

def apply_mappings(fiche_ref, tech_chars, date_eng_ts=None):
    """Applique les mappings de valeurs selon la fiche concernée.
    
    date_eng_ts : timestamp brut (ms) de dateEngagementReelle, utilisé pour
                  les mappings dont la signification dépend de la version du formulaire.
    """
    ref_upper = str(fiche_ref).upper()

    # BAR-EN-101 : type_pose
    if "BAR-EN-101" in ref_upper:
        if "type_pose" in tech_chars:
            val = tech_chars["type_pose"]
            tech_chars["type_pose"] = "En combles perdus" if val == 0 else "En rampant de toiture" if val == 1 else val

    # BAR-EN-104 : type_fenetre
    # Avant 01/01/2024 : 0=Fenêtre de toiture, 1=Autre(s) fenêtre(s)
    # À partir du 01/01/2024 : 0=Fenêtre de toiture, 1=Double(s) fenêtre(s), 2=Autre(s) fenêtre(s)
    if "BAR-EN-104" in ref_upper:
        if "type_fenetre" in tech_chars:
            val = tech_chars["type_fenetre"]
            paris_tz = pytz.timezone('Europe/Paris')
            eng_dt = datetime.fromtimestamp(date_eng_ts / 1000.0, paris_tz) if date_eng_ts else None
            nouvelle_version = eng_dt is not None and eng_dt >= SEUIL_BAR_EN_104_V2

            if val == 0:
                tech_chars["type_fenetre"] = "Fenêtre de toiture"
            elif val == 1:
                tech_chars["type_fenetre"] = "Double(s) fenêtre(s)" if nouvelle_version else "Autre(s) fenêtre(s)"
            elif val == 2:
                # Valeur 2 n'existe qu'en nouvelle version
                tech_chars["type_fenetre"] = "Autre(s) fenêtre(s)"

        # Marque : marque_fenetre OU marque_isolant
        if "marque_fenetre" in tech_chars:
            tech_chars["marque"] = tech_chars.pop("marque_fenetre")
        elif "marque_isolant" in tech_chars:
            tech_chars["marque"] = tech_chars.pop("marque_isolant")

        # Référence : reference_fenetre OU reference_isolant
        if "reference_fenetre" in tech_chars:
            tech_chars["reference_produit"] = tech_chars.pop("reference_fenetre")
        elif "reference_isolant" in tech_chars:
            tech_chars["reference_produit"] = tech_chars.pop("reference_isolant")

        # Renommages explicites pour clarté
        if "facteur_solaire_sw" in tech_chars:
            tech_chars["Sw"] = tech_chars.pop("facteur_solaire_sw")
        if "coefficient_surfacique" in tech_chars:
            tech_chars["Uw (W/m².K)"] = tech_chars.pop("coefficient_surfacique")
        if "nombre_de_fenetres_ou_portefenetres" in tech_chars:
            tech_chars["Quantité"] = tech_chars.pop("nombre_de_fenetres_ou_portefenetres")

    # BAR-EN-105 : résistance thermique (deux clés possibles)
    if "BAR-EN-105" in ref_upper:
        if "resistance_thermique_non_exported" in tech_chars:
            tech_chars["resistance_thermique"] = tech_chars.pop("resistance_thermique_non_exported")

    # BAR-TH-127 : type_logement, type_caisson, type_ventilation
    if "BAR-TH-127" in ref_upper:
        if "type_logement" in tech_chars:
            val = tech_chars["type_logement"]
            tech_chars["type_installation"] = "Installation collective" if val == 0 else "Installation individuelle" if val == 1 else val
            del tech_chars["type_logement"]

        if "type_caisson" in tech_chars:
            val = tech_chars["type_caisson"]
            if val == 0:
                tech_chars["type_caisson"] = "caisson standard"
            elif val == 1:
                tech_chars["type_caisson"] = "caisson basse consommation"
            elif val == 2:
                tech_chars["type_caisson"] = "caisson basse pression"

        if "type_ventilation" in tech_chars:
            val = tech_chars["type_ventilation"]
            tech_chars["type_ventilation"] = "hygro A" if val == 0 else "hygro B" if val == 1 else val

    return tech_chars


def extract_equipements_th158(tech_chars):
    """BAR-TH-158 : tableau Equipements multi-lignes (marque, ref, qté, puissance)."""
    equipements_list = []
    eq_key = next((k for k in list(tech_chars.keys()) if k.lower() == "equipements"), None)
    if eq_key:
        eq_data = tech_chars.pop(eq_key)
        try:
            if isinstance(eq_data, dict) and "values" in eq_data:
                values_data = eq_data["values"]
                eq_list = json.loads(values_data) if isinstance(values_data, str) else values_data
            elif isinstance(eq_data, str):
                eq_list = json.loads(eq_data)
            elif isinstance(eq_data, list):
                eq_list = eq_data
            else:
                eq_list = []

            for item in eq_list:
                equipements_list.append({
                    "Éq. Marque":        item[0] if len(item) > 0 else "",
                    "Éq. Référence":     item[1] if len(item) > 1 else "",
                    "Éq. Quantité":      item[3] if len(item) > 3 else "",
                    "Éq. Puissance (W)": item[4] if len(item) > 4 else ""
                })
        except Exception:
            pass
    return equipements_list


def extract_equipements_th106(tech_chars):
    """
    BAR-TH-106 : deux cas selon type_logement.
      - Individuel (type_logement == 1) : champs directs dans formData
        (marque_chaudiere, reference_chaudiere, puissance_thermique_nominale,
         efficacite_energetique, marque_regulateur, reference_regulateur,
         classe_regulateur, surface_habitable)
      - Collectif  (type_logement == 2) : tableau Puissance.values[i]
    Dans les deux cas, la clé brute "Puissance" est retirée de tech_chars.
    Retourne (equipements_list, est_collectif).
    """
    equipements_list = []
    type_logement = tech_chars.get("type_logement")
    est_collectif = (type_logement == 2)

    # Supprimer la clé "Puissance" dans tous les cas (tableau collectif ou vide en individuel)
    puissance_key = next((k for k in list(tech_chars.keys()) if k.lower() == "puissance"), None)
    if puissance_key:
        eq_data = tech_chars.pop(puissance_key)

        if est_collectif:
            # Cas collectif : parser le tableau
            try:
                if isinstance(eq_data, dict) and "values" in eq_data:
                    values_data = eq_data["values"]
                    eq_list = json.loads(values_data) if isinstance(values_data, str) else values_data
                elif isinstance(eq_data, str):
                    eq_list = json.loads(eq_data)
                elif isinstance(eq_data, list):
                    eq_list = eq_data
                else:
                    eq_list = []

                for item in eq_list:
                    equipements_list.append({
                        "Éq. M et R Chaudière":  item[0] if len(item) > 0 else "",
                        "Éq. Quantité":          item[1] if len(item) > 1 else "",
                        "Éq. Puissance (kW)":    item[2] if len(item) > 2 else "",
                        "Éq. ETAS (%)":          item[3] if len(item) > 3 else "",
                        "Éq. M et R Régulateur": item[4] if len(item) > 4 else "",
                        "Éq. Classe régu":       item[5] if len(item) > 5 else ""
                    })
            except Exception:
                pass
        # Cas individuel : on a juste supprimé la clé Puissance (tableau vide/inutile),
        # les vrais champs sont déjà dans tech_chars

    return equipements_list, est_collectif


# ==========================================
# CLÉS À EXCLURE GLOBALEMENT
# ==========================================
EXCLUDE_KEYS = {
    "sme", "titre", "ville", "version", "Altitude", "reference",
    "code_postal", "departement", "zoneClimatique", "adresse_travaux",
    "nom_site_travaux", "zoneGeographique", "adresse_travaux_ah",
    "complement_adresse", "count_html_block_A", "secteurApplication",
    "nombreLogements", "nombreLogementsConventionnes", "age_batiment_plus_que_deux_ans",
    "volume", "volumeClassique", "volumePrecarite", "professionnel_titulaire_signe_qualite",
    "coefficient_zone_a", "energieChauffage",
    "type_pose",  # géré via mapping BAR-EN-101 uniquement
    "min_value_resistance", "soustraction_resistance_minvr",
    "is_age_batiment_plus_que_deux_ans_auto_filled",
    "delta_temperature", "type_logement_and_chauffage", "systeme_chauffage_central",
    "ID Professionnel sous traitant", "type_emetteur_electrique",
    "marque_emetteurs", "reference_emetteurs", "is_multiple_entry",
    "surface_habitable_35", "surface_habitable_130", "surface_habitable_35_60",
    "surface_habitable_60_70", "surface_habitable_70_90", "surface_habitable_90_110",
    "surface_habitable_110_130", "max_puissance_collective",
    "validate_value_for_type_caisson", "validate_choice_for_type_caisson",
    "reference_technique", "validate_requireds", "surface_habitable_70",
    "batiment_age_plus_de_2_ans", "diff_nb_chaudiere_appartements",
    "validate_conditions", "chaudiere_plus_que_deux_ans",
    "radiateurs_plus_que_deux_ans", "is_multiple_entry_auto_filled",
    "mise_en_place_pare_vapeur", "date_debut_travaux",
    # Clés internes de calcul BAR-EN-101 (non affichées)
    "resistance_thermique_non_exported",  # remplacée par resistance_thermique dans BAR-EN-105
}

# type_pose est géré via mapping, on le retire de l'exclusion globale pour BAR-EN-101
# (on le réintègre dans apply_mappings)
EXCLUDE_KEYS_WITHOUT_TYPE_POSE = EXCLUDE_KEYS - {"type_pose"}


if uploaded_file is not None:
    try:
        data = json.load(uploaded_file)

        dossier_id = data.get("id", "")
        if dossier_id:
            st.success(f"Dossier {dossier_id} chargé avec succès !")

        # 1. Extraction des dates globales
        date_eng_ts  = data.get("dateEngagementReelle")   # timestamp brut (ms) pour les mappings versionnés
        date_real_ts = data.get("dateRealisationReelle")
        date_eng  = format_timestamp(date_eng_ts)
        date_real = format_timestamp(date_real_ts)

        st.subheader("🗓️ Dates du dossier")
        col1, col2 = st.columns(2)
        col1.metric("Date d'engagement réelle",  date_eng  if date_eng  else "Non renseignée")
        col2.metric("Date de réalisation réelle", date_real if date_real else "Non renseignée")

        # 2. Parcours des sites et lots
        records_by_fiche = {}

        for site in data.get("sites", []):

            # Adresse du site en dernier recours (non utilisée si formData contient l'adresse)
            numero_site = site.get("numero", "")
            voie_site   = site.get("nomVoie", "")
            cp_site     = site.get("codePostal", "")
            ville_site  = site.get("ville", "")
            parts_site  = [str(x) for x in [numero_site, voie_site, cp_site, ville_site] if x]
            adresse_globale_site = " ".join(parts_site)

            for lot in site.get("lotsTravaux", []):
                form_data = lot.get("formData", {})
                fiche_ref = form_data.get("reference", "")

                if "BAR" not in str(fiche_ref).upper():
                    continue

                if fiche_ref not in records_by_fiche:
                    records_by_fiche[fiche_ref] = []

                # ---------------------------------------------------
                # ADRESSE : formData en priorité (adresse_travaux + ville + code_postal)
                # ---------------------------------------------------
                adresse_travaux = form_data.get("adresse_travaux", "").strip()
                ville_fd        = form_data.get("ville", "").strip()
                cp_fd           = form_data.get("code_postal", "").strip()

                parts_adresse = [x for x in [adresse_travaux, cp_fd, ville_fd] if x]
                if parts_adresse:
                    adresse = " ".join(parts_adresse)
                elif adresse_globale_site:
                    adresse = adresse_globale_site
                else:
                    adresse = "Non renseignée"

                # ---------------------------------------------------
                # SÉLECTION DES CLÉS TECHNIQUES
                # ---------------------------------------------------
                ref_upper = str(fiche_ref).upper()

                # Pour BAR-EN-101, type_pose doit passer (il sera mappé)
                exclude_set = EXCLUDE_KEYS_WITHOUT_TYPE_POSE if "BAR-EN-101" in ref_upper else EXCLUDE_KEYS

                tech_chars = {
                    k: v for k, v in form_data.items()
                    if k not in exclude_set and v is not None
                }

                # ---------------------------------------------------
                # EXTRACTION DES TABLEAUX D'ÉQUIPEMENTS
                # ---------------------------------------------------
                equipements_list = []

                if "BAR-TH-158" in ref_upper:
                    equipements_list = extract_equipements_th158(tech_chars)

                elif "BAR-TH-106" in ref_upper:
                    equipements_list, est_collectif = extract_equipements_th106(tech_chars)
                    # Pour le cas individuel, type_logement reste dans tech_chars mais on
                    # supprime la clé brute (remplacée par le mapping ci-dessous)

                # ---------------------------------------------------
                # MAPPINGS DE VALEURS
                # ---------------------------------------------------
                tech_chars = apply_mappings(fiche_ref, tech_chars, date_eng_ts)

                # ---------------------------------------------------
                # CONSTRUCTION DES LIGNES
                # ---------------------------------------------------
                # surface_fenetres placée en dernière colonne (BAR-EN-104)
                surface_fenetres = tech_chars.pop("surface_fenetres", None)

                base_row = {
                    "Adresse concernée":  adresse,
                    "Date d'engagement":  date_eng,
                    "Date de réalisation": date_real
                }
                base_row.update(tech_chars)

                if surface_fenetres is not None:
                    base_row["surface_fenetres"] = surface_fenetres

                if not equipements_list:
                    records_by_fiche[fiche_ref].append(base_row)
                else:
                    # 1ère ligne : données de base + 1er équipement
                    first_row = {**base_row, **equipements_list[0]}
                    records_by_fiche[fiche_ref].append(first_row)
                    # Lignes suivantes : colonnes de base vides + équipements
                    for eq in equipements_list[1:]:
                        empty_row = {k: "" for k in base_row.keys()}
                        empty_row.update(eq)
                        records_by_fiche[fiche_ref].append(empty_row)

        # 3. Affichage et Export Excel
        if records_by_fiche:
            st.subheader("✅ Opération(s) trouvée(s)")

            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:

                for fiche, lignes in records_by_fiche.items():
                    df = pd.DataFrame(lignes).fillna("")

                    st.markdown(f"### 🏷️ Fiche : {fiche} ({len(lignes)} lignes)")
                    st.dataframe(df, use_container_width=True)

                    nom_onglet = str(fiche)[:31]
                    df.to_excel(writer, index=False, sheet_name=nom_onglet)

            nom_export = f'extraction_fiches_bar_{dossier_id}.xlsx' if dossier_id else 'extraction_fiches_bar.xlsx'
            st.download_button(
                label=f"📥 Télécharger le fichier Excel structuré par Fiches ({nom_export})",
                data=output.getvalue(),
                file_name=nom_export,
                mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            )
        else:
            st.warning("Aucune fiche BAR n'a été trouvée dans ce JSON.")

    except Exception as e:
        st.error(f"Erreur lors de la lecture du fichier : {e}")
