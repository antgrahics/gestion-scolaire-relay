# Relais gestion_scolaire — Phase 2 (pilote)

Petit service Flask qui porte `credentials.json` (compte de service Google)
**une seule fois, côté serveur**, au lieu d'une copie sur chaque poste.

Pour l'instant, un seul endpoint est migré : `/verifier_connexion` (le
login). Le reste (élèves, notes, absences, écolage...) suit une fois ce
pilote validé en conditions réelles.

## 1. Créer le service sur Render

1. Pousse ce dossier dans un nouveau repo GitHub (ex: `gestion-scolaire-relay`).
2. Sur Render : **New → Web Service**, connecte ce repo.
3. Runtime : Python 3. Build command : `pip install -r requirements.txt`.
   Start command : laisse vide (le `Procfile` s'en charge).
4. Choisis le plan **Free** (comme discuté).

## 2. Variables d'environnement à configurer sur Render

Dans l'onglet **Environment** du service :

- `GOOGLE_CREDENTIALS_JSON` : ouvre ton `credentials.json` local, copie **tout
  son contenu** (le JSON entier), colle-le tel quel comme valeur de cette
  variable. Ne jamais mettre ce fichier dans le repo.
- `ETABLISSEMENTS_JSON` : un JSON qui associe une clé API à chaque
  établissement, par exemple :
  ```json
  {"a1b2c3d4e5f6...": "1T_-_ESM4_fiz8q4WbYgr7k6wFegsl_HP0cPeegHhx4"}
  ```
  (la valeur est le `Sheet_ID` du classeur de l'établissement — celui que tu
  donnes déjà aux autres postes via le bouton "J'ai déjà un espace existant").

Pour générer une clé API pour un établissement, n'importe quelle chaîne
aléatoire suffit — par exemple depuis un terminal :
```
python -c "import secrets; print(secrets.token_hex(24))"
```

## 3. Configurer le poste local

Sur chaque machine, crée le fichier (pas versionné, pas dans un zip
partagé) :
```
%LOCALAPPDATA%\AntGraphicsSchool\relay_api_key.txt
```
Contenu : la clé API de cet établissement (la même que dans
`ETABLISSEMENTS_JSON` côté serveur).

## 4. Tester en local avant de déployer

```
pip install -r requirements.txt
set GOOGLE_CREDENTIALS_JSON=<contenu de credentials.json>
set ETABLISSEMENTS_JSON={"test123": "TON_SHEET_ID"}
python app.py
```

Puis dans un autre terminal :
```
curl -X POST http://localhost:5000/verifier_connexion ^
  -H "X-API-Key: test123" ^
  -H "Content-Type: application/json" ^
  -d "{\"email\": \"...\", \"mot_de_passe\": \"...\"}"
```

## 5. Une fois déployé

Mets à jour `RELAY_URL` dans `core/relay_client.py` (côté `gestion_scolaire`)
avec l'URL Render réelle (`https://<ton-service>.onrender.com`).
