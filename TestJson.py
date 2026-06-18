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
    # Nettoyage automatique : on extrait uniquement les chiffres
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
            for lot in site.get("lotsTravaux", []):
                form_data = lot.get("formData", {})
                fiche_ref = form_data.get("reference", "")
                
                # Vérification si c'est une fiche BAR
                if "BAR" in str(fiche_ref).upper():
                    
                    if fiche_ref not in records_by_fiche:
                        records_by_fiche[fiche_ref] = []
                        
                    adresse = form_data.get("adresse_travaux", "Non renseignée")
                    
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
                        "ID Professionnel sous traitant", "type_logement", "type_emetteur_electrique"
                    ]
                    
                    # On isole les caractéristiques techniques utiles
                    tech_chars = {k: v for k, v in form_data.items() if k not in exclude_keys and v is not None}
                    
                    # Extraction et suppression temporaire de "Equipements" pour traitement manuel
                    equipements_list = []
                    if "Equipements" in tech_chars:
                        eq_data = tech_chars.pop("Equipements")
                        try:
                            if isinstance(eq_data, dict) and "values" in eq_data:
                                values_str = eq_data["values"]
                                eq_list = json.loads(values_str) if isinstance(values_str, str) else values_str
                                
                                for item in eq_list:
                                    # Index : 0=Marque, 1=Ref, 2=Certificat (ignoré), 3=Qté, 4=Puissance
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

                    # Mapping spécifique de la valeur 'type_fenetre'
                    if "type_fenetre" in tech_chars:
                        if tech_chars["type_fenetre"] == 1:
                            tech_chars["type_fenetre"] = "Autres fenetres"
                        elif tech_chars["type_fenetre"] == 0:
                            tech_chars["type_fenetre"] = "Fenêtre de toiture"
                    
                    # Création de la ligne principale
                    base_row = {
                        "Adresse concernée": adresse,
                        "Date d'engagement": date_eng,
                        "Date de réalisation": date_real
                    }
                    base_row.update(tech_chars) # On y fusionne les autres infos techniques
                    
                    # Logique d'insertion avec ou sans équipements
                    if not equipements_list:
                        # Cas classique : pas d'équipement spécifique à lister
                        records_by_fiche[fiche_ref].append(base_row)
                    else:
                        # Cas avec tableau (ex: BAR-TH-158)
                        # Le 1er équipement est fusionné avec la ligne principale
                        first_row = {**base_row, **equipements_list[0]}
                        records_by_fiche[fiche_ref].append(first_row)
                        
                        # Les équipements suivants génèrent de nouvelles lignes vides, sauf pour l'équipement
                        for eq in equipements_list[1:]:
                            empty_row = {k: "" for k in base_row.keys()} # Création de colonnes vides
                            empty_row.update(eq) # On n'y met QUE le nouvel équipement
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
