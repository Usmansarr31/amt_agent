import base64
import io
import os
import re
import time
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import plotly.express as px
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import copilot
import core
import questions
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


def page_vue():
    entete()
    st.header("Vue d'ensemble", divider="gray")
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


def page_comparaison():
    entete()
    st.header("Comparaison", divider="gray")
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


def page_retards():
    entete()
    st.header("Retards", divider="gray")
    r = core.retard_pondere(f)
    if r.empty:
        st.info("Pas de colonne retard_moyen_min dans ce fichier.")
        return
    st.plotly_chart(px.bar(r, x="aerodrome", y="retard_moyen_pondere_min", text_auto=True,
                           labels={"aerodrome": "Aérodrome", "retard_moyen_pondere_min": "Retard moyen pondéré (min)"},
                           title="Retard moyen pondéré par le nombre de mouvements"), width="stretch")
    st.dataframe(r, width="stretch", hide_index=True)
    st.caption("Moyenne pondérée par nombre_mouvements. Les retards vides sont exclus, pas remplacés par zéro.")


def page_anomalies():
    entete()
    st.header("Anomalies", divider="gray")
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
        if st.button("Enregistrer la décision"):
            traces.ajouter(traces.REGISTRE, {"fichier": os.path.basename(nom), "empreinte": emp, "element": element,
                                             "decision": decision, "commentaire": commentaire})
            st.session_state.message_registre = True
            st.rerun()  # pour mettre à jour le statut dans le tableau ci-dessus
        if st.session_state.pop("message_registre", False):
            st.success("Décision enregistrée dans le registre (traces/registre_validation.jsonl). "
                       "Les données d'origine ne sont pas modifiées.")
    reg = registre()
    if not reg.empty:
        st.subheader("Registre de validation")
        st.dataframe(reg.drop(columns=["empreinte"]), width="stretch", hide_index=True)


def page_qualite():
    entete()
    st.header("Qualité des données", divider="gray")
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


def excel_bytes():
    out = io.BytesIO()
    totaux = core.totaux_mensuels(df, mois_info)
    detail = df.pivot_table(index="mois", columns=["aerodrome", "type_mouvement"], values="nombre_mouvements", aggfunc="sum", fill_value=0)
    detail.columns = [f"{a} {t}" for a, t in detail.columns]
    rec2 = meta["brut_par_mois"].to_frame().join(df.groupby("mois")["nombre_mouvements"].sum().rename("total_nettoye"), how="outer").fillna(0)
    rec2["ecart"] = rec2["total_nettoye"] - rec2["total_brut"]
    anos = anomalies_avec_statut(df)
    comm = pd.DataFrame({"commentaire": [core.commentaire_tendance(df, mois_info)]})
    feuilles = {"Totaux mensuels": totaux, "Commentaire tendance": comm, "Detail arr-dep": detail.reset_index()}
    for col, libelle in core.COLONNES_REPARTITION.items():
        if col in df.columns:
            feuilles[f"Repartition {col.split('_')[0]}"] = core.repartition(df, col)
    feuilles.update({"Reconciliation brut": rec2.reset_index().rename(columns={"index": "mois", "date": "mois"}),
                     "Journal nettoyage": journal, "Anomalies": anos,
                     "Registre validation": registre().drop(columns=["empreinte"]),
                     "Journal assistant": journal_assistant().drop(columns=["empreinte"])})
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        for nom_f, d in feuilles.items():
            d.to_excel(w, sheet_name=nom_f, index=False, startrow=3)
            ws = w.sheets[nom_f]
            ws["A1"] = f"AMT - {nom_f}"
            ws["A2"] = MENTION
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = 22
    return out.getvalue()


def pdf_bytes():
    out = io.BytesIO()
    st_ = getSampleStyleSheet()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    el = [Paragraph("Rapport d'analyse du trafic aérien", st_["Title"]),
          Paragraph(f"Période : du {meta['date_min']:%d/%m/%Y} au {meta['date_max']:%d/%m/%Y} | Fichier : {escape(os.path.basename(nom))}", st_["Normal"]),
          Paragraph(MENTION, st_["Italic"]), Spacer(1, 0.4 * cm)]
    kp = [["Total nettoyé", "Total brut", "Écart", "Anomalies à valider"],
          [fmt(meta["total_nettoye"]), fmt(meta["total_brut"]),
           f"{meta['total_nettoye'] - meta['total_brut']:+,.0f}".replace(",", " "), str(int(df['anomalie'].sum()))]]
    t = Table(kp); t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    anos_st = anomalies_avec_statut(df)
    attente = int((anos_st["statut_validation"] == "En attente").sum())
    statut = (f"Statut de validation : {len(anos_st) - attente} anomalie(s) traitée(s) par l'expert, {attente} en attente. "
              + ("Des anomalies attendent encore une décision : rapport à ne pas utiliser en l'état." if attente
                 else "Toutes les anomalies ont reçu une décision de l'expert.")
              + " Validation finale de l'expert métier requise avant toute utilisation.")
    el += [t, Spacer(1, 0.3 * cm), Paragraph(escape(statut), st_["Normal"]), Spacer(1, 0.4 * cm)]
    comp = df[df["statut"] == "Complet"].groupby(["mois", "aerodrome"])["nombre_mouvements"].sum().unstack()
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    comp.plot(ax=ax, marker="o"); ax.set_xlabel("Mois"); ax.set_ylabel("Mouvements"); ax.set_title("Mouvements par mois (mois complets)")
    ax.tick_params(axis="x", rotation=45); ax.legend(title="Aérodrome", fontsize=7); fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=150); plt.close(fig); buf.seek(0)
    el += [Image(buf, width=17 * cm, height=7.2 * cm), Spacer(1, 0.3 * cm),
           Paragraph("Commentaire de tendance (calculé, mois complets seulement)", st_["Heading3"]),
           Paragraph(escape(core.commentaire_tendance(df, mois_info)), st_["Normal"]), Spacer(1, 0.3 * cm)]
    tm = core.totaux_mensuels(df, mois_info)
    data = [["Mois", "Jours", "Statut"] + [c for c in tm.columns if c not in ("mois", "jours_couverts", "statut")]]
    for r in tm.itertuples(index=False):
        data.append([r[0], int(r[1]), r[2]] + [fmt(v) for v in r[3:]])
    t2 = Table(data, repeatRows=1); t2.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 7), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    el += [Paragraph("Totaux mensuels", st_["Heading3"]), t2, Spacer(1, 0.3 * cm)]
    style_t = TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 7),
                          ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)])
    for col, libelle in core.COLONNES_REPARTITION.items():
        rp = core.repartition(df, col)
        if rp.empty:
            continue
        lignes = [list(rp.columns)] + [[r[0]] + [f"{v:.1f} %" if r[0] == "Part (%)" else fmt(v) for v in r[1:]]
                                       for r in rp.itertuples(index=False)]
        tr = Table(lignes, repeatRows=1); tr.setStyle(style_t)
        el += [Paragraph(f"Répartition : {libelle.lower()}", st_["Heading3"]), tr, Spacer(1, 0.3 * cm)]
    el.append(Paragraph("Anomalies à valider (cause inconnue)", st_["Heading3"]))
    if anos_st.empty:
        el.append(Paragraph("Aucune anomalie statistique.", st_["Normal"]))
    else:
        lignes = [["Date", "Aérodrome", "Type", "Mouvements", "Score", "Ligne", "Décision de l'expert"]]
        lignes += [[f"{r.date:%d/%m/%Y}", r.aerodrome, r.type_mouvement, fmt(r.nombre_mouvements), str(r.score_anomalie),
                    str(int(r.ligne_fichier)), r.statut_validation] for r in anos_st.itertuples()]
        ta = Table(lignes, repeatRows=1); ta.setStyle(style_t)
        el.append(ta)
    reg = registre()
    el.append(Paragraph("Registre de validation de l'expert métier", st_["Heading3"]))
    if reg.empty:
        el.append(Paragraph("Aucune décision enregistrée pour ce fichier.", st_["Normal"]))
    else:
        lignes = [["Horodatage", "Élément", "Décision", "Commentaire"]]
        lignes += [[r.horodatage, Paragraph(escape(str(r.element)), st_["BodyText"]), r.decision,
                    Paragraph(escape(str(r.commentaire or "")), st_["BodyText"])] for r in reg.itertuples()]
        tg = Table(lignes, repeatRows=1, colWidths=[3.2 * cm, 6 * cm, 2.2 * cm, 6.5 * cm]); tg.setStyle(style_t)
        el.append(tg)
    ja = journal_assistant()
    el.append(Paragraph("Questions posées à l'assistant (journal tracé)", st_["Heading3"]))
    if ja.empty:
        el.append(Paragraph("Aucune question posée pour ce fichier.", st_["Normal"]))
    else:
        resume = ", ".join(f"{k} : {v}" for k, v in ja["type_reponse"].value_counts().items())
        el.append(Paragraph(escape(f"{len(ja)} question(s) ; types de réponse : {resume}. "
                                   "Détail complet (question, réponse, chiffres) dans l'export Excel, feuille « Journal assistant »."),
                            st_["Normal"]))
        lignes = [["Horodatage", "Moteur", "Question", "Type de réponse"]]
        lignes += [[r.horodatage, Paragraph(escape(str(r.moteur)), st_["BodyText"]),
                    Paragraph(escape(str(r.question)), st_["BodyText"]), r.type_reponse]
                   for r in ja.tail(20).itertuples()]
        tj = Table(lignes, repeatRows=1, colWidths=[3.2 * cm, 3.4 * cm, 7.5 * cm, 3.8 * cm]); tj.setStyle(style_t)
        el.append(tj)
        if len(ja) > 20:
            el.append(Paragraph("(20 dernières questions affichées)", st_["Italic"]))
    el.append(Paragraph("Nettoyage", st_["Heading3"]))
    for p_, n in journal["probleme"].str.replace(r" \(colonne .*\)", "", regex=True).str.replace(r"^\d+ jour.*", "Jours sans ligne", regex=True).value_counts().items():
        el.append(Paragraph(f"{p_} : {n}", st_["Normal"]))
    el += [Spacer(1, 0.3 * cm), Paragraph("Validation de l'expert métier requise avant toute utilisation. Aucune cause n'est avancée sans donnée pour la prouver.", st_["Italic"])]
    doc.build(el)
    return out.getvalue()


def page_exports():
    entete()
    st.header("Exports", divider="gray")
    st.write("Les exports portent sur l'ensemble du fichier, sans les filtres, pour garder la traçabilité complète.")
    e1, e2, e3 = st.columns(3)
    e1.download_button("Export Excel", excel_bytes(), "amt_analyse_trafic.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", icon=":material/table_view:")
    e2.download_button("Export PDF", pdf_bytes(), "amt_rapport_trafic.pdf", "application/pdf", icon=":material/picture_as_pdf:")
    e3.download_button("Données nettoyées (CSV)", df.drop(columns=["statut"]).to_csv(index=False).encode("utf-8-sig"),
                       "trafic_nettoye.csv", "text/csv", icon=":material/download:")


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


def interroger_agent(attente, sur_etat=None):
    """Envoie la seule question à l'agent Copilot Studio (mode secret Direct Line) et ajoute sa réponse,
    telle quelle, au fil. Le secret est lu par copilot.py dans l'environnement, jamais ici."""
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
        r = copilot.demander(conv, question, sur_etat=sur_etat)
        texte = "\n\n".join(r.textes)
        st.session_state.chat.append({"role": "assistant", "content": texte, "pieces": r.pieces_jointes,
                                      "fichiers": r.fichiers, "incomplete": not r.complete})
        traces.ajouter(traces.JOURNAL, {**entree, "type_reponse": "reponse_agent" if r.complete
                                        else "reponse_agent_incomplete", "reponse": texte,
                                        "conversation_agent": r.conversation_id, "duree_s": r.duree})
    except Exception as ex:  # jamais de plantage : message clair et secours discret
        raison = str(ex) if isinstance(ex, copilot.ErreurAgent) else f"erreur inattendue ({type(ex).__name__})."
        texte = f"L'agent n'a pas pu répondre : {raison}"
        st.session_state.chat.append({"role": "assistant", "content": texte, "echec": question,
                                      "langue": attente["langue"]})
        traces.ajouter(traces.JOURNAL, {**entree, "type_reponse": "erreur_agent", "reponse": texte,
                                        "conversation_agent": conv.id if conv else None,
                                        "duree_s": round(time.time() - debut, 1)})


def secours_local(question, lg):
    repondre_local(question, lg)
    st.session_state.chat_nouveau = True


def nouvelle_conversation():
    st.session_state.update(chat=[], chat_nouveau=False, agent_conv=None, agent_attente=None)


def page_tracabilite():
    entete()
    st.header("Traçabilité", divider="gray")
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
        st.dataframe(ja.drop(columns=["empreinte"]), width="stretch", hide_index=True,
                     column_config={"reponse": st.column_config.TextColumn("réponse", width="large")})
        st.download_button("Journal de l'assistant (CSV)", ja.to_csv(index=False).encode("utf-8-sig"),
                           "journal_assistant.csv", "text/csv", icon=":material/download:")


def effet_frappe(texte):
    for morceau in re.split(r"(\s+)", texte):
        yield morceau
        time.sleep(0.008)


TAILLE_MAX_JOINT = 20 * 1024 * 1024


def fichier_data(url):
    """Contenu d'une adresse data: (base64), ou None si elle est invalide ou trop grande."""
    entete_d, _, donnees = url.partition(",")
    if not entete_d.endswith(";base64") or len(donnees) > TAILLE_MAX_JOINT * 4 // 3 + 4:
        return None
    try:
        return base64.b64decode(donnees, validate=True)
    except ValueError:
        return None


def afficher_fichiers(i, fichiers):
    """Fichiers envoyés par l'agent : lien https ouvert dans un nouvel onglet, ou téléchargement direct."""
    with st.container(horizontal=True):
        for k, f in enumerate(fichiers):
            nom_j = Path(f["nom"]).name or "fichier_agent"
            if f["url"].startswith("https://"):
                st.link_button(nom_j, f["url"], icon=":material/download:")
            else:
                contenu = fichier_data(f["url"])
                if contenu is None:
                    st.caption(f"{nom_j} : fichier joint illisible ou trop volumineux.")
                else:
                    st.download_button(nom_j, contenu, nom_j, f["type"], key=f"joint_{i}_{k}",
                                       icon=":material/download:", on_click="ignore")


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
            st.download_button("Rapport Excel", excel_bytes, "amt_analyse_trafic.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               icon=":material/table_view:", width="stretch", on_click="ignore")
            st.download_button("Rapport PDF", pdf_bytes, "amt_rapport_trafic.pdf", "application/pdf",
                               icon=":material/picture_as_pdf:", width="stretch", on_click="ignore")
            st.download_button("Données nettoyées (CSV)",
                               lambda: df.drop(columns=["statut"]).to_csv(index=False).encode("utf-8-sig"),
                               "trafic_nettoye.csv", "text/csv", icon=":material/dataset:", width="stretch",
                               on_click="ignore")
        st.caption("Les rapports portent sur tout le fichier, sans les filtres.")


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
                    attente_ui = {"spinner": None, "texte": None}

                    def afficher_attente(texte):
                        if attente_ui["texte"] == texte:
                            return
                        if attente_ui["spinner"] is not None:
                            attente_ui["spinner"].__exit__(None, None, None)
                        with zone.container():
                            sp = st.spinner(texte)
                            sp.__enter__()
                        attente_ui.update(spinner=sp, texte=texte)

                    afficher_attente("L'agent analyse le fichier...")
                    try:  # activité « typing » reçue : l'agent travaille encore
                        interroger_agent(attente, sur_etat=lambda _e: afficher_attente("L'agent réfléchit..."))
                    finally:
                        attente_ui["spinner"].__exit__(None, None, None)
                st.session_state.update(agent_attente=None, chat_nouveau=True)
                st.rerun()
    st.chat_input("Posez votre question à l'agent AMT...", key="chat_saisie", on_submit=poser, max_chars=500)


# =====================================================================
# Assistant : agent Copilot Studio
# =====================================================================
def page_agent():
    st.title("Voir l'agent")
    st.caption("L'agent conversationnel du projet, intégré par son adresse ou son code d'intégration.")
    saisie = st.text_area("Adresse https:// de l'agent ou code d'intégration <iframe>",
                          value=os.environ.get("AMT_AGENT_URL", ""), height=90, key="agent_saisie",
                          placeholder="https://copilotstudio.microsoft.com/environments/.../webchat?... ou <iframe src=\"https://...\"></iframe>")
    adresse, erreur = None, None
    texte = saisie.strip()
    if texte:
        if "<iframe" in texte.lower():
            m = re.search(r"""src\s*=\s*["'](https://[^"'\s]+)["']""", texte, re.I)
            if m:
                adresse = m.group(1).replace("&amp;", "&")
            else:
                erreur = "Code <iframe> reconnu, mais sans adresse src en https://."
        elif re.fullmatch(r"https://\S+", texte):
            adresse = texte
        elif texte.lower().startswith("http://"):
            erreur = "Adresse refusée : utilisez une adresse sécurisée https://."
        else:
            erreur = "Saisie non reconnue : collez une adresse https:// ou un code <iframe>."
    if erreur:
        st.warning(erreur)
    if adresse:
        st.link_button("Ouvrir l'agent dans un nouvel onglet", adresse, icon=":material/open_in_new:")
        st.iframe(adresse, height=620, alt="Agent Copilot Studio")
        st.caption("Si la fenêtre reste blanche, le site de l'agent bloque l'affichage intégré : utilisez le bouton "
                   "« Ouvrir l'agent dans un nouvel onglet ». Pour la sécurité, seul l'attribut src d'un code <iframe> "
                   "est utilisé : aucun script collé n'est exécuté.")
    with st.expander("Mode d'emploi : intégrer l'agent Copilot Studio", expanded=not adresse):
        st.markdown(
            "1. Dans **Copilot Studio**, ouvrir l'agent puis cliquer sur **Publier** (à refaire après chaque modification).\n"
            "2. Ouvrir **Canaux** (*Channels*).\n"
            "3. Choisir **Site web de démonstration** (*Demo website*) et copier l'**adresse https://**, "
            "ou **Site web personnalisé** (*Custom website*) et copier le **code d'intégration** `<iframe>`.\n"
            "4. Coller l'adresse ou le code dans la zone ci-dessus : l'agent s'affiche dans la page.\n"
            "5. Si l'affichage est bloqué ou demande une connexion, utiliser **Ouvrir l'agent dans un nouvel onglet**.\n\n"
            "Astuce démonstration : lancer l'application avec `AMT_AGENT_URL=https://... streamlit run app.py` "
            "pour pré-remplir l'adresse. L'intégration dans un site peut exiger un réglage de l'authentification "
            "de l'agent (Paramètres > Sécurité) : à vérifier selon la politique de votre organisation.")


# =====================================================================
# Navigation (barre latérale), chargement et filtres
# =====================================================================
P_CHAT = st.Page(page_chat, title="Discuter", icon=":material/chat:", url_path="discuter")
P_AGENT = st.Page(page_agent, title="Voir l'agent", icon=":material/smart_toy:", url_path="agent")
page = st.navigation({
    "Tableau de bord": [
        st.Page(page_vue, title="Vue d'ensemble", icon=":material/dashboard:", url_path="vue", default=True),
        st.Page(page_comparaison, title="Comparaison", icon=":material/compare_arrows:", url_path="comparaison"),
        st.Page(page_retards, title="Retards", icon=":material/schedule:", url_path="retards"),
        st.Page(page_anomalies, title="Anomalies", icon=":material/report:", url_path="anomalies"),
        st.Page(page_qualite, title="Qualité des données", icon=":material/fact_check:", url_path="qualite"),
        st.Page(page_tracabilite, title="Traçabilité", icon=":material/history:", url_path="tracabilite"),
        st.Page(page_exports, title="Exports", icon=":material/download:", url_path="exports"),
    ],
    "Assistant": [P_CHAT, P_AGENT],
}, position="sidebar")
sur_chat = page.url_path == P_CHAT.url_path

if page.url_path == P_AGENT.url_path:  # l'intégration de l'agent ne dépend pas du fichier
    page.run()
    st.stop()

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
    if sur_chat:  # la discussion porte toujours sur le fichier complet
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
