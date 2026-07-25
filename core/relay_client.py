"""
core/relay_client.py — Client HTTPS vers le relais gestion_scolaire.

Remplace les appels directs à Google (gspread) pour les opérations migrées.
En cas d'échec réseau, lève RelayIndisponible — à charge de l'appelant
(sheets_manager.py) de retomber sur le cache local existant, comme il le
fait déjà aujourd'hui pour les coupures réseau vers Google.
"""

import requests

# À déplacer dans config.py une fois l'URL Render connue.
RELAY_URL = "https://gestion-scolaire-relay.onrender.com"
TIMEOUT_SECONDES = 60  # généreux : le service gratuit peut mettre du temps à se réveiller


class RelayIndisponible(Exception):
    """Levée quand le relais ne répond pas (réseau, service down, etc.)."""
    pass


class IdentifiantsInvalides(Exception):
    """Levée quand la clé API de ce poste est invalide/révoquée."""
    pass


def _cle_api() -> str:
    """Lit la clé API de cet établissement (fichier local, pas versionné)."""
    from core.config_local import dossier_donnees_utilisateurs
    chemin = dossier_donnees_utilisateurs() / "relay_api_key.txt"
    if not chemin.exists():
        raise RelayIndisponible("Aucune clé API configurée pour ce poste (relay_api_key.txt manquant).")
    return chemin.read_text(encoding="utf-8").strip()


def verifier_connexion(email: str, mot_de_passe: str):
    """
    Appelle le relais pour vérifier les identifiants.
    Retourne le dict utilisateur si OK, None si identifiants invalides.
    Lève RelayIndisponible si le service ne répond pas du tout.
    """
    try:
        reponse = requests.post(
            f"{RELAY_URL}/verifier_connexion",
            json={"email": email, "mot_de_passe": mot_de_passe},
            headers={"X-API-Key": _cle_api()},
            timeout=TIMEOUT_SECONDES,
        )
    except requests.RequestException as e:
        raise RelayIndisponible(str(e))

    if reponse.status_code == 200:
        return reponse.json().get("utilisateur")
    if reponse.status_code == 401:
        return None
    raise RelayIndisponible(f"Réponse inattendue du relais ({reponse.status_code}).")
