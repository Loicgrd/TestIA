"""
Surlignage PDF — Odicee <-> Prestataire IA
============================================

Objectif : à partir du ZIP des pièces jointes d'un dossier, repérer visuellement
sur chaque PDF (scanné ou non) où se trouvent les valeurs extraites par le
prestataire (JSON d'analyse) et les valeurs déclarées dans Odicee (JSON dossier),
en les surlignant de deux couleurs différentes.

Entrées :
  1. Le ZIP contenant les PDF du dossier (tel que déposé sur Odicee)
  2. Le JSON Odicee du dossier
  3. Le JSON produit par le prestataire

Fonctionnement :
  - Pour chaque document du rapport prestataire (report.documents[]), on retrouve
    le PDF correspondant dans le ZIP via fileName, et on surligne en JAUNE les
    valeurs que le prestataire dit avoir extraites de CE document précis.
  - Les valeurs du formData Odicee (+ adresse, SIRET, raison sociale) n'ont pas
    de document associé dans Odicee : on les recherche sur TOUS les PDF du
    dossier et on les surligne en BLEU.
  - PDF avec calque texte : recherche directe (fitz.search_for).
  - PDF scanné (image pure) : OCR page par page (Tesseract, lang='fra') pour
    obtenir la position des mots, puis recherche par séquence de tokens.

Limites connues :
  - L'OCR peut manquer une valeur mal numérisée (page penchée, contraste faible,
    date coupée...) : dans ce cas la valeur n'est simplement pas surlignée,
    plutôt que de risquer un mauvais positionnement.
  - Dépendance système : Tesseract OCR doit être installé sur le serveur
    (`apt install tesseract-ocr tesseract-ocr-fra`), ce n'est pas un simple
    package pip.
"""

import streamlit as st
import json
import re
import unicodedata
import zipfile
import io
from datetime import datetime

import fitz  # PyMuPDF
from PIL import Image

try:
    import pytesseract
    pytesseract.get_tesseract_version()  # lève TesseractNotFoundError si le binaire système est absent
    OCR_DISPONIBLE = True
except ImportError:
    OCR_DISPONIBLE = False
except Exception:
    # pytesseract.pytesseract.TesseractNotFoundError (ou toute autre erreur d'accès au binaire) :
    # le package Python est là mais le binaire système ne l'est pas — cas typique Streamlit Cloud
    # sans packages.txt. On désactive l'OCR plutôt que de planter l'app.
    OCR_DISPONIBLE = False

st.set_page_config(page_title="Surlignage PDF — Odicee / Prestataire", layout="wide")

COULEUR_PRESTA = (1, 0.85, 0)      # jaune — trouvée uniquement côté prestataire
COULEUR_ODICEE = (0.9, 0.2, 0.2)   # rouge — trouvée uniquement côté Odicee
COULEUR_ACCORD = (0.2, 0.75, 0.3)  # vert  — trouvée aux deux endroits (même position)

# Champs à ignorer systématiquement (bruit, non recherchable tel quel sur un PDF)
CHAMPS_IGNORES = {"confidences", "rgeQualifications", "hasOwnerSignature", "hasOwnerStamp",
                   "ownerSignatureVision"}


# ─────────────────────────────────────────────
# EXTRACTION DES VALEURS À SURLIGNER
# ─────────────────────────────────────────────

def valeurs_presta_document(doc_presta):
    """Aplati extractedFields (+ technicalFields) d'un document prestataire en une liste
    de (label, valeur texte) exploitable pour la recherche, en écartant listes/dicts/booléens
    et les valeurs trop courtes pour être recherchées de façon fiable."""
    ef = doc_presta.get("extractedFields") or {}
    valeurs = []
    for cle, val in ef.items():
        if cle in CHAMPS_IGNORES:
            continue
        if cle == "technicalFields" and isinstance(val, dict):
            for cle2, val2 in val.items():
                if val2 not in (None, "") and not isinstance(val2, (dict, list, bool)):
                    valeurs.append((cle2, val2))
            continue
        if val not in (None, "") and not isinstance(val, (dict, list, bool)):
            valeurs.append((cle, val))
    return [(l, v) for l, v in valeurs if len(str(v).strip()) >= 3]


def valeurs_odicee_dossier(fd, lot, data):
    """Rassemble les valeurs Odicee jugées repérables sur un PDF : champs techniques du
    lot (formData), adresse des travaux, et identité du professionnel — pas de notion de
    document associé côté Odicee, ces valeurs sont donc cherchées sur tous les PDF."""
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


# ─────────────────────────────────────────────
# RECHERCHE & SURLIGNAGE
# ─────────────────────────────────────────────

def tokenize(s):
    s = str(s).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.findall(r"[a-z0-9]+", s)


def chaines_recherche_texte(valeur):
    """Chaînes littérales à essayer avec page.search_for() (sous-chaîne exacte, pas de
    tokenisation) : la valeur telle quelle, et ses formats de date usuels."""
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


def variantes_valeur(valeur):
    """Génère les variantes de tokens à chercher en OCR pour une valeur donnée : la valeur
    telle quelle, et — si elle ressemble à une date — ses représentations JJ/MM/AAAA (les
    PDF affichent rarement le format ISO renvoyé par le prestataire)."""
    variantes = [tokenize(valeur)]
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
    """OCR une page (mise en cache par dossier PDF + index de page pour ne pas relancer
    Tesseract à chaque interaction). Retourne une liste de mots avec leur position en
    points PDF (coordonnées déjà ramenées à l'échelle 1:1 du document)."""
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
        # Binaire système absent (ex: packages.txt manquant sur Streamlit Cloud) — on renvoie
        # une page vide plutôt que de faire planter l'app ; le tableau "non localisées" le reflète.
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
    """Cherche une valeur dans les mots OCR d'une page (séquence de tokens contigus, ou
    correspondance de préfixe pour les identifiants longs mal lus comme un SIRET)."""
    flat = []
    for wi, w in enumerate(mots):
        for tok in tokenize(w["text"]):
            flat.append((tok, wi))
    toks_only = [t for t, _ in flat]

    for cand in variantes_valeur(valeur):
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

    val_tokens = tokenize(valeur)
    if len(val_tokens) == 1 and len(val_tokens[0]) >= 6:
        v = val_tokens[0]
        for tok, wi in flat:
            if len(tok) >= 6 and tok[:6] == v[:6]:
                return fitz.Rect(mots[wi]["rect"])
    return None


def _rects_pour_valeur(page, pdf_bytes, valeur, ocr_active):
    """Retourne la liste des rectangles où une valeur apparaît sur cette page (calque texte
    ou OCR selon le cas)."""
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
    """Retourne un nouveau PDF (bytes) avec les valeurs surlignées, colorées selon la ou
    les source(s) qui les confirment à un même endroit :
    - 🟩 vert  : trouvée à la fois côté Odicee ET côté prestataire (au même endroit)
    - 🟥 rouge : trouvée seulement côté Odicee
    - 🟨 jaune : trouvée seulement côté prestataire
    valeurs_presta / valeurs_odicee : listes de (label, valeur)."""
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
                couleur = COULEUR_ODICEE
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




# ─────────────────────────────────────────────
# HELPERS DIVERS (repris de l'app de comparaison)
# ─────────────────────────────────────────────

def get_odicee_lots_bar(data):
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


def trouver_fichier_zip(zip_files, nom_cible):
    """Retrouve un fichier du ZIP correspondant au fileName du prestataire, en tolérant
    les écarts d'espaces/casse/tirets entre les deux systèmes."""
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

st.title("🖍️ Surlignage PDF — Odicee vs Prestataire IA")
st.caption(
    "Repère visuellement, sur les PDF du dossier, où se trouvent les valeurs extraites par "
    "le prestataire et les valeurs déclarées dans Odicee — "
    "🟩 trouvée aux deux endroits · 🟥 Odicee seul · 🟨 prestataire seul."
)

if not OCR_DISPONIBLE:
    st.warning(
        "⚠️ Tesseract OCR n'est pas disponible sur ce serveur — seuls les PDF avec calque "
        "texte pourront être surlignés (les PDF scannés seront ignorés).\n\n"
        "**Sur Streamlit Community Cloud** : ajoutez un fichier `packages.txt` à la racine du "
        "repo contenant les lignes `tesseract-ocr` et `tesseract-ocr-fra`, puis redémarrez "
        "l'app (Manage app → Reboot).\n\n"
        "**Sur un serveur classique** : `apt install tesseract-ocr tesseract-ocr-fra`, "
        "en plus de `pip install pytesseract`."
    )

col1, col2, col3 = st.columns(3)
with col1:
    fichier_zip = st.file_uploader("ZIP des PDF du dossier", type="zip")
with col2:
    fichier_odicee = st.file_uploader("JSON Odicee (dossier)", type="json", key="odicee_h")
with col3:
    fichier_presta = st.file_uploader("JSON Prestataire (rapport)", type="json", key="presta_h")

if not (fichier_zip and fichier_odicee and fichier_presta):
    st.info("Chargez le ZIP et les deux JSON pour lancer le surlignage.")
    st.stop()

try:
    zf = zipfile.ZipFile(fichier_zip)
    pdfs_zip = {n: zf.read(n) for n in zf.namelist() if n.lower().endswith(".pdf")}
except Exception as e:
    st.error(f"ZIP invalide : {e}")
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
documents_presta = report.get("documents", []) or []

lots_par_fiche = get_odicee_lots_bar(data)
fiche_odicee_match = next((f for f in lots_par_fiche if f.upper() == fiche_presta), None)
if not fiche_odicee_match:
    st.error(f"Aucun lot Odicee avec la fiche **{fiche_presta}** dans ce dossier.")
    st.stop()

lots_sites = lots_par_fiche[fiche_odicee_match]
if len(lots_sites) > 1:
    adresses = [a for _, a in lots_sites]
    choix = st.selectbox("Site à comparer :", adresses)
    lot, adresse_site = next((l, a) for l, a in lots_sites if a == choix)
else:
    lot, adresse_site = lots_sites[0]
fd = lot.get("formData", {}) or {}

valeurs_odicee = valeurs_odicee_dossier(fd, lot, data)

st.markdown("---")
# L'attestation sur l'honneur (AH) est exclue du surlignage : c'est une déclaration signée par
# le bénéficiaire, pas une pièce probante à recouper visuellement comme une facture ou un bon
# de commande — la surligner n'a pas de sens métier ici et peut induire en erreur.
DOC_TYPES_EXCLUS = {"HonorAttestation"}

documents_presta = [d for d in documents_presta if d.get("type") not in DOC_TYPES_EXCLUS]
if not documents_presta:
    st.warning("Aucun document surlignable dans ce rapport (l'attestation sur l'honneur est exclue).")
    st.stop()

noms_docs = [d.get("fileName") for d in documents_presta if d.get("fileName")]
doc_choisi_nom = st.selectbox("Document à surligner :", noms_docs)
doc_choisi = next(d for d in documents_presta if d.get("fileName") == doc_choisi_nom)

nom_fichier_zip = trouver_fichier_zip(pdfs_zip, doc_choisi_nom)
if not nom_fichier_zip:
    st.error(
        f"Le fichier **{doc_choisi_nom}** annoncé par le prestataire est introuvable dans le ZIP. "
        f"Fichiers disponibles : {', '.join(pdfs_zip) or '—'}"
    )
    st.stop()

valeurs_presta = valeurs_presta_document(doc_choisi)

with st.expander(f"🟨 {len(valeurs_presta)} valeur(s) prestataire recherchée(s) sur ce document"):
    st.write({l: v for l, v in valeurs_presta})
with st.expander(f"🟥 {len(valeurs_odicee)} valeur(s) Odicee recherchée(s) (tous documents)"):
    st.write({l: v for l, v in valeurs_odicee})

pdf_bytes = pdfs_zip[nom_fichier_zip]
nb_pages = fitz.open(stream=pdf_bytes, filetype="pdf").page_count
a_calque_texte = any(
    len(fitz.open(stream=pdf_bytes, filetype="pdf")[i].get_text().strip()) > 20
    for i in range(nb_pages)
)
if not a_calque_texte:
    st.caption(f"📄 Document scanné ({nb_pages} page(s)) — traitement par OCR, peut prendre quelques secondes.")

with st.spinner("Surlignage en cours..."):
    pdf_annote, trouvees, non_trouvees = surligner_pdf(pdf_bytes, valeurs_presta, valeurs_odicee)

c1, c2 = st.columns(2)
c1.metric("Valeurs localisées", len(trouvees))
c2.metric("Valeurs non localisées", len(non_trouvees))
if non_trouvees:
    with st.expander("⚪ Valeurs non localisées sur ce document (OCR/format non reconnu)"):
        for l, v in non_trouvees:
            st.caption(f"{l} : {v}")

st.download_button(
    "📥 Télécharger ce PDF surligné",
    data=pdf_annote,
    file_name=f"surligne_{doc_choisi_nom}",
    mime="application/pdf",
)

st.markdown("---")
st.markdown("#### 🗂️ Tous les documents du dossier")
st.caption(
    "Génère un ZIP avec chaque document du dossier surligné (hors attestation sur l'honneur). "
    "Peut prendre du temps si plusieurs PDF sont scannés (OCR page par page)."
)
if st.button("Générer le ZIP de tous les documents surlignés"):
    zip_buffer = io.BytesIO()
    with st.spinner("Surlignage de tous les documents en cours..."):
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zout:
            barre = st.progress(0.0)
            for i, doc_p in enumerate(documents_presta):
                nom_p = doc_p.get("fileName")
                nom_zip_p = trouver_fichier_zip(pdfs_zip, nom_p)
                if not nom_zip_p:
                    st.caption(f"⚠️ {nom_p} introuvable dans le ZIP — ignoré.")
                    continue
                pdf_bytes_p = pdfs_zip[nom_zip_p]
                valeurs_presta_p = valeurs_presta_document(doc_p)
                pdf_annote_p, _, _ = surligner_pdf(pdf_bytes_p, valeurs_presta_p, valeurs_odicee)
                zout.writestr(f"surligne_{nom_p}", pdf_annote_p)
                barre.progress((i + 1) / len(documents_presta))
    st.download_button(
        "📥 Télécharger le ZIP",
        data=zip_buffer.getvalue(),
        file_name=f"surlignage_{data.get('id')}.zip",
        mime="application/zip",
    )

st.markdown("---")
doc_rendu = fitz.open(stream=pdf_annote, filetype="pdf")
for i in range(doc_rendu.page_count):
    pix = doc_rendu[i].get_pixmap(matrix=fitz.Matrix(1.8, 1.8))
    st.image(pix.tobytes("png"), caption=f"Page {i + 1}/{doc_rendu.page_count}", use_container_width=True)
