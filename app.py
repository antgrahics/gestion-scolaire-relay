"""
app.py — Relais gestion_scolaire

Rôle : porter credentials.json (compte de service Google) UNE SEULE FOIS,
côté serveur, au lieu d'une copie sur chaque poste (secrétaire, prof,
surveillant, censeur...). Les postes locaux appellent ce service en HTTPS,
avec une clé API propre à leur établissement — jamais Google directement.

Phase 2 — pilote : un seul endpoint (/verifier_connexion) pour valider
l'approche de bout en bout avant de migrer le reste (élèves, notes,
absences, écolage...).

Variables d'environnement nécessaires (configurées sur Render, jamais
écrites dans un fichier versionné) :
  - GOOGLE_CREDENTIALS_JSON : le contenu complet de credentials.json (le JSON
    du compte de service), collé tel quel comme valeur de variable d'env.
  - ETABLISSEMENTS_JSON : mapping clé API -> Sheet_ID, ex:
    {"cle-api-olivier": "1T_-_ESM4_fiz8q4WbYgr7k6wFegsl_HP0cPeegHhx4"}
"""

import os
import json
import bcrypt
import hashlib
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_client_gspread = None  # authentifié une seule fois, réutilisé (cache mémoire)


def client_gspread():
    """Authentifie le compte de service à partir de la variable d'environnement,
    jamais depuis un fichier sur disque. Mis en cache en mémoire pour ne pas
    refaire l'authentification à chaque requête."""
    global _client_gspread
    if _client_gspread is None:
        brut = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if not brut:
            raise RuntimeError("Variable d'environnement GOOGLE_CREDENTIALS_JSON manquante.")
        infos = json.loads(brut)
        creds = Credentials.from_service_account_info(infos, scopes=SCOPES)
        _client_gspread = gspread.authorize(creds)
    return _client_gspread


def sheet_id_pour_cle_api(cle_api: str):
    """Retourne le Sheet_ID de l'établissement associé à cette clé API, ou None."""
    brut = os.environ.get("ETABLISSEMENTS_JSON", "{}")
    mapping = json.loads(brut)
    return mapping.get(cle_api)


def _verifier_cle_api():
    """Vérifie le header X-API-Key. Retourne (sheet_id, None) si valide,
    (None, réponse_erreur) sinon."""
    cle_api = request.headers.get("X-API-Key", "")
    sheet_id = sheet_id_pour_cle_api(cle_api)
    if not sheet_id:
        return None, (jsonify({"erreur": "Clé API invalide ou inconnue."}), 401)
    return sheet_id, None


@app.route("/health", methods=["GET"])
def health():
    """Route de vie — utilisée aussi par le ping GitHub Actions pour
    empêcher le service de s'endormir pendant les heures d'école."""
    return jsonify({"status": "ok"})


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
    except gspread.exceptions.APIError as e:
        detail = getattr(e.response, "text", str(e))
        return jsonify({"erreur": f"Erreur API Google ({e.response.status_code}) : {detail[:500]}"}), 502
    except gspread.exceptions.WorksheetNotFound:
        return jsonify({"erreur": "Onglet 'UTILISATEURS' introuvable dans ce classeur."}), 502
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire le classeur : {type(e).__name__} - {e}"}), 502

    for u in utilisateurs:
        if u.get("Email", "").lower() != email.lower() or u.get("Actif", "") != "OUI":
            continue

        hash_stocke = u.get("Mot_de_passe_hash", "")

        if hash_stocke.startswith("$2a$") or hash_stocke.startswith("$2b$") or hash_stocke.startswith("$2y$"):
            try:
                if bcrypt.checkpw(mot_de_passe.encode("utf-8"), hash_stocke.encode("utf-8")):
                    return jsonify({"utilisateur": u})
            except ValueError:
                pass
            return jsonify({"erreur": "Identifiants invalides."}), 401

        # Ancien format SHA-256 : vérifié une dernière fois, puis migré en bcrypt
        if hash_stocke == hashlib.sha256(mot_de_passe.encode()).hexdigest():
            nouveau_hash = bcrypt.hashpw(mot_de_passe.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            try:
                cell = ws.find(str(u.get("ID")), in_column=1)
                if cell:
                    entetes = ws.row_values(1)
                    col = entetes.index("Mot_de_passe_hash") + 1 if "Mot_de_passe_hash" in entetes else 4
                    ws.update_cell(cell.row, col, nouveau_hash)
            except Exception:
                pass  # migration best-effort, ne bloque jamais la connexion
            u["Mot_de_passe_hash"] = nouveau_hash
            return jsonify({"utilisateur": u})

        return jsonify({"erreur": "Identifiants invalides."}), 401

    return jsonify({"erreur": "Identifiants invalides."}), 401


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
