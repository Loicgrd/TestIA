"""
Configuration des fiches BAR — chemins JSON, libellés, seuils.

Source : "Tableau de correspondance - Extraction IA vs JSON Odicee"
(document interne Loïc, rédigé pour le prestataire IA).

Ce fichier centralise tout ce qui est spécifique à chaque fiche BAR, pour que
2_Supervision_dossier.py reste un moteur d'affichage générique. Si une fiche
JSON évolue côté Odicee, c'est ici qu'il faut corriger.

Structure d'une entrée de REGLES :
    (cle_json, label_affiche, unite, critique)
    - cle_json : clé dans formData (ou alias géré séparément, voir ALIAS)
    - critique : True si une valeur vide doit déclencher une alerte rouge
"""

# ─────────────────────────────────────────────
# CHAMPS COMMUNS (niveau racine du JSON, hors formData)
# ─────────────────────────────────────────────
# id, dateEngagementReelle, dateRealisationReelle : data
# adresse_travaux, ville, code_postal, reference : formData (déjà gérés dans l'app)


# ─────────────────────────────────────────────
# RÈGLES TECHNIQUES PAR FICHE
# ─────────────────────────────────────────────

REGLES = {
    "BAR-EN-101": [
        ("type_pose", "Type de pose", "", True),  # 0=combles perdus, 1=rampant — conditionne le seuil R
        ("surface", "Surface isolée (m²)", "m²", True),
        ("resistance_thermique", "Résistance thermique R (m².K/W)", "m².K/W", True),
        ("marque_isolant", "Marque isolant", "", True),
        ("reference_isolant", "Référence isolant", "", False),
        ("epaisseur_isolant", "Épaisseur (mm)", "mm", False),
        ("date_visite_pro", "Date de visite préalable", "", False),
    ],
    "BAR-EN-102": [
        ("surface", "Surface isolée (m²)", "m²", True),
        ("resistance_thermique", "Résistance thermique R (m².K/W)", "m².K/W", True),
        ("marque_isolant", "Marque isolant", "", True),
        ("reference_isolant", "Référence isolant", "", False),
        ("epaisseur_isolant", "Épaisseur (mm)", "mm", False),
        ("date_visite_pro", "Date de visite préalable", "", False),
    ],
    "BAR-EN-103": [
        ("surface", "Surface isolée (m²)", "m²", True),
        ("resistance_thermique", "Résistance thermique R (m².K/W)", "m².K/W", True),
        ("marque_isolant", "Marque isolant", "", True),
        ("reference_isolant", "Référence isolant", "", False),
        ("epaisseur_isolant", "Épaisseur (mm)", "mm", False),
        ("date_visite_pro", "Date de visite préalable", "", False),
    ],
    # BAR-EN-104 : volontairement absent d'ici, voir champs_en104() ci-dessous — les clés JSON
    # diffèrent selon la date d'engagement (changement de version du logiciel Odicee).
    "BAR-EN-105": [
        ("surface", "Surface isolée (m²)", "m²", True),
        ("resistance_thermique_non_exported", "Résistance thermique R (m².K/W)", "m².K/W", True),
        ("marque_isolant", "Marque isolant", "", True),
        ("reference_isolant", "Référence isolant", "", False),
        ("epaisseur_isolant", "Épaisseur (mm)", "mm", False),
    ],
    "BAR-TH-106": [
        ("type_logement", "Type de logement", "", True),  # 1=individuel, 2=collectif
        # -- Individuel uniquement --
        ("marque_chaudiere", "Marque chaudière", "", False),
        ("reference_chaudiere", "Référence chaudière", "", False),
        ("efficacite_energetique", "Efficacité ηs / ETAS (%)", "%", False),
        ("marque_regulateur", "Marque régulateur", "", False),
        ("reference_regulateur", "Référence régulateur", "", False),
        ("classe_regulateur", "Classe régulateur", "", False),  # 0=IV,1=V,2=VI,3=VII,4=VIII
        ("surface_habitable", "Surface habitable (m²)", "m²", False),
    ],
    "BAR-TH-110": [
        ("nb_radiateurs", "Nombre de radiateurs", "", True),
        ("marque_radiateurs", "Marque des radiateurs", "", True),
        ("reference_radiateurs", "Référence des radiateurs", "", False),
    ],
    "BAR-TH-127": [
        ("type_logement", "Type d'installation", "", True),  # 0=collectif, 1=individuel
        ("type_caisson", "Type de caisson", "", True),  # 0=standard,1=basse conso,2=basse pression
        ("type_ventilation", "Type de ventilation", "", True),  # 0=hygro A, 1=hygro B
        ("marque_caisson", "Marque du caisson", "", True),
        ("reference_caisson", "Référence du caisson", "", True),
        ("marque_bouches_entree_air", "Marque bouches entrée d'air", "", False),
        ("reference_bouches_entree_air", "Référence bouches entrée d'air", "", False),
        ("marque_bouches_extraction", "Marque bouches extraction", "", True),
        ("reference_bouches_extraction", "Référence bouches extraction", "", True),
        ("surface_habitable", "Surface habitable (m²)", "m²", False),  # individuel
        ("puissance_individuelle", "Puissance individuelle (WThC)", "WThC", False),  # individuel
        ("puissance_collective", "Puissance collective (WThC/m3/h)", "WThC/m3/h", False),  # collectif
    ],
    "BAR-TH-158": [
        # Champs hors tableau multi-équipements ; le détail par équipement (marque, référence,
        # quantité, puissance) est affiché via le tableau Equipements.values, voir l'app principale.
    ],
}


# ─────────────────────────────────────────────
# SEUILS DE CONTRÔLE
# ─────────────────────────────────────────────
# BAR-EN-101 : seuil R conditionnel selon type_pose (géré à part dans verif_seuil_en101 ci-dessous)
SEUILS = {
    "BAR-EN-102": {"resistance_thermique": (3.7, None)},
    "BAR-EN-103": {"resistance_thermique": (3.7, None)},
    "BAR-EN-104": {"coefficient_surfacique": (None, 1.3), "facteur_solaire_sw": (None, 0.36)},
    "BAR-EN-105": {"resistance_thermique_non_exported": (3.7, None)},
    "BAR-TH-106": {"efficacite_energetique": (87, None)},
}


def seuil_r_en101(type_pose):
    """BAR-EN-101 : le seuil de résistance thermique R dépend du type de pose.
    type_pose : 0 = combles perdus (R >= 7), 1 = rampant de toiture (R >= 6)."""
    if type_pose == 0:
        return 7
    if type_pose == 1:
        return 6
    return None


# ─────────────────────────────────────────────
# CHAMPS CUMULABLES (pour le total dans le tableau multi-sites "même facture")
# ─────────────────────────────────────────────
CHAMPS_CUMULABLES = {
    "surface", "nombre_de_fenetres_ou_portefenetres", "surface_fenetres",
    "nb_radiateurs", "nombreLogements", "nombreLogementsConventionnes",
}


# ─────────────────────────────────────────────
# FICHES NÉCESSITANT UN RGE (à ajuster selon connaissance métier ;
# toutes les fiches BAR du référentiel nécessitent en pratique un RGE qualifié
# pour le type de travaux concerné)
# ─────────────────────────────────────────────
FICHES_RGE_OBLIGATOIRE = {
    "BAR-EN-101", "BAR-EN-102", "BAR-EN-103", "BAR-EN-104", "BAR-EN-105",
    "BAR-TH-106", "BAR-TH-110", "BAR-TH-127", "BAR-TH-158",
}


# ─────────────────────────────────────────────
# DÉCODAGE DES VALEURS NUMÉRIQUES (listes à choix dans le JSON)
# ─────────────────────────────────────────────
DECODAGE = {
    ("BAR-EN-101", "type_pose"): {0: "En combles perdus", 1: "En rampant de toiture"},
    ("BAR-TH-106", "type_logement"): {1: "Individuel", 2: "Collectif"},
    ("BAR-TH-106", "classe_regulateur"): {0: "IV", 1: "V", 2: "VI", 3: "VII", 4: "VIII"},
    ("BAR-TH-127", "type_logement"): {0: "Installation collective", 1: "Installation individuelle"},
    ("BAR-TH-127", "type_caisson"): {0: "Caisson standard", 1: "Caisson basse consommation", 2: "Caisson basse pression"},
    ("BAR-TH-127", "type_ventilation"): {0: "Hygro A", 1: "Hygro B"},
}


def decoder_valeur(fiche_ref, cle, valeur):
    """Retourne le libellé décodé si la fiche/clé a une table de correspondance connue,
    sinon retourne la valeur telle quelle."""
    ref_upper = fiche_ref.upper()
    for (ref_k, cle_k), table in DECODAGE.items():
        if ref_k in ref_upper and cle_k == cle:
            return table.get(valeur, valeur)
    return valeur


def decoder_type_fenetre(valeur, date_engagement_ts):
    """BAR-EN-104 : l'encodage de type_fenetre dépend de la date d'engagement.
    - Avant le 01/01/2024 (2 valeurs) : 0 = Fenêtre(s) de toiture, 1 = Autre(s) fenêtre(s)
    - À partir du 01/01/2024 (3 valeurs) : 0 = Fenêtre(s) de toiture, 1 = Double(s) fenêtre(s),
      2 = Autre(s) fenêtre(s)
    date_engagement_ts : timestamp en millisecondes (dateEngagementReelle du dossier)."""
    if valeur is None:
        return valeur
    SEUIL_2024 = 1704063600000  # 01/01/2024 00:00:00 (Europe/Paris)
    post_2024 = date_engagement_ts is not None and date_engagement_ts >= SEUIL_2024
    if post_2024:
        table = {0: "Fenêtre(s) de toiture", 1: "Double(s) fenêtre(s)", 2: "Autre(s) fenêtre(s)"}
    else:
        table = {0: "Fenêtre(s) de toiture", 1: "Autre(s) fenêtre(s)"}
    return table.get(valeur, valeur)


SEUIL_2024_TS = 1704063600000  # 01/01/2024 00:00:00 (Europe/Paris)


def champs_en104(date_engagement_ts):
    """BAR-EN-104 : suite à une mise à jour du logiciel Odicee, la fiche a changé de clés JSON
    pour la marque/référence de menuiserie selon la date d'engagement, et le champ surface n'existe
    que sur la version récente :
    - Avant le 01/01/2024 (ancienne version) : marque_isolant / reference_isolant, pas de surface_fenetres
    - À partir du 01/01/2024 (nouvelle version) : marque_fenetre / reference_fenetre / surface_fenetres
    Retourne la liste (cle, label, unite, critique) adaptée à la version de la fiche."""
    post_2024 = date_engagement_ts is not None and date_engagement_ts >= SEUIL_2024_TS
    base = [
        ("type_fenetre", "Type de menuiserie", "", True),
        ("nombre_de_fenetres_ou_portefenetres", "Quantité", "", True),
        ("coefficient_surfacique", "Coefficient Uw (W/m².K)", "W/m².K", True),
        ("facteur_solaire_sw", "Facteur solaire Sw", "", False),
    ]
    if post_2024:
        base += [
            ("marque_fenetre", "Marque", "", True),
            ("reference_fenetre", "Référence", "", False),
            ("surface_fenetres", "Surface des fenêtres (m²)", "m²", False),
        ]
    else:
        base += [
            ("marque_isolant", "Marque", "", True),
            ("reference_isolant", "Référence", "", False),
        ]
    return base