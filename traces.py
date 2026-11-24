"""Traçabilité persistante : registre de validation de l'expert et journal des questions à l'assistant.

Chaque enregistrement est ajouté (jamais modifié) à un fichier JSON Lines du dossier traces/,
avec l'horodatage, le nom du fichier analysé et son empreinte SHA-256 (12 caractères) pour relier
chaque décision ou réponse à la version exacte des données.
"""
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

DOSSIER = Path(os.environ.get("AMT_TRACES_DIR", Path(__file__).resolve().parent / "traces"))
REGISTRE = "registre_validation.jsonl"
JOURNAL = "journal_assistant.jsonl"
MOTEUR_CALCUL = "Calcul local (secours)"
MOTEUR_AGENT = "Agent Copilot Studio"
COLONNES = {
    REGISTRE: ["horodatage", "fichier", "empreinte", "element", "decision", "commentaire"],
    JOURNAL: ["horodatage", "fichier", "empreinte", "moteur", "langue", "type_reponse", "question", "reponse",
              "chiffres", "conversation_agent", "duree_s"],
}


def empreinte(contenu: bytes) -> str:
    return hashlib.sha256(contenu).hexdigest()[:12]


def ajouter(nom_journal, enregistrement, dossier=None):
    dossier = Path(dossier or DOSSIER)
    dossier.mkdir(parents=True, exist_ok=True)
    ligne = {"horodatage": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **enregistrement}
    with open(dossier / nom_journal, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ligne, ensure_ascii=False, default=str) + "\n")
    return ligne


def lire(nom_journal, empreinte_fichier=None, dossier=None):
    """Enregistrements du journal, limités à une empreinte de fichier si elle est donnée."""
    chemin = Path(dossier or DOSSIER) / nom_journal
    lignes = []
    if chemin.exists():
        with open(chemin, encoding="utf-8") as fh:
            for brut in fh:
                try:
                    lignes.append(json.loads(brut))
                except json.JSONDecodeError:
                    continue  # ligne abîmée : ignorée, le reste du journal reste lisible
    d = pd.DataFrame(lignes, columns=COLONNES[nom_journal])
    if nom_journal == JOURNAL:  # lignes écrites avant l'ajout de la colonne : réponses calculées localement
        d["moteur"] = d["moteur"].fillna(MOTEUR_CALCUL)
    if empreinte_fichier is not None:
        d = d[d["empreinte"] == empreinte_fichier]
    return d.reset_index(drop=True)


def statut_anomalies(registre):
    """Dernière décision enregistrée pour chaque élément du registre."""
    if registre.empty:
        return {}
    return registre.sort_values("horodatage").groupby("element")["decision"].last().to_dict()
