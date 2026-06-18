import streamlit as st
import json
import pandas as pd
from datetime import datetime
import io
import re

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
        return datetime.fromtimestamp(ts / 1000.0).strftime('%d/%m/%Y')
    return None

if uploaded_file is not None:
    try:
        data = json.load(uploaded_file)
        
        dossier_id = data.get("id", "")
        if dossier_id:
            st.success(f"Dossier {dossier_id} chargé avec succès !")

        # 1. Extraction des dates globales au niveau du dossier
        date_eng = format_timestamp(data.get("dateEngagementReelle"))
        date_real = format_timestamp(data.get("dateRealisationReelle"))
        
        st.subheader("🗓️ Dates du dossier")
        col1, col2 = st.columns(2)
        col1.metric("Date d'engagement réelle", date_eng if date_eng else "Non renseignée")
        col2.metric("Date de réalisation réelle", date_real if date_real else "Non renseignée")
        
        # 2. Parcours des sites et lots pour extraire les fiches BAR
        records_by_fiche = {}
        
        for site in data.get("sites", []):
            
            # Récupération de l'adresse globale du Site en secours
            numero_site = site.get("numero", "")
            voie_site = site.get("nomVoie", "")
            cp_site = site.get("codePostal", "")
            ville_site = site.get("ville", "")
            parts_site = [str(x) for x in [numero_site, voie_site, cp_site, ville_site] if x]
            adresse_globale_site = " ".join(parts_site)
            
            for lot in site.get("lotsTravaux", []):
                form_data = lot.get("formData", {})
                fiche_ref = form_data.get("reference", "")
                
                if "BAR" in str(fiche_ref).upper():
                    
                    if fiche_ref not in records_by_fiche:
                        records_by_fiche[fiche_ref] = []
                        
                    # Mécanisme de choix de l'adresse
                    adresse_form = form_data.get("adresse_travaux", "").strip()
                    if adresse_form:
                        adresse = adresse_form 
                    elif adresse_globale_site:
                        adresse = adresse_globale_site 
                    else:
                        adresse = "Non renseignée"
                    
                    # Liste des clés à exclure
                    exclude_keys = [
                        "sme", "titre", "ville", "version", "Altitude", "reference", 
                        "code_postal", "departement", "zoneClimatique", "adresse_travaux", 
                        "nom_site_travaux", "zoneGeographique", "adresse_travaux_ah", 
                        "complement_adresse", "count_html_block_A", "secteurApplication",
                        "nombreLogements", "nombreLogementsConventionnes", "age_batiment_plus_que_deux_ans",
                        "volume", "volumeClassique", "volumePrecarite", "professionnel_titulaire_signe_qualite",
                        "coefficient_zone_a", "energieChauffage", 
                        "type_pose", "min_value_resistance", "soustraction_resistance_minvr", 
                        "is_age_batiment_plus_que_deux_ans_auto_filled",
                        "delta_temperature", "type_logement_and_chauffage", "systeme_chauffage_central",
                        "ID Professionnel sous traitant", "type_logement", "type_emetteur_electrique",
                        "marque_emetteurs", "reference_emetteurs", "is_multiple_entry",
                        "surface_habitable_35", "surface_habitable_130", "surface_habitable_35_60",
                        "surface_habitable_60_70", "surface_habitable_70_90", "surface_habitable_90_110",
                        "surface_habitable_110_130", "max_puissance_collective", 
                        "validate_value_for_type_caisson", "validate_choice_for_type_caisson",
                        "reference_technique", "validate_requireds", "surface_habitable_70",
                        "batiment_age_plus_de_2_ans", "diff_nb_chaudiere_appartements"
                    ]
                    
                    tech_chars = {k: v for k, v in form_data.items() if k not in exclude_keys and v is not None}
                    
                    # ----------------------------------------------------
                    # GESTION DES TABLEAUX IMBRIQUÉS (Équipements multi-lignes)
                    # ----------------------------------------------------
                    equipements_list = []
                    
                    # Détection insensible à la casse de la clé "Equipements" (ex: BAR-TH-158)
                    eq_key = next((k for k in list(tech_chars.keys()) if k.lower() == "equipements"), None)
                    if eq_key:
                        eq_data = tech_chars.pop(eq_key)
                        try:
                            # Parser de manière souple (dict, list ou string JSON)
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
                                marque = item[0] if len(item) > 0 else ""
                                ref = item[1] if len(item) > 1 else ""
                                qte = item[3] if len(item) > 3 else ""
                                puis = item[4] if len(item) > 4 else ""
                                equipements_list.append({
                                    "Éq. Marque": marque,
                                    "Éq. Référence": ref,
                                    "Éq. Quantité": qte,
                                    "Éq. Puissance (W)": puis
                                })
                        except Exception:
                            pass

                    # Détection insensible à la casse de la clé "Puissance" (ex: BAR-TH-106)
                    puissance_key = next((k for k in list(tech_chars.keys()) if k.lower() == "puissance"), None)
                    if puissance_key and "BAR-TH-106" in str(fiche_ref).upper():
                        eq_data = tech_chars.pop(puissance_key)
                        try:
                            # Parser de manière souple (dict, list ou string JSON)
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
                                mr_chaudiere = item[0] if len(item) > 0 else ""
                                qte = item[1] if len(item) > 1 else ""
                                puis = item[2] if len(item) > 2 else ""
                                etas = item[3] if len(item) > 3 else ""
                                mr_regu = item[4] if len(item) > 4 else ""
                                classe_regu = item[5] if len(item) > 5 else ""
                                
                                equipements_list.append({
                                    "Éq. M et R Chaudière": mr_chaudiere,
                                    "Éq. Quantité": qte,
                                    "Éq. Puissance": puis,
                                    "Éq. ETAS": etas,
                                    "Éq. M et R Régulateur": mr_regu,
                                    "Éq. Classe régu": classe_regu
                                })
                        except Exception:
                            pass

                    # ----------------------------------------------------
                    # MAPPINGS SPÉCIFIQUES (Traduction des codes)
                    # ----------------------------------------------------
                    if "type_fenetre" in tech_chars:
                        if tech_chars["type_fenetre"] == 1:
                            tech_chars["type_fenetre"] = "Autres fenetres"
                        elif tech_chars["type_fenetre"] == 0:
                            tech_chars["type_fenetre"] = "Fenêtre de toiture"
                            
                    if "type_caisson" in tech_chars:
                        if tech_chars["type_caisson"] == 2:
                            tech_chars["type_caisson"] = "basse pression"
                        elif tech_chars["type_caisson"] == 1:
                            tech_chars["type_caisson"] = "basse consommation"
                        elif tech_chars["type_caisson"] == 0:
                            tech_chars["type_caisson"] = "standard"

                    if "type_ventilation" in tech_chars:
                        if tech_chars["type_ventilation"] == 0:
                            tech_chars["type_ventilation"] = "hygro A"
                        elif tech_chars["type_ventilation"] == 1:
                            tech_chars["type_ventilation"] = "hygro B"
                    
                    # ----------------------------------------------------
                    # CRÉATION DES LIGNES (Avec ou sans équipements)
                    # ----------------------------------------------------
                    base_row = {
                        "Adresse concernée": adresse,
                        "Date d'engagement": date_eng,
                        "Date de réalisation": date_real
                    }
                    base_row.update(tech_chars) 
                    
                    if not equipements_list:
                        # Cas classique
                        records_by_fiche[fiche_ref].append(base_row)
                    else:
                        # Cas avec tableau : Le 1er équipement fusionne avec la ligne adresse/dates
                        first_row = {**base_row, **equipements_list[0]}
                        records_by_fiche[fiche_ref].append(first_row)
                        
                        # Les équipements suivants génèrent de nouvelles lignes (colonnes vides sauf équipement)
                        for eq in equipements_list[1:]:
                            empty_row = {k: "" for k in base_row.keys()} 
                            empty_row.update(eq) 
                            records_by_fiche[fiche_ref].append(empty_row)
        
        # 3. Affichage et Export Excel
        if records_by_fiche:
            total_fiches = sum(len(lignes) for lignes in records_by_fiche.values())
            st.subheader(f"✅ Opération(s) trouvée(s)")
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                
                for fiche, lignes in records_by_fiche.items():
                    df = pd.DataFrame(lignes)
                    df = df.fillna("")
                    
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
