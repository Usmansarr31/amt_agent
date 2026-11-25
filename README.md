# AMT : analyse intelligente du trafic aérien

Prototype réalisé pour les **ATSA-ASECNA Innovation Days 2026** (AI Agents Challenge, fiche thématique n°02 « ATM : analyse des données »).

L'application réunit dans une seule interface :

- un **tableau de bord Streamlit** qui nettoie et analyse un historique de trafic aérien ;
- une **discussion avec l'agent Copilot Studio** du projet, par l'API Direct Line.

> **Principe : l'agent IA assiste, l'expert métier valide.**
> Les données sont fictives (bac à sable). Aucune décision opérationnelle n'est prise par l'outil.

## Fonctionnalités

| Page | Contenu |
|---|---|
| Vue d'ensemble | Indicateurs, graphiques mensuels, commentaire de tendance automatique, répartition par catégorie de vol et origine / destination |
| Comparaison | Deux périodes de même durée en mois complets (par défaut), total, moyenne par jour et écarts en % |
| Retards | Retard moyen pondéré par le nombre de mouvements |
| Anomalies | Détection statistique (score MAD), validation ou rejet par l'expert, registre conservé |
| Qualité des données | Journal de nettoyage, réconciliation brut / nettoyé, mois incomplets |
| Traçabilité | Registre des décisions de l'expert et journal des échanges avec l'agent |
| Exports | Excel, PDF et CSV, avec le statut de validation |
| Discuter | Questions en texte libre à l'agent AMT (Copilot Studio) |
| Voir l'agent | L'agent intégré par son adresse ou son code `<iframe>` |

### Formats et règles de nettoyage

- **Formats lus :** CSV, Excel, XML et PDF (tableaux). Les quatre formats passent par les mêmes règles de nettoyage.
- **Doublons exacts :** retirés.
- **Valeurs vides ou négatives :** exclues, jamais remplacées par une valeur inventée. Un zéro enregistré est conservé, et un jour absent n'est jamais compté comme zéro.
- **Anomalie :** signalée si |score| > 6, avec score = (valeur − médiane) / (1,4826 × MAD), calculé par aérodrome et par type de mouvement.
- **Mois incomplet :** un mois de moins de 28 jours renseignés est signalé.
- **Colonne `source` :** traitée comme une donnée. Un texte qui ressemble à une consigne est ignoré et signalé dans le journal de nettoyage.

## Installation

Il faut Python 3.11 ou plus récent.

### Linux / macOS

```bash
git clone https://github.com/Usmansarr31/amt_agent.git
cd amt_agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Pour activer l'environnement virtuel dans un terminal : `source .venv/bin/activate`.

### Windows

Installez Python depuis [python.org](https://www.python.org/downloads/) en cochant **« Add python.exe to PATH »**. Ensuite, dans l'invite de commandes (`cmd`) ou PowerShell :

```bat
git clone https://github.com/Usmansarr31/amt_agent.git
cd amt_agent
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Pour activer l'environnement virtuel :

| Terminal | Commande |
|---|---|
| Invite de commandes (`cmd`) | `.venv\Scripts\activate.bat` |
| PowerShell | `.venv\Scripts\Activate.ps1` |

Le nom `(.venv)` apparaît alors au début de la ligne. Pour quitter l'environnement, tapez `deactivate`.

Si PowerShell refuse d'exécuter `Activate.ps1` (« l'exécution de scripts est désactivée sur ce système »), autorisez les scripts pour votre utilisateur, une seule fois :

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Vous n'avez pas besoin d'activer l'environnement pour utiliser `lancer.bat` : le script le trouve tout seul.

## Lancement

| Système | Commande |
|---|---|
| Linux / macOS | `./lancer.sh` |
| Windows | double-cliquer sur `lancer.bat`, ou taper `lancer.bat` dans un terminal |

Le script demande le secret Direct Line sans l'afficher. Collez-le puis appuyez sur Entrée, ou appuyez directement sur Entrée pour lancer sans l'agent. Le secret n'est écrit dans aucun fichier.

Le script charge le fichier des organisateurs et trouve tout seul l'environnement `.venv`. Les options sont transmises à Streamlit, par exemple `./lancer.sh --server.port 8502`.

L'application s'ouvre sur http://localhost:8501. Le jeu de données est chargé automatiquement, sans upload.

Pour lancer sans script :

```bash
read -rs COPILOT_DIRECTLINE_SECRET && export COPILOT_DIRECTLINE_SECRET   # coller le secret, puis Entrée
AMT_DEFAULT_FILE=donnees/DONNEES_03_ATM_ANALYSE_TRAFIC.xlsx .venv/bin/streamlit run app.py
```

Sous Windows, dans PowerShell, avec l'environnement activé :

```powershell
$env:COPILOT_DIRECTLINE_SECRET = Read-Host "Secret" -MaskInput   # PowerShell 7 ; sinon utiliser lancer.bat
$env:AMT_DEFAULT_FILE = "donnees\DONNEES_03_ATM_ANALYSE_TRAFIC.xlsx"
streamlit run app.py
```

| Variable | Rôle |
|---|---|
| `AMT_DEFAULT_FILE` | Jeu de données chargé au démarrage. C'est le même fichier que celui de l'agent. |
| `COPILOT_DIRECTLINE_SECRET` | Secret Direct Line de l'agent, à prendre dans Copilot Studio : **Paramètres > Sécurité > Sécurité du canal web**. |
| `COPILOT_DIRECTLINE_URL` | Facultatif. Autre domaine Direct Line (par défaut `https://directline.botframework.com`). |
| `AMT_AGENT_URL` | Facultatif. Adresse de l'agent pour la page « Voir l'agent ». |
| `AMT_TRACES_DIR` | Facultatif. Autre emplacement pour les journaux de traçabilité. |

> **Le secret ne doit jamais être écrit dans un fichier ni versionné.**
> L'application le lit uniquement dans l'environnement. Elle ne l'affiche pas et ne l'écrit dans aucun journal, aucune trace et aucun message d'erreur.

Pour remplacer le jeu de données pendant une session, ouvrez la section **Avancé**, repliée en bas de la barre latérale.

## Discussion avec l'agent

Voici ce qui se passe quand l'utilisateur pose une question :

1. L'application obtient un jeton Direct Line à partir du secret.
2. Elle ouvre une conversation avec l'agent.
3. Elle envoie la question.
4. Elle lit les réponses toutes les 2 secondes, pendant 90 secondes au plus.

Seule la question est envoyée, jamais les données du fichier. La réponse de l'agent s'affiche telle quelle.

La réponse est considérée comme complète dans l'un de ces deux cas :

- un message de l'agent suivi d'une activité `event` ;
- 5 secondes sans nouvelle activité après la réponse.

L'agent garde le contexte d'une question à l'autre. Le bouton « Nouvelle conversation » ouvre une nouvelle conversation.

Le bouton **Exporter**, en haut de la discussion, télécharge :

- la discussion (Markdown) ;
- le rapport Excel ;
- le rapport PDF ;
- les données nettoyées (CSV).

Quand l'agent joint un fichier à sa réponse, un bouton à son nom apparaît sous la réponse. Un fichier intégré à la réponse se télécharge directement. Un lien https s'ouvre dans un nouvel onglet. Les cartes adaptatives ne sont pas affichées.

Si l'agent est injoignable, un bouton discret **« Voir la réponse calculée localement »** affiche une réponse calculée sur le jeu de données, sans Internet. Chaque échange est enregistré dans la page Traçabilité.

### Tester sans l'agent réel

`faux_copilot.py` simule un agent Direct Line en local :

```bash
.venv/bin/python faux_copilot.py &
COPILOT_DIRECTLINE_SECRET=faux-secret-de-test COPILOT_DIRECTLINE_URL=http://localhost:8765 \
AMT_DEFAULT_FILE=donnees/DONNEES_03_ATM_ANALYSE_TRAFIC.xlsx .venv/bin/streamlit run app.py
```

## Tests

```bash
.venv/bin/python test_questions.py
```

La suite comprend 67 tests d'acceptation. Les 35 tests de la variante piégée ne tournent que si on fournit ce fichier, qui n'est pas dans le dépôt : `AMT_FICHIER_PIEGE=chemin/variante.csv .venv/bin/python test_questions.py`. Sans lui, ils sont ignorés et les 32 autres doivent passer.

Les tests couvrent :

- les chiffres de contrôle ;
- le nettoyage ;
- les formats XML et PDF ;
- la comparaison de périodes ;
- la traçabilité ;
- le client Direct Line (mode secret, réponses lentes, activités `typing` et `event`, erreurs) ;
- l'interface complète.

### Chiffres de contrôle

| Jeu de données | Contrôles |
|---|---|
| Fichier des organisateurs | Total nettoyé : 110 229 (brut : 110 266)<br>1 anomalie (DAK-SIM, arrivées, 14/08/2025)<br>Dakar, janvier 2025 : 2 139 |
| Comparaison (fichier des organisateurs) | Janvier à juin 2025 contre janvier à juin 2026, Dakar : 14 077 contre 14 095 (+0,13 %) |
| Variante piégée (fichier non fourni) | Total nettoyé : 55 643 (brut : 55 711)<br>2 anomalies<br>Douala, novembre 2025 : 1 512<br>Février 2026 incomplet (17 jours) |

## Structure

```
lancer.sh          lancement sous Linux / macOS (demande le secret sans l'afficher)
lancer.bat         lancement sous Windows
app.py             interface Streamlit (tableau de bord, discussion, navigation)
core.py            lecture (CSV, Excel, XML, PDF), nettoyage, calculs, commentaire de tendance
copilot.py         client Direct Line de l'agent Copilot Studio (mode secret)
questions.py       réponse calculée localement (secours), sans modèle externe
traces.py          registre de validation et journal des échanges (JSON Lines)
faux_copilot.py    faux serveur Direct Line pour les tests
test_questions.py  tests d'acceptation
donnees/           fichier fictif des organisateurs
LISEZ_MOI.txt      documentation détaillée
```

## Garde-fous

- **Validation humaine :** obligatoire avant toute utilisation. Chaque réponse le rappelle.
- **Opérations :** aucune décision opérationnelle (créneaux, vols, piste, sécurité). L'outil refuse ces demandes.
- **Causes :** aucune cause n'est inventée. Une cause inconnue est présentée comme inconnue, à vérifier par l'expert.
- **Prévisions :** seulement sur demande, présentées comme indicatives et réservées à la planification stratégique.
- **Contenu du fichier :** toujours traité comme une donnée, jamais comme une instruction.
