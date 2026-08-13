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
from datetime import date
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


@app.route("/append_rows", methods=["POST"])
def append_rows():
    """
    Route générique : ajoute une ou plusieurs lignes à un onglet donné.
    Utilisée par sync_queue.py (côté appli) pour rejouer la file d'attente
    hors-ligne via le relais au lieu de gspread direct — commune à TOUS les
    modules qui font des ajouts (élèves, notes, absences, écolage, annonces...),
    pas seulement Élèves.
    """
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
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


@app.route("/get_records", methods=["POST"])
def get_records():
    """Lit tous les enregistrements d'un onglet (équivalent get_all_records)."""
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    head = corps.get("head", 1)

    if not onglet:
        return jsonify({"erreur": "onglet requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        records = ws.get_all_records(head=head)
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"records": records})


@app.route("/modifier_ligne", methods=["POST"])
def modifier_ligne():
    """
    Trouve une ligne par une valeur dans une colonne donnée, et la remplace.
    Note : colonne_debut est une lettre simple (A-Z) — suffisant tant
    qu'aucun onglet migré n'a plus de 26 colonnes.
    """
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
        colonne_fin = chr(ord(colonne_debut) + len(nouvelle_ligne) - 1)
        ws.update(f"{colonne_debut}{cell.row}:{colonne_fin}{cell.row}", [nouvelle_ligne])
    except Exception as e:
        return jsonify({"erreur": f"Impossible de modifier l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


@app.route("/supprimer_ligne", methods=["POST"])
def supprimer_ligne():
    """Trouve une ligne par une valeur dans une colonne donnée, et la supprime.
    Silencieux si non trouvée (cohérent avec le comportement existant côté appli)."""
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
    except Exception as e:
        return jsonify({"erreur": f"Impossible de supprimer dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


@app.route("/modifier_cellule", methods=["POST"])
def modifier_cellule():
    """
    Trouve une ligne par DEUX critères combinés (colonne_recherche_1 ==
    valeur_recherche_1 ET colonne_recherche_2 == valeur_recherche_2), et met
    à jour uniquement la cellule colonne_cible. Utile quand un seul critère
    (comme dans /modifier_ligne) risquerait de matcher la mauvaise ligne
    (ex : un élève avec plusieurs paiements dans ECOLAGE).
    """
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")
    colonne_recherche_1 = corps.get("colonne_recherche_1")
    valeur_recherche_1 = corps.get("valeur_recherche_1")
    colonne_recherche_2 = corps.get("colonne_recherche_2")
    valeur_recherche_2 = corps.get("valeur_recherche_2")
    colonne_cible = corps.get("colonne_cible")
    nouvelle_valeur = corps.get("nouvelle_valeur")

    if not onglet or None in (colonne_recherche_1, valeur_recherche_1,
                               colonne_recherche_2, valeur_recherche_2,
                               colonne_cible, nouvelle_valeur):
        return jsonify({"erreur": "onglet, colonne_recherche_1/2, valeur_recherche_1/2, "
                                   "colonne_cible et nouvelle_valeur requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        toutes_lignes = ws.get_all_values()

        for i, ligne in enumerate(toutes_lignes[1:], start=2):  # ligne 1 = en-têtes
            val_1 = ligne[colonne_recherche_1 - 1] if len(ligne) >= colonne_recherche_1 else ""
            val_2 = ligne[colonne_recherche_2 - 1] if len(ligne) >= colonne_recherche_2 else ""
            if str(val_1).strip() == str(valeur_recherche_1).strip() and \
               str(val_2).strip() == str(valeur_recherche_2).strip():
                ws.update_cell(i, colonne_cible, nouvelle_valeur)
                return jsonify({"status": "ok"})

        return jsonify({"erreur": "Ligne introuvable pour ces critères."}), 404
    except Exception as e:
        return jsonify({"erreur": f"Impossible de modifier l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502


@app.route("/remplacer_lignes_cle", methods=["POST"])
def remplacer_lignes_cle():
    """
    Remplace, en UN seul aller-retour, toutes les lignes d'un onglet où
    colonne_cle vaut valeur_cle par nouvelles_lignes (garde le reste de
    l'onglet intact, garde la ligne d'en-têtes). Le filtrage + la
    réécriture se font ici, côté serveur — évite au poste local de faire
    un aller-retour par ligne à supprimer.
    """
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
        ws.update("A1", lignes_finales)
    except Exception as e:
        return jsonify({"erreur": f"Impossible de remplacer dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})

@app.route("/get_values", methods=["POST"])
def get_values():
    """Lit toutes les valeurs brutes (grille) d'un onglet — équivalent
    get_all_values(). Utile pour les feuilles à structure libre (comme
    CONFIGURATION, plusieurs blocs de colonnes côte à côte) que
    get_records() (get_all_records, une seule table à en-têtes) ne peut
    pas gérer."""
    sheet_id, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    onglet = corps.get("onglet")

    if not onglet:
        return jsonify({"erreur": "onglet requis."}), 400

    try:
        wb = client_gspread().open_by_key(sheet_id)
        ws = wb.worksheet(onglet)
        valeurs = ws.get_all_values()
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"valeurs": valeurs})


@app.route("/ecrire_cellule", methods=["POST"])
def ecrire_cellule():
    """
    Écrit une valeur dans UNE cellule précise (ligne/colonne, sans recherche).
    Utile pour insérer dans la première case libre d'une colonne dans un
    onglet à structure libre (ex : CONFIGURATION — plusieurs blocs de
    colonnes côte à côte, où le "prochain emplacement libre" est déterminé
    côté appli à partir de /get_values, pas par une recherche de valeur).
    """
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
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans l'onglet {onglet!r} : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


def registre_sheet_id():
    """ID FIXE du classeur Registre (années scolaires) — un seul, partagé,
    indépendant de la clé API de chaque établissement."""
    sid = os.environ.get("REGISTRE_SHEET_ID")
    if not sid:
        raise RuntimeError("Variable d'environnement REGISTRE_SHEET_ID manquante.")
    return sid


NOM_ONGLET_ANNEES = "ANNEES"


@app.route("/registre/annee_active", methods=["POST"])
def registre_annee_active():
    """Retourne la ligne (dict) de l'année marquée 'Active' dans le
    registre, ou {"annee": None} si aucune."""
    _, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    try:
        wb = client_gspread().open_by_key(registre_sheet_id())
        ws = wb.worksheet(NOM_ONGLET_ANNEES)
        lignes = ws.get_all_records()
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire le registre : {type(e).__name__} - {e}"}), 502

    for ligne in lignes:
        if ligne.get("Statut") == "Active":
            return jsonify({"annee": ligne})
    return jsonify({"annee": None})


@app.route("/registre/enregistrer_nouvelle_annee", methods=["POST"])
def registre_enregistrer_nouvelle_annee():
    """Ajoute une nouvelle ligne 'Active' dans le registre — appelé à la
    clôture d'année, juste après la création du nouveau classeur."""
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
        ws = wb.worksheet(NOM_ONGLET_ANNEES)
        ws.append_row([annee, spreadsheet_id, "Active", ""])
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'écrire dans le registre : {type(e).__name__} - {e}"}), 502

    return jsonify({"status": "ok"})


@app.route("/registre/archiver_annee", methods=["POST"])
def registre_archiver_annee():
    """Passe une année de 'Active' à 'Archivée', avec la date du jour."""
    _, erreur = _verifier_cle_api()
    if erreur:
        return erreur

    corps = request.get_json(silent=True) or {}
    annee = corps.get("annee")
    if not annee:
        return jsonify({"erreur": "annee requis."}), 400

    try:
        wb = client_gspread().open_by_key(registre_sheet_id())
        ws = wb.worksheet(NOM_ONGLET_ANNEES)
        cell = ws.find(annee)
        if not cell:
            return jsonify({"erreur": f"Année introuvable dans le registre : {annee}"}), 404
        ws.update_cell(cell.row, 3, "Archivée")
        ws.update_cell(cell.row, 4, date.today().strftime("%d/%m/%Y"))
    except Exception as e:
        return jsonify({"erreur": f"Impossible d'archiver dans le registre : {type(e).__name__} - {e}"}), 502

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
        return jsonify({"erreur": f"Erreur API Google ({e.response.status_code}) sur sheet_id={sheet_id!r} : {detail[:500]}"}), 502
    except gspread.exceptions.SpreadsheetNotFound as e:
        # SpreadsheetNotFound cache la vraie réponse HTTP de Google dans ses
        # arguments — on va la chercher pour voir le message réel au lieu
        # du résumé générique "<Response [404]>".
        reponse_brute = e.args[0] if e.args else None
        detail = getattr(reponse_brute, "text", str(e))
        return jsonify({"erreur": f"SpreadsheetNotFound sur sheet_id={sheet_id!r} — détail Google : {detail[:500]}"}), 502
    except gspread.exceptions.WorksheetNotFound:
        return jsonify({"erreur": f"Onglet 'UTILISATEURS' introuvable dans le classeur sheet_id={sheet_id!r}."}), 502
    except Exception as e:
        return jsonify({"erreur": f"Impossible de lire le classeur (sheet_id={sheet_id!r}) : {type(e).__name__} - {e}"}), 502

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
