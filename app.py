import io
import os
import re
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import copilot
import core
import questions
import rapport
import traces

MENTION = "Données fictives - validation humaine requise"
st.set_page_config(page_title="AMT - Analyse du trafic aérien", page_icon=":material/flight:", layout="wide")

MOTEUR_CALCUL, MOTEUR_AGENT = traces.MOTEUR_CALCUL, traces.MOTEUR_AGENT
for cle, defaut in {"chat": [], "chat_nouveau": False, "agent_conv": None, "agent_attente": None}.items():
    st.session_state.setdefault(cle, defaut)


def fmt(x):
    return f"{x:,.0f}".replace(",", " ")


def registre():
    """Décisions de l'expert pour ce fichier (journal persistant, relu à chaque affichage)."""
    return traces.lire(traces.REGISTRE, emp)


def journal_assistant():
    return traces.lire(traces.JOURNAL, emp)


def journal_lisible(ja):
    """Journal pour l'affichage et les exports : fichiers reçus réduits à leurs noms."""
    ja = ja.drop(columns=["empreinte"]).copy()
    ja["fichiers"] = [", ".join(f.get("nom", "") for f in v if isinstance(f, dict)) if isinstance(v, list) else ""
                      for v in ja["fichiers"]]
    return ja


def etiquette(r):
    """Identifiant lisible d'une anomalie, utilisé dans le registre."""
    return f"{pd.Timestamp(r.date).date()} | {r.aerodrome} | {r.type_mouvement} | {int(r.nombre_mouvements)}"


def anomalies_avec_statut(d):
    ano = d[d["anomalie"] == 1][["date", "aerodrome", "type_mouvement", "nombre_mouvements", "score_anomalie", "ligne_fichier"]].copy()
    statuts = traces.statut_anomalies(registre())
    ano["statut_validation"] = [statuts.get(etiquette(r), "En attente") for r in ano.itertuples()]
    ano["date"] = ano["date"].dt.date
    return ano


# =====================================================================
# Pages du tableau de bord
# =====================================================================
def entete():
    st.title("Analyse intelligente du trafic aérien")
    st.caption(MENTION + " | L'outil assiste, l'expert métier valide.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total nettoyé (filtres)", fmt(f["nombre_mouvements"].sum()))
    c2.metric("Total brut du fichier", fmt(meta["total_brut"]))
    c3.metric("Écart brut - nettoyé", f"{meta['total_nettoye'] - meta['total_brut']:+,.0f}".replace(",", " "))
    c4.metric("Anomalies à valider", int(f["anomalie"].sum()))


def section_vue():
    m = f_graph.groupby(["mois", "aerodrome"])["nombre_mouvements"].sum().reset_index()
    fig = px.line(m, x="mois", y="nombre_mouvements", color="aerodrome", markers=True,
                  labels={"mois": "Mois", "nombre_mouvements": "Mouvements", "aerodrome": "Aérodrome"},
                  title="Mouvements par mois et par aérodrome")
    st.plotly_chart(fig, width="stretch")
    with st.container(border=True):
        st.markdown("**Commentaire de tendance** (calculé sur la sélection, sans modèle externe)")
        st.write(core.commentaire_tendance(f, mois_info))
        st.caption(MENTION)
    a = f.groupby("aerodrome")["nombre_mouvements"].sum().reset_index()
    st.plotly_chart(px.bar(a, x="aerodrome", y="nombre_mouvements", text_auto=True,
                           labels={"aerodrome": "Aérodrome", "nombre_mouvements": "Mouvements"},
                           title="Total sur la période choisie"), width="stretch")
    st.subheader("Totaux mensuels")
    st.dataframe(core.totaux_mensuels(f, mois_info.loc[mois_info.index.isin(f["mois"].unique())]), width="stretch", hide_index=True)
    cols_rep = [c for c in core.COLONNES_REPARTITION if c in f.columns]
    if cols_rep:
        st.subheader("Répartition par catégorie de vol et par origine / destination")
        for col, zone in zip(cols_rep, st.columns(len(cols_rep))):
            libelle = core.COLONNES_REPARTITION[col]
            with zone:
                g = f.groupby(["aerodrome", col])["nombre_mouvements"].sum().reset_index()
                st.plotly_chart(px.bar(g, x="aerodrome", y="nombre_mouvements", color=col,
                                       labels={"aerodrome": "Aérodrome", "nombre_mouvements": "Mouvements", col: libelle},
                                       title=libelle), width="stretch")
                st.dataframe(core.repartition(f, col), width="stretch", hide_index=True)
        st.caption("Calculé sur la sélection nettoyée (filtres du menu de gauche). La dernière ligne donne la part de chaque valeur.")


def section_comparaison():
    st.write("Compare deux périodes. Par défaut : deux périodes de même durée en mois complets "
             "(les derniers mois complets et les mêmes mois un an plus tôt quand le fichier le permet).")
    defaut = core.periodes_comparables(mois_info) or ((tous_mois[0], tous_mois[0]), (tous_mois[-1], tous_mois[-1]))
    ca, cb = st.columns(2)
    a1, a2 = ca.select_slider("Période A", options=tous_mois, value=defaut[0], key=f"pa_{emp}")
    b1, b2 = cb.select_slider("Période B", options=tous_mois, value=defaut[1], key=f"pb_{emp}")
    base = df[df["aerodrome"].isin(choix)]
    for col, valeurs in filtres_cat.items():
        base = base[base[col].astype(str).isin(valeurs)]
    cmp_ = core.comparer_periodes(base, (a1, a2), (b1, b2))
    st.dataframe(cmp_, width="stretch", hide_index=True,
                 column_config={c: st.column_config.NumberColumn(c, format="%.2f %%") for c in cmp_.columns if "%" in c})
    na, nb = core.nb_mois(a1, a2), core.nb_mois(b1, b2)
    if na != nb:
        st.warning(f"Les périodes n'ont pas la même durée : compare les moyennes par jour. (A : {na} mois, B : {nb} mois)")
    inc = [x for x in incomplets if a1 <= x <= a2 or b1 <= x <= b2]
    if inc:
        st.warning("Une des périodes contient un mois incomplet : " + ", ".join(inc) + ". Compare avec prudence.")
    st.caption("Moyenne par jour = total de la période / nombre de jours renseignés pour l'aérodrome (un jour absent "
               "n'est jamais compté comme zéro). Un écart minime est peu significatif. Le fichier ne permet pas "
               "d'établir les causes d'un écart.")


def section_retards():
    r = core.retard_pondere(f)
    if r.empty:
        st.info("Pas de colonne retard_moyen_min dans ce fichier.")
        return
    st.plotly_chart(px.bar(r, x="aerodrome", y="retard_moyen_pondere_min", text_auto=True,
                           labels={"aerodrome": "Aérodrome", "retard_moyen_pondere_min": "Retard moyen pondéré (min)"},
                           title="Retard moyen pondéré par le nombre de mouvements"), width="stretch")
    st.dataframe(r, width="stretch", hide_index=True)
    st.caption("Moyenne pondérée par nombre_mouvements. Les retards vides sont exclus, pas remplacés par zéro.")


def section_anomalies():
    ano = anomalies_avec_statut(f)
    st.write(f"Méthode : score = (valeur - médiane) / (1,4826 x MAD) par aérodrome et type de mouvement. Seuil : |score| > {core.SEUIL_ANOMALIE:g}. "
             "Les anomalies restent dans les totaux et sont à valider, la cause est inconnue.")
    if ano.empty:
        st.success("Aucune anomalie statistique sur la sélection.")
    else:
        st.dataframe(ano, width="stretch", hide_index=True)
        hors = f["nombre_mouvements"].sum() - f[f["anomalie"] == 1]["nombre_mouvements"].sum()
        st.metric("Total hors anomalies (scénario, la référence reste le total nettoyé)", fmt(hors))
        sel = f[f["anomalie"] == 1].iloc[0]
        serie = f[(f["aerodrome"] == sel["aerodrome"]) & (f["type_mouvement"] == sel["type_mouvement"])]
        fig = px.line(serie, x="date", y="nombre_mouvements",
                      title=f"{sel['aerodrome']} - {sel['type_mouvement']} : valeurs journalières")
        pts = serie[serie["anomalie"] == 1]
        fig.add_scatter(x=pts["date"], y=pts["nombre_mouvements"], mode="markers", marker=dict(color="red", size=11), name="Anomalie à valider")
        st.plotly_chart(fig, width="stretch")
        st.subheader("Validation par l'expert métier")
        etiquettes = [etiquette(r) for r in ano.itertuples()]
        element = st.selectbox("Élément", etiquettes)
        decision = st.radio("Décision", ["Valider", "Rejeter", "Commenter"], horizontal=True)
        commentaire = st.text_input("Commentaire")
        nom_expert = st.text_input("Nom de l'expert", key="anomalie_nom")
        if st.button("Enregistrer la décision"):
            traces.ajouter(traces.REGISTRE, {"fichier": os.path.basename(nom), "empreinte": emp, "element": element,
                                             "decision": decision, "commentaire": commentaire,
                                             "nom": nom_expert.strip()})
            st.session_state.message_registre = True
            st.rerun()  # pour mettre à jour le statut dans le tableau ci-dessus
        if st.session_state.pop("message_registre", False):
            st.success("Décision enregistrée dans le registre (traces/registre_validation.jsonl). "
                       "Les données d'origine ne sont pas modifiées.")
    reg = registre()
    if not reg.empty:
        st.subheader("Registre de validation")
        st.dataframe(reg.drop(columns=["empreinte"]), width="stretch", hide_index=True)


def section_qualite():
    q1, q2, q3 = st.columns(3)
    q1.metric("Lignes du fichier", meta["lignes_brutes"])
    q2.metric("Lignes retenues", meta["lignes_propres"])
    q3.metric("Problèmes traités", len(journal))
    st.subheader("Journal de nettoyage")
    st.dataframe(journal, width="stretch", hide_index=True)
    st.subheader("Réconciliation brut / nettoyé par mois")
    rec = meta["brut_par_mois"].to_frame().join(df.groupby("mois")["nombre_mouvements"].sum().rename("total_nettoye"), how="outer").fillna(0)
    rec["ecart"] = rec["total_nettoye"] - rec["total_brut"]
    st.dataframe(rec.reset_index().rename(columns={"index": "mois", "date": "mois"}), width="stretch", hide_index=True)
    st.caption("Le total brut additionne toutes les lignes du fichier, doublons et valeurs négatives compris. Il n'est jamais présenté comme correct.")


def analyse_courante():
    return rapport.analyser(df, journal, mois_info, meta, os.path.basename(nom), emp, rapport.sources_excel(contenu, nom))


def rapport_excel():
    return rapport.construire_excel(analyse_courante(), registre())


def rapport_pdf():
    return rapport.construire_pdf(analyse_courante(), registre())


ICONES_CONTROLE = {True: ":green[:material/check_circle:]", False: ":red[:material/cancel:]",
                   None: ":gray[:material/help:]"}


def section_exports():
    st.write("Rapport complet au format des livrables de l'agent AMT : Excel (tableau de bord à filtres, formules, "
             "données nettoyées) et PDF (tableau de bord, analyse, registre). Il porte sur tout le fichier, sans les "
             "filtres de la barre latérale.")
    nom_xlsx, nom_pdf = rapport.nom_fichiers()
    cle = (emp, len(registre()))
    pret = st.session_state.get("rapport_pret")
    if not pret or pret["cle"] != cle:
        if st.button("Préparer le rapport (Excel et PDF)", type="primary", icon=":material/description:"):
            with st.spinner("Construction et relecture du rapport..."):
                A = analyse_courante()
                reg = registre()
                x = rapport.construire_excel(A, reg)
                p = rapport.construire_pdf(A, reg)
                st.session_state.rapport_pret = {"cle": cle, "xlsx": x, "pdf": p,
                                                 "controles": rapport.relire(A, x, p)}
            st.rerun()
    else:
        with st.container(horizontal=True):
            st.download_button(nom_xlsx, pret["xlsx"], nom_xlsx,
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:", on_click="ignore", type="primary")
            st.download_button(nom_pdf, pret["pdf"], nom_pdf, "application/pdf",
                               icon=":material/picture_as_pdf:", on_click="ignore", type="primary")
        echecs = [c for c in pret["controles"] if c[1] is False]
        with st.expander("Relecture avant livraison" + (" : contrôle en échec" if echecs else " : contrôles réussis"),
                         icon=":material/fact_check:", expanded=bool(echecs)):
            for nom_c, ok, detail in pret["controles"]:
                st.markdown(f"{ICONES_CONTROLE[ok]} {nom_c} : {detail}")
    st.download_button("Données nettoyées (CSV)", df.drop(columns=["statut"]).to_csv(index=False).encode("utf-8-sig"),
                       "trafic_nettoye.csv", "text/csv", icon=":material/download:", on_click="ignore")


# =====================================================================
# Assistant : discussion en texte libre (calcul local, sans modèle externe)
# =====================================================================
STYLE_CHAT = """
<style>
.st-key-chat_accueil, .st-key-chat_fil { max-width: 48rem; margin-left: auto; margin-right: auto; }
.st-key-chat_accueil { margin-top: 22vh; text-align: center; }
.st-key-chat_accueil h1 { font-weight: 400; }
[class*="st-key-bulle_user_"] {
    background: var(--st-secondary-background-color, rgba(128,128,128,.14));
    border-radius: 1.2rem; padding: .7rem 1.1rem; max-width: 80%;
}
[class*="st-key-bulle_user_"] p { margin-bottom: 0; }
[class*="st-key-reponse_"] { font-family: Georgia, "Times New Roman", serif; font-size: 1.04rem; line-height: 1.65; padding: .3rem .2rem 1rem; }
[class*="st-key-reponse_"] code { font-size: .85em; }
[class*="st-key-reponse_"] table { font-family: system-ui, sans-serif; font-size: .92rem; }
.st-key-chat_saisie { max-width: 48rem; margin-left: auto; margin-right: auto; }
.st-key-chat_saisie > div { border-radius: 1.4rem; }
</style>
"""


def repondre_local(question, lg):
    """Secours si l'agent est injoignable : calcul sur le jeu de données nettoyé, sans réseau."""
    chat = st.session_state.chat
    if df is None:
        chat.append({"role": "assistant", "content": "Aucun jeu de données chargé : réponse locale impossible.",
                     "secours": True})
        return
    rep = questions.repondre(question, df, mois_info)
    chat.append({"role": "assistant", "content": rep.texte, "secours": True})
    traces.ajouter(traces.JOURNAL, {"fichier": os.path.basename(nom), "empreinte": emp, "moteur": MOTEUR_CALCUL,
                                    "langue": rep.langue, "type_reponse": rep.type, "question": question,
                                    "reponse": rep.texte, "chiffres": rep.chiffres})


def poser():
    """La question part à l'agent ; l'appel réseau se fait dans la page, sous la question, avec l'indicateur."""
    question = (st.session_state.get("chat_saisie") or "").strip()
    if not question:
        return
    st.session_state.chat.append({"role": "user", "content": question})
    st.session_state.agent_attente = {"question": question, "langue": questions.langue_de(question),
                                      "fichier": nom, "empreinte": emp}


def interroger_agent(attente, sur_etat=None, sur_progression=None):
    """Envoie la seule question à l'agent Copilot Studio (mode secret Direct Line) et ajoute sa réponse au fil.
    Seuls les liens sandbox: (inaccessibles au navigateur) sont remplacés à l'affichage ; le journal garde la
    réponse originale. Les fichiers joints sont récupérés tout de suite et conservés dans traces/fichiers/.
    Le secret est lu par copilot.py dans l'environnement, jamais ici."""
    question = attente["question"]
    url, dl = copilot.configuration_env()
    conv = st.session_state.agent_conv
    debut = time.time()
    entree = {"fichier": os.path.basename(attente["fichier"] or ""), "empreinte": attente["empreinte"],
              "moteur": MOTEUR_AGENT, "langue": attente["langue"], "question": question, "chiffres": {}}
    try:
        if conv is None:
            conv = copilot.ouvrir_conversation(url, dl or None)
            st.session_state.agent_conv = conv
        if attente.get("reprise"):  # réponse arrivée après le délai : relecture, rien n'est renvoyé à l'agent
            r = copilot.attendre_reponse(conv, attente["reprise"]["id_envoi"], sur_etat=sur_etat,
                                         sur_progression=sur_progression)
        else:
            r = copilot.demander(conv, question, sur_etat=sur_etat, sur_progression=sur_progression)
        texte = "\n\n".join(r.textes)
        fichiers = copilot.recuperer_fichiers(conv, r.fichiers)
        references = []
        for f in fichiers:
            if f.get("contenu") is not None:
                f["reference"] = traces.enregistrer_fichier(f["contenu"], f["nom"], f["type"])
                references.append(f["reference"])
        affiche = copilot.remplacer_liens_sandbox(texte, [f["nom"] for f in fichiers])
        st.session_state.chat.append({"role": "assistant", "content": affiche, "pieces": r.pieces_jointes,
                                      "fichiers": fichiers, "incomplete": not r.complete,
                                      "suite": None if r.complete else {"id_envoi": r.id_envoi, "question": question,
                                                                        "langue": attente["langue"]}})
        traces.ajouter(traces.JOURNAL, {**entree, "type_reponse": "reponse_agent" if r.complete
                                        else "reponse_agent_incomplete", "reponse": texte,
                                        "conversation_agent": r.conversation_id, "duree_s": r.duree,
                                        "fichiers": references})
    except copilot.ReponseEnRetard as ex:  # l'agent travaille peut-être encore : réponse récupérable
        texte = f"L'agent n'a pas encore répondu. {ex}"
        st.session_state.chat.append({"role": "assistant", "content": texte, "echec": question,
                                      "langue": attente["langue"],
                                      "suite": {"id_envoi": ex.id_envoi, "question": question,
                                                "langue": attente["langue"]}})
        traces.ajouter(traces.JOURNAL, {**entree, "type_reponse": "reponse_agent_en_retard", "reponse": texte,
                                        "conversation_agent": conv.id if conv else None,
                                        "duree_s": round(time.time() - debut, 1)})
    except Exception as ex:  # jamais de plantage : message clair et secours discret
        raison = str(ex) if isinstance(ex, copilot.ErreurAgent) else f"erreur inattendue ({type(ex).__name__})."
        texte = f"L'agent n'a pas pu répondre : {raison}"
        st.session_state.chat.append({"role": "assistant", "content": texte, "echec": question,
                                      "langue": attente["langue"]})
        traces.ajouter(traces.JOURNAL, {**entree, "type_reponse": "erreur_agent", "reponse": texte,
                                        "conversation_agent": conv.id if conv else None,
                                        "duree_s": round(time.time() - debut, 1)})


def recuperer_reponse(suite):
    """Relit la conversation pour récupérer la réponse (ou la suite) arrivée après le délai d'attente."""
    st.session_state.agent_attente = {"question": suite["question"], "langue": suite["langue"], "fichier": nom,
                                      "empreinte": emp, "reprise": suite}


def secours_local(question, lg):
    repondre_local(question, lg)
    st.session_state.chat_nouveau = True


def nouvelle_conversation():
    st.session_state.update(chat=[], chat_nouveau=False, agent_conv=None, agent_attente=None)


def section_tracabilite():
    st.caption(f"Fichier analysé : {os.path.basename(nom)} | empreinte SHA-256 : `{emp}`. Les journaux sont conservés "
               f"sur disque (dossier traces/), ajoutés ligne par ligne et jamais modifiés ; ils sont repris dans les exports.")
    reg, ja = registre(), journal_assistant()
    t1, t2, t3 = st.columns(3)
    t1.metric("Décisions de l'expert", len(reg))
    t2.metric("Anomalies en attente", int((anomalies_avec_statut(df)["statut_validation"] == "En attente").sum()))
    t3.metric("Questions à l'assistant", len(ja))
    if not ja.empty:
        st.caption("Par source de réponse : " + ", ".join(f"{k} : {v}" for k, v in ja["moteur"].value_counts().items()))
    st.subheader("Registre de validation")
    if reg.empty:
        st.info("Aucune décision enregistrée pour ce fichier (page Anomalies).")
    else:
        st.dataframe(reg.drop(columns=["empreinte"]), width="stretch", hide_index=True)
        st.download_button("Registre (CSV)", reg.to_csv(index=False).encode("utf-8-sig"), "registre_validation.csv",
                           "text/csv", icon=":material/download:")
    st.subheader("Journal de l'assistant")
    if ja.empty:
        st.info("Aucune question posée à l'assistant pour ce fichier (page Discuter).")
    else:
        st.dataframe(journal_lisible(ja), width="stretch", hide_index=True,
                     column_config={"reponse": st.column_config.TextColumn("réponse", width="large")})
        st.download_button("Journal de l'assistant (CSV)", journal_lisible(ja).to_csv(index=False).encode("utf-8-sig"),
                           "journal_assistant.csv", "text/csv", icon=":material/download:")
    fichiers_recus = [(r.horodatage, r.question, ref) for r in ja.itertuples()
                      if isinstance(r.fichiers, list) for ref in r.fichiers if isinstance(ref, dict)]
    st.subheader("Fichiers reçus de l'agent")
    if not fichiers_recus:
        st.info("Aucun fichier reçu de l'agent pour ce jeu de données.")
    for k, (quand, question, ref) in enumerate(reversed(fichiers_recus)):
        with st.container(horizontal=True, vertical_alignment="center"):
            contenu = traces.lire_fichier(ref)
            if contenu is None:
                st.caption(f":material/error: {ref.get('nom')} : fichier absent du dossier traces/fichiers/.")
            else:
                st.download_button(ref["nom"], contenu, ref["nom"], ref.get("type") or "application/octet-stream",
                                   key=f"trace_fichier_{k}", icon=":material/download:", on_click="ignore")
            st.caption(f"{quand} · {fmt(ref.get('taille', 0))} octets · empreinte `{ref.get('empreinte')}` · "
                       f"question : {question}")


ONGLETS = {"Vue d'ensemble": "section_vue", "Comparaison": "section_comparaison", "Retards": "section_retards",
           "Anomalies": "section_anomalies", "Qualité des données": "section_qualite",
           "Traçabilité": "section_tracabilite", "Exports": "section_exports"}


def page_tableau():
    entete()
    onglets = st.tabs(list(ONGLETS), key="onglet_tableau", on_change="rerun")
    for onglet, fonction in zip(onglets, ONGLETS.values()):
        if onglet.open:  # seul l'onglet affiché est calculé (les exports ne sont générés qu'au clic)
            with onglet:
                globals()[fonction]()


# =====================================================================
# Journal de l'agent : ce que l'agent a fait, session par session
# =====================================================================
SANS_SESSION = "Sans conversation (calcul local ou agent non joint)"


def sessions_agent(ja):
    """Une ligne par session (conversation Direct Line) : début, fin, questions, réponses, erreurs, durée."""
    ja = ja.copy()
    ja["session"] = [c if isinstance(c, str) and c else SANS_SESSION for c in ja["conversation_agent"]]
    ja["nb_fichiers"] = [len(v) if isinstance(v, list) else 0 for v in ja["fichiers"]]
    ja["duree_s"] = pd.to_numeric(ja["duree_s"], errors="coerce")
    t = ja.groupby("session", sort=False).agg(
        debut=("horodatage", "min"), fin=("horodatage", "max"), questions=("question", "size"),
        reponses=("type_reponse", lambda v: int((v == "reponse_agent").sum())),
        incompletes=("type_reponse", lambda v: int((v == "reponse_agent_incomplete").sum())),
        erreurs=("type_reponse", lambda v: int((v == "erreur_agent").sum())),
        secours=("moteur", lambda v: int((v == traces.MOTEUR_CALCUL).sum())),
        duree_moyenne_s=("duree_s", "mean"), fichiers=("nb_fichiers", "sum"))
    t["duree_moyenne_s"] = t["duree_moyenne_s"].round(1)
    return t.sort_values("debut", ascending=False).reset_index(), ja


def rapport_sessions_markdown(resume, ja):
    lignes = ["# Journal de l'agent AMT", "", f"Jeu de données : {os.path.basename(nom)} (empreinte {emp})",
              f"Rapport du {pd.Timestamp.now():%d/%m/%Y à %H:%M}", ""]
    for s_ in resume.itertuples():
        lignes += [f"## Session {s_.session}", "",
                   f"Du {s_.debut} au {s_.fin} : {s_.questions} question(s), {s_.reponses} réponse(s) complète(s), "
                   f"{s_.incompletes} incomplète(s), {s_.erreurs} erreur(s), {s_.secours} réponse(s) de secours, "
                   f"{s_.fichiers} fichier(s) reçu(s).", ""]
        for e in ja[ja["session"] == s_.session].sort_values("horodatage").itertuples():
            lignes += [f"### {e.horodatage} · {LIBELLES_TYPE.get(e.type_reponse, e.type_reponse)}", "",
                       f"**Question :** {e.question}", "", str(e.reponse), ""]
            if isinstance(e.fichiers, list) and e.fichiers:
                lignes += ["Fichiers reçus : " + ", ".join(f.get("nom", "") for f in e.fichiers), ""]
    lignes += ["---", MENTION]
    return "\n".join(lignes)


LIBELLES_TYPE = {"reponse_agent": "Réponse de l'agent", "reponse_agent_incomplete": "Réponse incomplète",
                 "reponse_agent_en_retard": "Délai dépassé (réponse récupérable)",
                 "erreur_agent": "Erreur", "reponse_locale": "Réponse calculée localement"}


def page_journal_agent():
    st.title("Journal de l'agent")
    st.caption(f"Ce que l'agent a fait, session par session, pour le jeu de données {os.path.basename(nom)} "
               f"(empreinte `{emp}`). Une session correspond à une conversation avec l'agent ; "
               "« Nouvelle conversation » (page Discuter) en ouvre une autre. Journal conservé sur disque, jamais modifié.")
    ja = journal_assistant()
    if ja.empty:
        st.info("Aucun échange avec l'agent pour ce jeu de données. Posez une question dans la page Discuter.")
        return
    resume, ja = sessions_agent(ja)
    with st.container(horizontal=True):
        st.metric("Sessions", len(resume), border=True)
        st.metric("Questions", len(ja), border=True)
        st.metric("Réponses complètes", int(resume["reponses"].sum()), border=True)
        st.metric("Erreurs", int(resume["erreurs"].sum()), border=True)
        st.metric("Fichiers reçus", int(resume["fichiers"].sum()), border=True)
    with st.container(horizontal=True, vertical_alignment="center"):
        st.subheader("Sessions", width="stretch")
        st.download_button("Rapport (Markdown)", lambda: rapport_sessions_markdown(resume, ja),
                           "journal_agent_amt.md", "text/markdown", icon=":material/description:", on_click="ignore")
        st.download_button("Sessions (CSV)", resume.to_csv(index=False).encode("utf-8-sig"), "sessions_agent.csv",
                           "text/csv", icon=":material/download:", on_click="ignore")
    st.dataframe(resume, hide_index=True, width="stretch", alt="Résumé des sessions avec l'agent", column_config={
        "session": st.column_config.TextColumn("Session"), "debut": "Début", "fin": "Fin",
        "questions": "Questions", "reponses": "Réponses", "incompletes": "Incomplètes", "erreurs": "Erreurs",
        "secours": "Secours local", "duree_moyenne_s": st.column_config.NumberColumn("Durée moyenne", format="%.1f s"),
        "fichiers": "Fichiers"})
    st.subheader("Détail par session")
    for n_s, s_ in enumerate(resume.itertuples()):
        titre = f"{s_.debut} · {s_.questions} question(s) · session {s_.session}"
        with st.expander(titre, icon=":material/forum:", expanded=n_s == 0):
            for k, e in enumerate(ja[ja["session"] == s_.session].sort_values("horodatage").itertuples()):
                with st.container(border=True):
                    duree = f" · {e.duree_s:g} s" if pd.notna(e.duree_s) else ""
                    st.caption(f"{e.horodatage} · {LIBELLES_TYPE.get(e.type_reponse, e.type_reponse)} · "
                               f"{e.moteur}{duree}")
                    st.markdown(f"**Question :** {e.question}")
                    st.markdown(copilot.remplacer_liens_sandbox(str(e.reponse), [
                        f.get("nom", "") for f in (e.fichiers if isinstance(e.fichiers, list) else [])]))
                    if isinstance(e.fichiers, list) and e.fichiers:
                        with st.container(horizontal=True):
                            for j, ref in enumerate(e.fichiers):
                                contenu = traces.lire_fichier(ref)
                                if contenu is not None:
                                    st.download_button(ref["nom"], contenu, ref["nom"],
                                                       ref.get("type") or "application/octet-stream",
                                                       key=f"journal_fichier_{n_s}_{k}_{j}",
                                                       icon=":material/download:", on_click="ignore")
                                else:
                                    st.caption(f":material/error: {ref.get('nom')} : fichier absent de traces/fichiers/.")


def effet_frappe(texte):
    for morceau in re.split(r"(\s+)", texte):
        yield morceau
        time.sleep(0.008)


def afficher_fichiers(i, fichiers):
    """Fichiers envoyés par l'agent : un bouton de téléchargement par fichier (contenu déjà récupéré)."""
    with st.container(horizontal=True):
        for k, f in enumerate(fichiers):
            if f.get("contenu") is not None:
                st.download_button(f["nom"], f["contenu"], f["nom"], f["type"], key=f"joint_{i}_{k}",
                                   icon=":material/download:", on_click="ignore")
    for f in fichiers:
        if f.get("contenu") is None:
            st.caption(f":material/error: {f['nom']} : {f.get('erreur', 'fichier joint indisponible.')}")


def discussion_markdown():
    lignes = ["# Discussion avec l'agent AMT", "",
              f"Jeu de données : {os.path.basename(nom) if nom else 'aucun'}",
              f"Exportée le {pd.Timestamp.now():%d/%m/%Y à %H:%M}", ""]
    for msg in st.session_state.chat:
        if msg["role"] == "user":
            lignes += ["## Question", "", msg["content"], ""]
        else:
            source = "Réponse calculée localement (secours)" if msg.get("secours") else "Réponse de l'agent"
            lignes += [f"### {source}", "", msg["content"], ""]
            if msg.get("fichiers"):
                lignes += ["Fichiers joints : " + ", ".join(Path(f["nom"]).name for f in msg["fichiers"]), ""]
    lignes += ["---", MENTION]
    return "\n".join(lignes)


def menu_export():
    with st.popover("Exporter", icon=":material/download:"):
        st.download_button("Discussion (Markdown)", discussion_markdown(), "discussion_agent_amt.md",
                           "text/markdown", icon=":material/chat:", width="stretch", on_click="ignore",
                           disabled=not st.session_state.chat)
        if df is not None:
            nom_xlsx, nom_pdf = rapport.nom_fichiers()
            st.download_button(f"Rapport Excel ({nom_xlsx})", rapport_excel, nom_xlsx,
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:", width="stretch", on_click="ignore")
            st.download_button(f"Rapport PDF ({nom_pdf})", rapport_pdf, nom_pdf, "application/pdf",
                               icon=":material/picture_as_pdf:", width="stretch", on_click="ignore")
            st.download_button("Données nettoyées (CSV)",
                               lambda: df.drop(columns=["statut"]).to_csv(index=False).encode("utf-8-sig"),
                               "trafic_nettoye.csv", "text/csv", icon=":material/dataset:", width="stretch",
                               on_click="ignore")
        st.caption("Les rapports portent sur tout le fichier, sans les filtres. Relecture complète : onglet Exports.")


def page_chat():
    st.html(STYLE_CHAT)
    if not st.session_state.chat:
        with st.container(key="chat_accueil"):
            st.title(":primary[:material/flight_takeoff:] Discuter avec l'agent AMT")
            st.caption("Vos questions sont envoyées à l'agent Copilot Studio du projet, qui travaille sur le même "
                       "jeu de données" + (f" ({os.path.basename(nom)})" if nom else "") + ". Seule la question est "
                       "envoyée ; ses réponses s'affichent telles quelles. Validation de l'expert métier requise.")
    else:
        with st.container(key="chat_fil"):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.subheader("Discuter avec l'agent AMT", width="stretch")
                menu_export()
            dernier = len(st.session_state.chat) - 1
            for i, msg in enumerate(st.session_state.chat):
                if msg["role"] == "user":
                    with st.container(horizontal=True, horizontal_alignment="right"):
                        with st.container(key=f"bulle_user_{i}", width="content"):
                            st.markdown(msg["content"])
                else:
                    with st.container(key=f"reponse_{i}"):
                        if i == dernier and st.session_state.chat_nouveau:
                            st.write_stream(effet_frappe(msg["content"]))
                            st.session_state.chat_nouveau = False
                        else:
                            st.markdown(msg["content"])
                        if msg.get("incomplete"):
                            st.caption(":material/hourglass_bottom: Réponse peut-être incomplète : délai de "
                                       f"{copilot.DELAI_MAX} s atteint alors que l'agent répondait encore.")
                        if msg.get("suite") and i == dernier and st.session_state.agent_conv is not None:
                            st.button("Récupérer la suite de la réponse" if msg.get("incomplete")
                                      else "Récupérer la réponse", icon=":material/refresh:", key=f"suite_{i}",
                                      type="secondary", on_click=recuperer_reponse, args=(msg["suite"],))
                        if msg.get("fichiers"):
                            afficher_fichiers(i, msg["fichiers"])
                        if msg.get("pieces"):
                            st.caption("L'agent a aussi envoyé : " + ", ".join(msg["pieces"]) + " (non affiché ici).")
                        if msg.get("secours"):
                            st.caption(":material/calculate: Réponse calculée localement (secours), l'agent "
                                       "n'ayant pas répondu.")
                        if msg.get("echec") and i == dernier and df is not None:
                            st.button("Voir la réponse calculée localement", icon=":material/calculate:",
                                      key=f"secours_{i}", type="tertiary", on_click=secours_local,
                                      args=(msg["echec"], msg.get("langue", "fr")))
            attente = st.session_state.agent_attente
            if attente:
                with st.container(key=f"reponse_{len(st.session_state.chat)}"):
                    zone = st.empty()
                    attente_ui = {"spinner": None, "texte": None, "etat": "L'agent analyse le fichier",
                                  "secondes": 0}

                    def afficher_attente(texte):
                        if attente_ui["texte"] == texte:
                            return
                        if attente_ui["spinner"] is not None:
                            attente_ui["spinner"].__exit__(None, None, None)
                        with zone.container():
                            sp = st.spinner(texte)
                            sp.__enter__()
                        attente_ui.update(spinner=sp, texte=texte)

                    def maj_attente(etat=None, secondes=None):
                        if etat:
                            attente_ui["etat"] = etat
                        if secondes is not None:
                            attente_ui["secondes"] = secondes
                        duree = f" ({attente_ui['secondes']} s)" if attente_ui["secondes"] >= 10 else ""
                        afficher_attente(f"{attente_ui['etat']}...{duree}")

                    if attente.get("reprise"):
                        attente_ui["etat"] = "Récupération de la réponse de l'agent"
                    maj_attente()
                    try:  # « typing » reçu : l'agent travaille encore ; temps d'attente affiché après 10 s
                        interroger_agent(attente, sur_etat=lambda _e: maj_attente(etat="L'agent réfléchit"),
                                         sur_progression=lambda sec: maj_attente(secondes=sec))
                    finally:
                        attente_ui["spinner"].__exit__(None, None, None)
                st.session_state.update(agent_attente=None, chat_nouveau=True)
                st.rerun()
    st.chat_input("Posez votre question à l'agent AMT...", key="chat_saisie", on_submit=poser, max_chars=500)


# =====================================================================
# Navigation (barre latérale), chargement et filtres
# =====================================================================
P_TABLEAU = st.Page(page_tableau, title="Tableau de bord", icon=":material/dashboard:", url_path="tableau",
                    default=True)
P_JOURNAL = st.Page(page_journal_agent, title="Journal de l'agent", icon=":material/receipt_long:",
                    url_path="journal")
P_CHAT = st.Page(page_chat, title="Discuter", icon=":material/chat:", url_path="discuter")
page = st.navigation([P_TABLEAU, P_JOURNAL, P_CHAT], position="sidebar")
sur_chat = page.url_path == P_CHAT.url_path
sur_tableau = page.url_path == P_TABLEAU.url_path

# Barre latérale : conversation, jeu de données, filtres, puis « Avancé » (replié).
with st.sidebar:
    if sur_chat and (st.session_state.chat or st.session_state.agent_conv):
        st.button("Nouvelle conversation", icon=":material/edit_square:", width="stretch",
                  on_click=nouvelle_conversation)
    zone_jeu = st.container()
    zone_filtres = st.container()
    with st.expander("Avancé", icon=":material/tune:"):
        fichier = st.file_uploader("Remplacer le jeu de données (CSV, Excel, XML ou PDF)", type=core.FORMATS,
                                   help="Par défaut, le jeu de données est celui de l'agent (variable "
                                        "AMT_DEFAULT_FILE). Retirer le fichier ici pour y revenir.")
        conv_agent = st.session_state.agent_conv
        st.caption("Connexion à l'agent : " + ("mode secret Direct Line" if copilot.mode_secret() else
                                               f"non configurée (variable {copilot.VAR_SECRET} absente)")
                   + (f" ; conversation `{conv_agent.id}`" if conv_agent else ""))

# Le dernier fichier chargé est gardé sur disque : il sert seulement si AMT_DEFAULT_FILE n'est pas défini.
DOSSIER_DERNIER = Path(os.environ.get("AMT_DERNIER_DIR", Path(__file__).resolve().parent / "dernier_fichier"))


def memoriser_octets(nom_f, contenu_f):
    DOSSIER_DERNIER.mkdir(parents=True, exist_ok=True)
    cible = DOSSIER_DERNIER / Path(nom_f).name
    if cible.exists() and cible.read_bytes() == contenu_f:
        return
    for ancien in DOSSIER_DERNIER.iterdir():
        if ancien.is_file():
            ancien.unlink()
    cible.write_bytes(contenu_f)


def dernier_memorise():
    if not DOSSIER_DERNIER.is_dir():
        return None
    fichiers = [p for p in DOSSIER_DERNIER.iterdir()
                if p.is_file() and p.suffix.lower() in tuple("." + x for x in core.FORMATS)]
    return max(fichiers, key=lambda p: p.stat().st_mtime) if fichiers else None


def fichier_par_defaut():
    """Fichier de AMT_DEFAULT_FILE (chemin absolu, relatif au dossier courant ou à celui de l'application)."""
    valeur = os.environ.get("AMT_DEFAULT_FILE", "").strip()
    if not valeur:
        return None
    for chemin in (Path(valeur), Path(__file__).resolve().parent / valeur):
        if chemin.is_file():
            return chemin
    st.sidebar.error(f"AMT_DEFAULT_FILE : fichier introuvable ({valeur}).")
    return None


@st.cache_data(show_spinner="Chargement du jeu de données...")
def charger(contenu, nom_fichier):
    brut = core.lire_fichier(io.BytesIO(contenu), nom_fichier)
    return core.nettoyer(brut)


# Jeu de données : celui de l'agent (AMT_DEFAULT_FILE), sauf remplacement explicite dans « Avancé ».
defaut = fichier_par_defaut()
nom, contenu, origine = None, None, None
if fichier is not None:
    nom, contenu, origine = fichier.name, fichier.getvalue(), "remplace"
    memoriser_octets(nom, contenu)
elif defaut is not None:
    nom, contenu, origine = defaut.name, defaut.read_bytes(), "defaut"
elif (chemin := dernier_memorise()) is not None:
    nom, contenu, origine = chemin.name, chemin.read_bytes(), "dernier"

df = mois_info = emp = None
if contenu is not None:
    emp = traces.empreinte(contenu)  # relie chaque décision et chaque réponse à la version exacte du fichier
    try:
        df, journal, mois_info, meta = charger(contenu, nom)
    except Exception as e:
        st.error(f"Impossible d'analyser ce fichier : {e}")
with zone_jeu:
    if origine == "defaut":
        st.caption(f":material/database: Jeu de données : **{nom}**, identique à celui de l'agent")
    elif origine == "remplace":
        st.caption(f":material/database: Jeu de données : **{nom}** (remplacé : différent de celui de l'agent)")
    elif origine == "dernier":
        st.caption(f":material/database: Jeu de données : **{nom}**")
if df is None:
    if sur_chat:  # l'agent n'a pas besoin du fichier local : la discussion reste possible
        page.run()
    elif contenu is None:
        st.info("Aucun jeu de données : lancez l'application avec AMT_DEFAULT_FILE (fichier de l'agent), "
                "ou chargez un fichier dans « Avancé » (barre latérale).")
    st.stop()

if st.session_state.get("chat_fichier") != emp:  # nouveau fichier : nouvelle conversation
    st.session_state.update(chat=[], chat_nouveau=False, chat_fichier=emp, agent_conv=None, agent_attente=None)

aeros = sorted(df["aerodrome"].unique())
tous_mois = sorted(df["mois"].unique())
incomplets = mois_info[mois_info["statut"] == "INCOMPLET"].index.tolist()
filtres_cat = {}  # catégorie de vol / origine-destination
valeurs_cat = {c: sorted(df[c].dropna().astype(str).unique()) for c in core.COLONNES_REPARTITION if c in df.columns}
# Filtres : clé liée à l'empreinte du fichier (remis à zéro si le fichier change) ; persist_state garde
# leur valeur quand on change de page, y compris en passant par Discuter où ils sont masqués.
with zone_filtres:
    st.caption(f"Période du fichier : du {meta['date_min']:%d/%m/%Y} au {meta['date_max']:%d/%m/%Y}")
    if not sur_tableau:  # la discussion et le journal portent toujours sur le fichier complet
        choix, inclure_inc, (d_deb, d_fin) = aeros, False, (tous_mois[0], tous_mois[-1])
    else:
        choix = st.multiselect("Aérodromes", aeros, default=aeros, key=f"filtre_aeros_{emp}", persist_state="session")
        inclure_inc = st.checkbox("Inclure les mois incomplets dans les graphiques", value=False, key=f"filtre_inc_{emp}",
                                  persist_state="session")
        d_deb, d_fin = st.select_slider("Période (mois)", options=tous_mois, value=(tous_mois[0], tous_mois[-1]),
                                        key=f"filtre_periode_{emp}", persist_state="session")
        for col, valeurs in valeurs_cat.items():
            filtres_cat[col] = st.multiselect(core.COLONNES_REPARTITION[col], valeurs, default=valeurs,
                                             key=f"filtre_{col}_{emp}", persist_state="session")
        if incomplets and not inclure_inc:
            st.warning("Mois incomplets exclus des graphiques : " + ", ".join(incomplets))

f = df[df["aerodrome"].isin(choix) & df["mois"].between(d_deb, d_fin)]
for col, valeurs in filtres_cat.items():
    f = f[f[col].astype(str).isin(valeurs)]
f_graph = f if inclure_inc else f[f["statut"] == "Complet"]

page.run()
