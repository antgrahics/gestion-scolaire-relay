"""
app.py — Relais gestion_scolaire (VERSION CLIENTS WEB)

Variables d'environnement Render :
  - GOOGLE_CREDENTIALS_JSON : contenu JSON du compte de service
  - REGISTRE_SHEET_ID : ID fixe du classeur Registre
  - ETABLISSEMENTS_JSON (optionnel) : fallback JSON statique
"""

import time
import os
import json
import bcrypt
import hashlib
import secrets
from datetime import date, datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ── CORS : autorise les apps web à appeler le relais ───────────────────────
CORS(app, origins=[
    "https://app-notes.onrender.com",
    "https://portail-parents.onrender.com",
    "http://localhost:5000",
    "http://localhost:5001",
    "http://127.0.0.1:5000",
    "http://127.0.0.1:5001",
])

# ── SCOPES ───────────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── CLIENT GSPREAD (cache mémoire) ─────────────────────────────────────────
_client_gspread = None


def _clean_env_json(raw: str) -> str:
    """Nettoie les échappements de Render (guillemets et \n échappés)."""
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace(r'\n', '\n').replace(r'\"', '"')
    return raw


def client_gspread():
    """Authentifie le compte de service depuis la variable d'environnement."""
    global _client_gspread
    if _client_gspread is None:
        brut = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
        if not brut:
            raise RuntimeError("Variable d'environnement GOOGLE_CREDENTIALS_JSON manquante.")
        try:
            infos = json.loads(_clean_env_json(brut))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"GOOGLE_CREDENTIALS_JSON invalide : {e}")
        creds = Credentials.from_service_account_info(infos, scopes=SCOPES)
        _client_gspread = gspread.authorize(creds)
    return _client_gspread


def registre_sheet_id():
    sid = os.environ.get("REGISTRE_SHEET_ID")
    if not sid:
        raise RuntimeError("Variable d'environnement REGISTRE_SHEET_ID manquante.")
    return sid


# ── ETABLISSEMENTS (mapping clé API → Sheet_ID) ──────────────────────────────
_cache_etablissements = {"data": {}, "expires": 0}


def _charger_etablissements():
    """Charge le mapping depuis ETABLISSEMENTS_JSON (env) ou le Registre Sheets."""
    global _cache_etablissements
    maintenant = time.time()

    if maintenant < _cache_etablissements["expires"]:
        return _cache_etablissements["data"]

    mapping = {}
    brut = os.environ.get("ETABLISSEMENTS_JSON", "{}")
    try:
        mapping.update(json.loads(_clean_env_json(brut)))
    except json.JSONDecodeError:
        pass

    try:
        wb = client_gspread().open_by_key(registre_sheet_id())
        try:
            ws = wb.worksheet("ETABLISSEMENTS")
            for row in ws.get_all_records():
                cle = str(row.get("Cle_API", "")).strip()
                sid = str(row.get("Sheet_ID", "")).strip()
                if cle and sid:
                    mapping[cle] = sid
        except gspread.exceptions.WorksheetNotFound:
            pass
    except Exception as e:
        print(f"[WARN] Impossible de lire le Registre ETABLISSEMENTS : {e}")

    _cache_etablissements = {"data": mapping, "expires": maintenant + 300}
    return mapping


def sheet_id_pour_cle_api(cle_api: str):
    return _charger_etablissements().get(cle_api)


def _verifier_cle_api():
    """Vérifie le header X-API-Key."""
    cle_api = request.headers.get("X-API-Key", "").strip()
    sheet_id = sheet_id_pour_cle_api(cle_api)
    if not sheet_id:
        return None, (jsonify({"erreur": "Clé API invalide ou inconnue."}), 401)
    return sheet_id, None


# ── CACHE DE LECTURE ────────────────────────────────────────────────────────
_cache_lectures = {}
_DUREE_CACHE_LECTURE = 20


def _cle_cache_lecture(sheet_id, onglet, suffixe=""):
    return (sheet_id, onglet, suffixe)


def _lire_avec_cache(cle, fonction_lecture):
    maintenant = time.time()
    entree = _cache_lectures.get(cle)
    if entree and maintenant < entree["expires"]:
        return entree["data"]
    data = _appel_avec_retry(fonction_lecture)
    _cache_lectures[cle] = {"data": data, "expires": maintenant + _DUREE_CACHE_LECTURE}
    return data


def _invalider_cache_onglet(sheet_id, onglet):
    for cle in list(_cache_lectures.keys()):
        if cle[0] == sheet_id and cle[1] == onglet:
            _cache_lectures.pop(cle, None)


def _appel_avec_retry(fonction, tentatives=4, delai_initial=2):
    derniere_erreur = None
    for tentative in range(tentatives):
        try:
            return fonction()
        except Exception as e:
            derniere_erreur = e
            msg = str(e)
            if "429" in msg or "Quota exceeded" in msg or "Rate limit" in msg:
                if tentative < tentatives - 1:
                    pause = delai_initial * (2 ** tentative)
                    print(f"[Retry {tentative+1}/{tentatives}] Quota dépassé, attente {pause}s...")
                    time.sleep(pause)
                    continue
            raise
    raise derniere_erreur


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/enregistrer_etablissement", methods=["POST"])
def enregistrer_etablissement():
    corps = request.get_json(silent=True) or {}
    nom = corps.get("nom_etablissement", "").strip()
    sheet_id = corps.get("sheet_id", "").strip()
    registre_id = corps.get("registre_sheet_id", "").strip()

    if not nom or not sheet_id:
        return jsonify({"erreur": "nom_etablissement et sheet_id requis."}), 400

    cle_api = f"AG-{int(time.time())}-{secrets.token_hex(8).upper()}"

    try:
        rid = registre_id or registre_sheet_id()
        wb = client_gspread().open_by_key(rid)
        try:
            ws = wb.worksheet("ETABLISSEMENTS")
        except gspread.exceptions.WorksheetNotFound:
            ws = wb.add_worksheet("ETABLISSEMENTS", rows=100, cols=4)
            ws.append_row(["Cle_API", "Sheet_ID", "Nom_Etablissement", "Date_Creation"])

        ws.append_row([
            cle_api, sheet_id, nom,
            datetime.now().strftime("%d/%m/%Y %H:%M")
        ])
        _cache_etablissements["expires"] = 0
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans le registre : {type(e).__name__} - {e}"}), 502

    return jsonify({"cle_api": cle_api, "sheet_id": sheet_id}), 200


# ── VERIFIER CONNEXION (Professeurs) ────────────────────────────────────────
@app.route("/verifier_connexion", methods=["POST"])
def verifier_connexion():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    email = (corps.get("email") or "").strip()
    mot_de_passe = corps.get("mot_de_passe") or ""

    if not email or not mot_de_passe:
        return jsonify({"erreur": "email et mot_de_passe requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet("UTILISATEURS")
        utilisateurs = ws.get_all_records()
    except gspread.exceptions.WorksheetNotFound:
        return jsonify({"erreur": "Onglet UTILISATEURS introuvable."}), 502
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire le classeur : {type(e).__name__} - {e}"}), 502

    for u in utilisateurs:
        if u.get("Email", "").lower() != email.lower() or u.get("Actif", "") != "OUI":
            continue

        hash_stocke = u.get("Mot_de_passe_hash", "")

        if hash_stocke.startswith(("$2a$", "$2b$", "$2y$")):
            try:
                if bcrypt.checkpw(mot_de_passe.encode("utf-8"), hash_stocke.encode("utf-8")):
                    return jsonify({"utilisateur": u})
            except ValueError:
                pass
            return jsonify({"erreur": "Identifiants invalides."}), 401

        if hash_stocke == hashlib.sha256(mot_de_passe.encode()).hexdigest():
            nouveau_hash = bcrypt.hashpw(mot_de_passe.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            try:
                cell = ws.find(str(u.get("ID")), in_column=1)
                if cell:
                    entetes = ws.row_values(1)
                    col = entetes.index("Mot_de_passe_hash") + 1 if "Mot_de_passe_hash" in entetes else 4
                    ws.update_cell(cell.row, col, nouveau_hash)
            except Exception:
                pass
            u["Mot_de_passe_hash"] = nouveau_hash
            return jsonify({"utilisateur": u})

        return jsonify({"erreur": "Identifiants invalides."}), 401

    return jsonify({"erreur": "Identifiants invalides."}), 401


# ── VERIFIER PARENT (NOUVEAU) ───────────────────────────────────────────────
@app.route("/verifier_parent", methods=["POST"])
def verifier_parent():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    telephone = (corps.get("telephone") or "").strip().replace(" ", "")
    code = (corps.get("code") or "").strip()

    if not telephone or not code:
        return jsonify({"erreur": "telephone et code requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet("PARENTS")
        parents = ws.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        return jsonify({"erreur": "Onglet PARENTS introuvable."}), 502
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire le classeur : {type(e).__name__} - {e}"}), 502

    for row in parents[3:]:
        if len(row) >= 2:
            tel = row[0].strip().replace(" ", "")
            code_stocke = row[1].strip()
            if tel == telephone and code_stocke == code:
                ids = [x.strip() for x in row[2].replace(";", ",").split(",") if x.strip()]
                statut = row[3].strip() if len(row) > 3 else "Actif"
                return jsonify({"valide": True, "id_eleves": ids, "statut": statut})

    return jsonify({"erreur": "Code ou téléphone incorrect."}), 401


# ── GET RECORDS ───────────────────────────────────────────────────────────────
@app.route("/get_records", methods=["POST"])
def get_records():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    head = corps.get("head", 1)

    if not onglet:
        return jsonify({"erreur": "onglet requis."}), 400

    try:
        records = _lire_avec_cache(
            _cle_cache_lecture(sheet_id, onglet, f"records:{head}"),
            lambda: client_gspread().open_by_key(sheet_id).worksheet(onglet).get_all_records(head=head)
        )
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"records": records})


# ── GET VALUES ──────────────────────────────────────────────────────────────
@app.route("/get_values", methods=["POST"])
def get_values():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")

    if not onglet:
        return jsonify({"erreur": "onglet requis."}), 400

    try:
        valeurs = _lire_avec_cache(
            _cle_cache_lecture(sheet_id, onglet, "values"),
            lambda: client_gspread().open_by_key(sheet_id).worksheet(onglet).get_all_values()
        )
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"valeurs": valeurs})


# ── APPEND ROWS ───────────────────────────────────────────────────────────────
@app.route("/append_rows", methods=["POST"])
def append_rows():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    lignes = corps.get("lignes")

    if not onglet or not lignes:
        return jsonify({"erreur": "onglet et lignes requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        ws.append_rows(lignes, value_input_option="USER_ENTERED")
        _invalider_cache_onglet(sheet_id, onglet)
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── MODIFIER LIGNE ──────────────────────────────────────────────────────────
@app.route("/modifier_ligne", methods=["POST"])
def modifier_ligne():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    valeur_recherche = corps.get("valeur_recherche")
    nouvelle_ligne = corps.get("nouvelle_ligne")
    colonne_recherche = corps.get("colonne_recherche", 1)
    colonne_debut = corps.get("colonne_debut", "A")

    if not onglet or valeur_recherche is None or nouvelle_ligne is None:
        return jsonify({"erreur": "onglet, valeur_recherche et nouvelle_ligne requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        cell = ws.find(str(valeur_recherche), in_column=colonne_recherche)
        if not cell:
            return jsonify({"erreur": f"Ligne introuvable pour {valeur_recherche!r}."}), 404

        def _col_name(index):
            name = ""
            while index >= 0:
                name = chr(index % 26 + ord('A')) + name
                index = index // 26 - 1
            return name

        start_idx = ord(colonne_debut.upper()) - ord('A')
        end_idx = start_idx + len(nouvelle_ligne) - 1
        colonne_fin = _col_name(end_idx)

        ws.update(f"{colonne_debut}{cell.row}:{colonne_fin}{cell.row}", [nouvelle_ligne])
        _invalider_cache_onglet(sheet_id, onglet)
    except Exception as e:
        return jsonify({"erreur": f"Impossible de modifier l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── SUPPRIMER LIGNE ───────────────────────────────────────────────────────────
@app.route("/supprimer_ligne", methods=["POST"])
def supprimer_ligne():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    valeur_recherche = corps.get("valeur_recherche")
    colonne_recherche = corps.get("colonne_recherche", 1)

    if not onglet or valeur_recherche is None:
        return jsonify({"erreur": "onglet et valeur_recherche requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        cell = ws.find(str(valeur_recherche), in_column=colonne_recherche)
        if cell:
            ws.delete_rows(cell.row)
            _invalider_cache_onglet(sheet_id, onglet)
    except Exception as e:
        return jsonify({"erreur": f"Impossible de supprimer dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── SUPPRIMER LIGNE CRITERES ─────────────────────────────────────────────────
@app.route("/supprimer_ligne_criteres", methods=["POST"])
def supprimer_ligne_criteres():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    criteres = corps.get("criteres")

    if not onglet or not criteres:
        return jsonify({"erreur": "onglet et criteres requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        toutes_lignes = ws.get_all_values()

        for i, ligne in enumerate(toutes_lignes[1:], start=2):
            match = True
            for c in criteres:
                col = c["colonne"]
                val = ligne[col - 1] if len(ligne) >= col else ""
                if str(val).strip() != str(c["valeur"]).strip():
                    match = False
                    break
            if match:
                ws.delete_rows(i)
                _invalider_cache_onglet(sheet_id, onglet)
                break
    except Exception as e:
        return jsonify({"erreur": f"Impossible de supprimer dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── SUPPRIMER LIGNES PLAGE ───────────────────────────────────────────────────
@app.route("/supprimer_lignes_plage", methods=["POST"])
def supprimer_lignes_plage():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    debut = corps.get("debut")
    fin = corps.get("fin")

    if not onglet or debut is None or fin is None:
        return jsonify({"erreur": "onglet, debut et fin requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        _appel_avec_retry(lambda: ws.delete_rows(debut, fin))
        _invalider_cache_onglet(sheet_id, onglet)
    except Exception as e:
        return jsonify({"erreur": f"Impossible de supprimer les lignes {debut}-{fin} de l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── MODIFIER CELLULE ────────────────────────────────────────────────────────
@app.route("/modifier_cellule", methods=["POST"])
def modifier_cellule():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    c1 = corps.get("colonne_recherche_1")
    v1 = corps.get("valeur_recherche_1")
    c2 = corps.get("colonne_recherche_2")
    v2 = corps.get("valeur_recherche_2")
    colonne_cible = corps.get("colonne_cible")
    nouvelle_valeur = corps.get("nouvelle_valeur")

    if not onglet or None in (c1, v1, c2, v2, colonne_cible, nouvelle_valeur):
        return jsonify({"erreur": "Tous les paramètres sont requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        toutes_lignes = ws.get_all_values()

        for i, ligne in enumerate(toutes_lignes[1:], start=2):
            val_1 = ligne[c1 - 1] if len(ligne) >= c1 else ""
            val_2 = ligne[c2 - 1] if len(ligne) >= c2 else ""
            if str(val_1).strip() == str(v1).strip() and str(val_2).strip() == str(v2).strip():
                ws.update_cell(i, colonne_cible, nouvelle_valeur)
                _invalider_cache_onglet(sheet_id, onglet)
                return jsonify({"status": "ok"})

        return jsonify({"erreur": "Ligne introuvable pour ces critères."}), 404
    except Exception as e:
        return jsonify({"erreur": f"Impossible de modifier l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502


# ── REMPLACER LIGNES CLE ────────────────────────────────────────────────────
@app.route("/remplacer_lignes_cle", methods=["POST"])
def remplacer_lignes_cle():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    colonne_cle = corps.get("colonne_cle")
    valeur_cle = corps.get("valeur_cle")
    nouvelles_lignes = corps.get("nouvelles_lignes")

    if not onglet or colonne_cle is None or valeur_cle is None or nouvelles_lignes is None:
        return jsonify({"erreur": "onglet, colonne_cle, valeur_cle et nouvelles_lignes requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        toutes_lignes = ws.get_all_values()

        if not toutes_lignes:
            lignes_finales = nouvelles_lignes
        else:
            entetes = toutes_lignes[0]
            lignes_finales = [entetes]
            for ligne in toutes_lignes[1:]:
                val = ligne[colonne_cle - 1] if len(ligne) >= colonne_cle else ""
                if str(val).strip() != str(valeur_cle).strip():
                    lignes_finales.append(ligne)
            lignes_finales.extend(nouvelles_lignes)

        ws.clear()
        if lignes_finales:
            ws.update("A1", lignes_finales)
        _invalider_cache_onglet(sheet_id, onglet)
    except Exception as e:
        return jsonify({"erreur": f"Impossible de remplacer dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── ECRIRE CELLULE ──────────────────────────────────────────────────────────
@app.route("/ecrire_cellule", methods=["POST"])
def ecrire_cellule():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    ligne = corps.get("ligne")
    colonne = corps.get("colonne")
    valeur = corps.get("valeur")

    if not onglet or ligne is None or colonne is None:
        return jsonify({"erreur": "onglet, ligne et colonne requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        ws.update_cell(ligne, colonne, valeur)
        _invalider_cache_onglet(sheet_id, onglet)
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── ECRIRE PARAM CONFIG ─────────────────────────────────────────────────────
@app.route("/ecrire_param_config", methods=["POST"])
def ecrire_param_config():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    colonne_cle = corps.get("colonne_cle")
    colonne_valeur = corps.get("colonne_valeur")
    cle = corps.get("cle")
    valeur = corps.get("valeur")
    ligne_depart = corps.get("ligne_depart", 1)

    if not onglet or colonne_cle is None or colonne_valeur is None or cle is None or valeur is None:
        return jsonify({"erreur": "Paramètres incomplets."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        data = ws.get_all_values()

        for i, row in enumerate(data, start=1):
            if len(row) >= colonne_cle and str(row[colonne_cle - 1]).strip().upper() == str(cle).upper():
                ws.update_cell(i, colonne_valeur, valeur)
                _invalider_cache_onglet(sheet_id, onglet)
                return jsonify({"status": "ok"})

        for i, row in enumerate(data[ligne_depart - 1:], start=ligne_depart):
            vide = len(row) < colonne_cle or not row[colonne_cle - 1]
            if vide:
                ws.update_cell(i, colonne_cle, str(cle).upper())
                ws.update_cell(i, colonne_valeur, valeur)
                _invalider_cache_onglet(sheet_id, onglet)
                return jsonify({"status": "ok"})

        nouvelle_ligne = len(data) + 1
        ws.update_cell(nouvelle_ligne, colonne_cle, str(cle).upper())
        ws.update_cell(nouvelle_ligne, colonne_valeur, valeur)
        _invalider_cache_onglet(sheet_id, onglet)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502


# ── VIDER PARAMS PREFIXE ────────────────────────────────────────────────────
@app.route("/vider_params_prefixe", methods=["POST"])
def vider_params_prefixe():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    colonne_cle = corps.get("colonne_cle")
    colonne_valeur = corps.get("colonne_valeur")
    prefixe = corps.get("prefixe")

    if not onglet or colonne_cle is None or colonne_valeur is None or not prefixe:
        return jsonify({"erreur": "Paramètres incomplets."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        data = ws.get_all_values()
        for i, row in enumerate(data, start=1):
            if len(row) >= colonne_cle and str(row[colonne_cle - 1]).strip().upper().startswith(prefixe.upper()):
                ws.update_cell(i, colonne_cle, "")
                ws.update_cell(i, colonne_valeur, "")
        _invalider_cache_onglet(sheet_id, onglet)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"erreur": f"Impossible de vider dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502


# ── REMPLACER ONGLET ──────────────────────────────────────────────────────────
@app.route("/remplacer_onglet", methods=["POST"])
def remplacer_onglet():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    lignes = corps.get("lignes")

    if not onglet or lignes is None:
        return jsonify({"erreur": "onglet et lignes requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        try:
            ws = wb.worksheet(onglet)
        except gspread.exceptions.WorksheetNotFound:
            nb_cols = max(10, len(lignes[0]) if lignes else 10)
            ws = wb.add_worksheet(title=onglet, rows="100", cols=str(nb_cols))
        ws.clear()
        if lignes:
            ws.update("A1", lignes)
        _invalider_cache_onglet(sheet_id, onglet)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"erreur": f"Impossible de remplacer l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502


# ── COLORER CELLULE ─────────────────────────────────────────────────────────
@app.route("/colorer_cellule", methods=["POST"])
def colorer_cellule():
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    colonne_recherche = corps.get("colonne_recherche")
    valeur_recherche = corps.get("valeur_recherche")
    colonne_cible = corps.get("colonne_cible")
    couleur = corps.get("couleur", "")

    if not onglet or colonne_recherche is None or valeur_recherche is None or colonne_cible is None:
        return jsonify({"erreur": "Paramètres incomplets."}), 400

    couleurs = {
        "vert":  {"red": 0.80, "green": 0.94, "blue": 0.80},
        "rouge": {"red": 0.98, "green": 0.80, "blue": 0.80},
        "":      {"red": 1.0,  "green": 1.0,  "blue": 1.0},
    }
    rgb = couleurs.get(couleur, couleurs[""])

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        toutes_lignes = ws.get_all_values()

        for i, ligne in enumerate(toutes_lignes, start=1):
            val = ligne[colonne_recherche - 1] if len(ligne) >= colonne_recherche else ""
            if str(val).strip() == str(valeur_recherche).strip():
                case = gspread.utils.rowcol_to_a1(i, colonne_cible)
                ws.format(case, {"backgroundColor": rgb})
                return jsonify({"status": "ok"})

        return jsonify({"erreur": f"Ligne introuvable pour {valeur_recherche!r}."}), 404
    except Exception as e:
        return jsonify({"erreur": f"Impossible de colorer dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502


# ── REGISTRE : ANNEE ACTIVE ─────────────────────────────────────────────────
@app.route("/registre/annee_active", methods=["POST"])
def registre_annee_active():
    _, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    try:
        wb = client_gspread().open_by_key(registre_sheet_id())
        ws = wb.worksheet("ANNEES")
        lignes = ws.get_all_records()
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire le registre : {type(e).__name__} - {e}"}), 502

    for ligne in lignes:
        if ligne.get("Statut") == "Active":
            return jsonify({"annee": ligne})
    return jsonify({"annee": None})


# ── REGISTRE : ENREGISTRER NOUVELLE ANNEE ───────────────────────────────────
@app.route("/registre/enregistrer_nouvelle_annee", methods=["POST"])
def registre_enregistrer_nouvelle_annee():
    _, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    annee = corps.get("annee")
    spreadsheet_id = corps.get("spreadsheet_id")
    if not annee or not spreadsheet_id:
        return jsonify({"erreur": "annee et spreadsheet_id requis."}), 400

    try:
        wb = client_gspread().open_by_key(registre_sheet_id())
        ws = wb.worksheet("ANNEES")
        ws.append_row([annee, spreadsheet_id, "Active", ""])
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans le registre : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ── REGISTRE : ARCHIVER ANNEE ───────────────────────────────────────────────
@app.route("/registre/archiver_annee", methods=["POST"])
def registre_archiver_annee():
    _, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    annee = corps.get("annee")
    if not annee:
        return jsonify({"erreur": "annee requis."}), 400

    try:
        wb = client_gspread().open_by_key(registre_sheet_id())
        ws = wb.worksheet("ANNEES")
        cell = ws.find(annee)
        if not cell:
            return jsonify({"erreur": f"Année introuvable dans le registre : {annee}"}), 404
        ws.update_cell(cell.row, 3, "Archivée")
        ws.update_cell(cell.row, 4, date.today().strftime("%d/%m/%Y"))
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'archiver dans le registre : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


# ═════════════════════════════════════════════════════════════════════════════
#  POINT D'ENTREE
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))