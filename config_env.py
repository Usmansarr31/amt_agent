"""Variables de configuration sur Streamlit Community Cloud.

Sur Streamlit Community Cloud, la configuration se saisit dans les Secrets de l'application (Advanced settings ->
Secrets, ou Settings -> Secrets après le déploiement), jamais dans un fichier du dépôt. Ce module recopie les clés
connues de st.secrets dans l'environnement, seulement si elles n'y sont pas déjà : l'application continue de lire
uniquement os.environ. Les valeurs ne sont jamais affichées, journalisées ni écrites.

À importer avant copilot (qui lit AMT_AGENT_DELAI au chargement).
"""
import os

CLES = ("COPILOT_DIRECTLINE_SECRET", "COPILOT_DIRECTLINE_URL", "AMT_DEFAULT_FILE", "AMT_AGENT_DELAI", "AMT_TRACES_DIR")


def charger_secrets_streamlit():
    try:
        import streamlit as st
        secrets = st.secrets
        disponibles = {c: secrets[c] for c in CLES if c in secrets}
    except Exception:  # aucun secret configuré (lancement local avec lancer.sh) : rien à faire
        return
    for cle, valeur in disponibles.items():
        if cle not in os.environ and isinstance(valeur, (str, int, float)) and str(valeur).strip():
            os.environ[cle] = str(valeur).strip()


charger_secrets_streamlit()
