import streamlit as st
import json
import pandas as pd
from datetime import datetime

st.set_page_config(page_title="Extracteur JSON - Fiches BAR (CEE)", layout="wide")

st.title("📄 Extracteur de données JSON - Dossiers CEE")
st.write("Importez votre fichier JSON pour extraire automatiquement les valeurs des fiches BAR.")

uploaded_file = st.file_uploader("Choisissez un fichier JSON", type="json")

def format_timestamp(ts):
    if ts:
        # Les timestamps sont souvent en millisecondes dans ces JSON
        return datetime.fromtimestamp(ts / 1000.0).strftime('%d/%m/%Y')
    return None

if uploaded_file is not None:
    try:
        data = json.load(uploaded_file)
        
        # 1. Extraction des dates globales au niveau du dossier
        date_eng = format_timestamp(data.get("dateEngagementReelle"))
        date_real = format_timestamp(data.get("dateRealisationReelle"))
        
        st.subheader("🗓️ Dates du dossier")
        col1, col2 = st.columns(2)
        col1.metric("Date d'engagement réelle", date_eng if date_eng else "Non renseignée")
        col2.metric("Date de réalisation réelle", date_real if date_real else "Non renseignée")
        
        # 2. Parcours des sites et lots pour extraire les fiches BAR
        records = []
        
        for site in data.get("sites", []):
            for lot in site.get("lotsTravaux", []):
                form_data = lot.get("formData", {})
                fiche_ref = form_data.get("reference", "")
                
                # Vérification si c'est une fiche BAR
                if "BAR" in str(fiche_ref).upper():
                    adresse = form_data.get("adresse_travaux", "Non renseignée")
                    
                    # Liste des clés à exclure (Ajout de 'energieChauffage' comme demandé)
                    exclude_keys = [
                        "sme", "titre", "ville", "version", "Altitude", "reference", 
                        "code_postal", "departement", "zoneClimatique", "adresse_travaux", 
                        "nom_site_travaux", "zoneGeographique", "adresse_travaux_ah", 
                        "complement_adresse", "count_html_block_A", "secteurApplication",
                        "nombreLogements", "nombreLogementsConventionnes", "age_batiment_plus_que_deux_ans",
                        "volume", "volumeClassique", "volumePrecarite", "professionnel_titulaire_signe_qualite",
                        "coefficient_zone_a", "energieChauffage"
                    ]
                    
                    # On isole les caractéristiques techniques utiles
                    tech_chars = {k: v for k, v in form_data.items() if k not in exclude_keys and v is not None}
                    
                    # Création de la ligne de base
                    row = {
                        "Fiche BAR": fiche_ref,
                        "Adresse concernée": adresse,
                        "Date d'engagement": date_eng,
                        "Date de réalisation": date_real
                    }
                    
                    # Fusion : on intègre chaque caractéristique technique comme une colonne distincte
                    row.update(tech_chars)
                    
                    records.append(row)
        
        if records:
            # Transformation en tableau (DataFrame)
            df = pd.DataFrame(records)
            
            # On remplace les valeurs "NaN" (Not a Number) générées par Pandas par des cases vides pour plus de propreté
            df = df.fillna("")
            
            # Tri par Fiche BAR pour regrouper les opérations identiques
            df = df.sort_values(by="Fiche BAR").reset_index(drop=True)
            
            st.subheader(f"✅ {len(records)} Fiche(s) BAR trouvée(s)")
            st.dataframe(df, use_container_width=True)
            
            # Option pour télécharger les données extraites en CSV
            csv = df.to_csv(index=False, sep=";").encode('utf-8-sig')
            st.download_button(
                label="📥 Télécharger le tableau en CSV",
                data=csv,
                file_name='extraction_fiches_bar_colonnes.csv',
                mime='text/csv',
            )
        else:
            st.warning("Aucune fiche BAR n'a été trouvée dans ce JSON.")
            
    except Exception as e:
        st.error(f"Erreur lors de la lecture du fichier : {e}")
