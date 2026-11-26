"""Rapport_AMT : export Excel et PDF au format des livrables de l'agent AMT (consignes « EXPORTS »).

- Excel Rapport_AMT_<date>.xlsx : feuille « Tableau de bord » (bandeau, filtres Période / Aérodrome / Catégorie,
  5 cartes, ligne du mois incomplet, 7 graphiques), cartes et graphiques 1 à 6 recalculés par formules SUMIFS
  selon les filtres, tables de calcul sur la feuille masquée « _calcul », puis une feuille par fichier source
  (données nettoyées, en-têtes en français, filtre automatique). Aucune fonction TEXT(), aucun dLblPos.
- PDF Rapport_AMT_<date>.pdf (reportlab) : page 1 tableau de bord, puis l'analyse en 10 sections (le chiffre,
  comment le lire, ce qu'il montre, ce qu'il faut vérifier), dernière page : registre de validation.
- relire() : contrôles avant livraison (totaux recalculés, deux mois, archive .xlsx, recalcul LibreOffice).

Tout chiffre vient d'un calcul sur le jeu de données nettoyé par core.nettoyer : rien n'est estimé.
Aucune cause n'est avancée pour un pic ou une anomalie : la cause est inconnue, à vérifier par l'expert.
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.chart import BarChart, DoughnutChart, LineChart, Reference, ScatterChart, Series  # noqa: E402
from openpyxl.chart.label import DataLabelList  # noqa: E402
from openpyxl.chart.data_source import StrRef  # noqa: E402
from openpyxl.chart.marker import DataPoint, Marker  # noqa: E402
from openpyxl.chart.series import SeriesLabel  # noqa: E402
from openpyxl.chart.shapes import GraphicalProperties  # noqa: E402
from openpyxl.chart.text import RichText  # noqa: E402
from openpyxl.drawing.line import LineProperties  # noqa: E402
from openpyxl.drawing.text import CharacterProperties, Paragraph as ParaXL, ParagraphProperties  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

import core  # noqa: E402

MENTION = "Données fictives - validation humaine requise"
NUIT, SARCELLE, BLEU, OCRE, PIC = "183549", "087E80", "386594", "DA9941", "C0392B"
GRIS = "F2F4F7"
PALETTE = [NUIT, SARCELLE, BLEU, OCRE, "7A8BA8", "5FB3B3"]
LIBELLES = {"REGULIER": "Régulier", "NON_REGULIER": "Non régulier", "AVIATION_GENERALE": "Aviation générale",
            "ARRIVEE": "Arrivée", "DEPART": "Départ", "INTERNATIONAL": "International", "DOMESTIQUE": "Domestique",
            "REGION_CENTRE": "Région centre", "REGION_OUEST": "Région ouest"}
JOURS_SEMAINE = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_COURTS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
ELEMENT_EXPORT = "Rapport d'export (Excel et PDF)"
NON_RENSEIGNE = "Non renseigné"


def clair(v):
    """Libellé en clair (REGULIER -> Régulier) ; valeur vide -> « Non renseigné »."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "None"):
        return NON_RENSEIGNE
    return LIBELLES.get(str(v), str(v))


def n0(x):
    return core.fmt_nombre(x)


def n1(x):
    return core.fmt_nombre(x, 1)


def n2(x):
    return core.fmt_nombre(x, 2)


def pct(x, d=2, signe=True):
    s = core.fmt_nombre(x * 100, d) + " %"
    return ("+" + s if x >= 0 else s) if signe else s


def lm(m):
    return core.libelle_mois(m)


def nom_fichiers(date=None):
    d = (date or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return f"Rapport_AMT_{d}.xlsx", f"Rapport_AMT_{d}.pdf"


# ===========================================================================
# Analyse : tous les chiffres du rapport, calculés une seule fois
# ===========================================================================
def sources_excel(contenu, nom):
    """Feuilles sources d'un classeur (agrégats, référentiel) en plus du trafic journalier, sinon {}."""
    if not nom.lower().endswith((".xlsx", ".xls")):
        return {}
    try:
        feuilles = pd.read_excel(io.BytesIO(contenu), sheet_name=None)
    except Exception:
        return {}
    out = {}
    for nom_f, d in feuilles.items():
        n = nom_f.lower()
        if "agreg" in n or "agrég" in n:
            out["agregats"] = d
        elif "referentiel" in n or "référentiel" in n:
            out["referentiel"] = d
    return out


def _retard(d):
    if "retard_moyen_min" not in d.columns:
        return None
    x = d.dropna(subset=["retard_moyen_min"])
    den = x["nombre_mouvements"].sum()
    return float((x["nombre_mouvements"] * x["retard_moyen_min"]).sum() / den) if den else None


def analyser(df, journal, mois_info, meta, nom_fichier="", empreinte="", sources=None):
    """Chiffres du rapport, sur le fichier complet (sans filtre)."""
    sources = sources or {}
    A = {"fichier": nom_fichier, "empreinte": empreinte, "sources": sources, "df": df, "journal": journal,
         "mois_info": mois_info, "meta": meta}
    A["total"] = float(df["nombre_mouvements"].sum())
    A["lignes"] = len(df)
    A["jours"] = int(df["date"].nunique())
    A["moyenne_jour"] = A["total"] / A["jours"] if A["jours"] else 0
    A["date_min"], A["date_max"] = meta["date_min"], meta["date_max"]
    A["retard"] = _retard(df)
    statut = mois_info["statut"].to_dict()
    jours = mois_info["jours_couverts"].to_dict()
    mensuel = df.groupby("mois")["nombre_mouvements"].sum()
    A["mensuel"] = mensuel
    A["mois"] = list(mensuel.index)
    A["complets"] = [m for m in A["mois"] if statut.get(m) == "Complet"]
    A["incomplets"] = [(m, float(mensuel[m]), int(jours.get(m, 0))) for m in A["mois"] if statut.get(m) != "Complet"]
    A["statut"], A["jours_mois"] = statut, jours
    A["partiels_retires"] = list(meta.get("mois_partiels_retires") or [])  # (mois, jours, mouvements, lignes)
    comp = mensuel[A["complets"]]
    A["pointe"] = comp.idxmax() if len(comp) else None
    A["calme"] = comp.idxmin() if len(comp) else None
    A["moyenne_mensuelle"] = float(comp.mean()) if len(comp) else None
    # Variation N/N-1 : mois complets de l'année du dernier mois complet, dont le même mois N-1 est complet
    A["annee_ref"] = A["complets"][-1][:4] if A["complets"] else None
    inclus = [m for m in A["complets"] if m[:4] == A["annee_ref"] and core._decaler(m, -12) in A["complets"]]
    A["mois_variation"] = inclus
    if inclus:
        n = float(mensuel[inclus].sum())
        n1_ = float(sum(mensuel[core._decaler(m, -12)] for m in inclus))
        A["n"], A["n1"], A["variation"] = n, n1_, (n / n1_ - 1) if n1_ else None
    else:
        A["n"] = A["n1"] = A["variation"] = None
    # Aérodromes
    ref = sources.get("referentiel")
    noms = {}
    if ref is not None and {"code"} <= set(ref.columns):
        col_nom = next((c for c in ref.columns if "nom" in c.lower()), None)
        if col_nom:
            noms = {str(c).strip().upper(): v for c, v in zip(ref["code"], ref[col_nom])}
    t = df.groupby("aerodrome")["nombre_mouvements"].sum().sort_values(ascending=False)
    aeros = pd.DataFrame({"aerodrome": t.index, "total": t.values})
    aeros["nom"] = [noms.get(a, "") for a in aeros["aerodrome"]]
    aeros["part"] = aeros["total"] / A["total"] if A["total"] else 0
    aeros["rang"] = range(1, len(aeros) + 1)
    aeros["retard"] = [_retard(df[df["aerodrome"] == a]) for a in aeros["aerodrome"]]
    if inclus:
        n_a = df[df["mois"].isin(inclus)].groupby("aerodrome")["nombre_mouvements"].sum()
        n1_a = df[df["mois"].isin([core._decaler(m, -12) for m in inclus])].groupby("aerodrome")["nombre_mouvements"].sum()
        aeros["n"] = [float(n_a.get(a, 0)) for a in aeros["aerodrome"]]
        aeros["n1"] = [float(n1_a.get(a, 0)) for a in aeros["aerodrome"]]
        aeros["variation"] = [(x / y - 1) if y else None for x, y in zip(aeros["n"], aeros["n1"])]
    A["aeros"] = aeros
    A["principal"] = aeros.iloc[0]["aerodrome"] if len(aeros) else None
    # Catégories, sens, retard mensuel
    A["categories"] = (df["categorie_vol"].map(clair).value_counts().index.tolist()
                       if "categorie_vol" in df.columns else [])
    A["cat_totaux"] = (df.assign(c=df["categorie_vol"].map(clair)).groupby("c")["nombre_mouvements"].sum()
                       .sort_values(ascending=False) if "categorie_vol" in df.columns else pd.Series(dtype=float))
    A["origine_totaux"] = (df.assign(c=df["origine_destination"].map(clair)).groupby("c")["nombre_mouvements"].sum()
                           .sort_values(ascending=False) if "origine_destination" in df.columns else pd.Series(dtype=float))
    sens = df.assign(s=df["type_mouvement"].map(clair)).groupby("s")["nombre_mouvements"].sum()
    A["sens"] = sens
    A["part_arrivees"] = float(sens.get("Arrivée", 0) / A["total"]) if A["total"] else 0
    A["retard_mensuel"] = pd.Series({m: _retard(df[df["mois"] == m]) for m in A["mois"]}, dtype=float)
    A["pics"] = _pics(df, A)
    A["qualite"] = _qualite(df, journal, meta, sources, A)
    return A


def _pics(df, A):
    """Journées atypiques (|score| > 6) : pourquoi c'est un pic, par le calcul seulement (jamais de cause)."""
    pics = []
    for _, x in df[df["anomalie"] == 1].sort_values("date").iterrows():
        serie = df[(df["aerodrome"] == x["aerodrome"]) & (df["type_mouvement"] == x["type_mouvement"])]
        jour = serie.groupby("date")["nombre_mouvements"].sum().sort_index()
        mediane = float(serie["nombre_mouvements"].median())
        d = pd.Timestamp(x["date"])
        rang = int((jour > x["nombre_mouvements"]).sum()) + 1
        veille, lendemain = jour.get(d - pd.Timedelta(days=1)), jour.get(d + pd.Timedelta(days=1))
        isole = all(v is not None and abs(v - mediane) < abs(x["nombre_mouvements"] - mediane) / 3
                    for v in (veille, lendemain))
        total_mois = float(A["mensuel"].get(x["mois"], 0))
        autre_sens = df[(df["date"] == d) & (df["aerodrome"] == x["aerodrome"]) &
                        (df["type_mouvement"] != x["type_mouvement"])]["nombre_mouvements"].sum()
        autres_aeros = (df[(df["date"] == d) & (df["aerodrome"] != x["aerodrome"]) &
                           (df["type_mouvement"] == x["type_mouvement"])]
                        .groupby("aerodrome")["nombre_mouvements"].sum())
        effet_variation = None
        if x["mois"] in A["mois_variation"] and A["n1"]:
            sans = (A["n"] - x["nombre_mouvements"] + mediane) / A["n1"] - 1
            effet_variation = (A["variation"], sans)
        pics.append({"date": d, "aerodrome": x["aerodrome"], "type": x["type_mouvement"],
                     "valeur": float(x["nombre_mouvements"]), "mediane": mediane,
                     "rapport": x["nombre_mouvements"] / mediane if mediane else None,
                     "score": float(x["score_anomalie"]), "rang": rang, "nb_jours": len(jour),
                     "veille": veille, "lendemain": lendemain, "isole": isole,
                     "jour_semaine": JOURS_SEMAINE[d.weekday()], "mois": x["mois"], "total_mois": total_mois,
                     "part_mois": x["nombre_mouvements"] / total_mois if total_mois else None,
                     "total_hors": A["total"] - x["nombre_mouvements"], "autre_sens": float(autre_sens),
                     "autres_aeros": autres_aeros.to_dict(), "serie": jour, "effet_variation": effet_variation,
                     "ligne": int(x["ligne_fichier"])})
    return pics


def explication_pic(p):
    """Pourquoi c'est un pic, par le calcul seulement (règle PICS) : titre et 4 temps, en texte simple.
    Aucune cause ni hypothèse de cause."""
    voisins = (f"veille {n0(p['veille']) if p['veille'] is not None else 'absente'}, "
               f"lendemain {n0(p['lendemain']) if p['lendemain'] is not None else 'absent'}")
    autres = ", ".join(f"{a} {n0(v)}" for a, v in p["autres_aeros"].items()) or "aucun"
    effet = ""
    if p["effet_variation"]:
        avec, sans = p["effet_variation"]
        effet = f" La variation N/N-1 passe de {pct(sans)} (pic remplacé par la médiane) à {pct(avec)} avec le pic."
    sens = clair(p["type"]).lower()
    autre_sens = clair("DEPART" if p["type"] == "ARRIVEE" else "ARRIVEE").lower()
    return {
        "titre": f"Journée atypique : {p['aerodrome']}, {sens}s, {p['jour_semaine']} {p['date']:%d/%m/%Y}",
        "chiffre": (f"{n0(p['valeur'])} mouvements contre une médiane de {n0(p['mediane'])}, soit × {n1(p['rapport'])}. "
                    f"Score {n1(p['score'])} pour un seuil de 6. Rang {p['rang']} sur {p['nb_jours']} jours de la série."),
        "lire": (f"Le score mesure l'écart à la médiane habituelle de la série {p['aerodrome']} {sens}s. "
                 "Au-delà de 6, la journée est signalée."),
        "montre": (f"Le pic est {'isolé' if p['isole'] else 'entouré de valeurs elles aussi élevées'} ({voisins}). "
                   f"Il pèse {pct(p['part_mois'], 1, False)} du total de {lm(p['mois'])}. Hors anomalie, le total du "
                   f"fichier serait de {n0(p['total_hors'])}.{effet} Le même jour : {autre_sens}s {n0(p['autre_sens'])} ; "
                   f"autres aérodromes ({sens}s) : {autres}."),
        "verifier": (f"La valeur reste dans les totaux. Cause inconnue : vérifiez la ligne {p['ligne']} du fichier "
                     "et validez ou rejetez l'anomalie."),
    }


def explications_mois(A):
    """Mois de pointe et mois le plus calme (mois complets) : pourquoi ce sont des pics, par le calcul."""
    mens, out = A["mensuel"], []
    for m, nom_m in ((A["pointe"], "Mois de pointe"), (A["calme"], "Mois le plus calme")):
        if not m:
            continue
        contrib = A["df"][A["df"]["mois"] == m].groupby("aerodrome")["nombre_mouvements"].sum().sort_values(ascending=False)
        m1 = core._decaler(m, -12)
        prec = (f"{n0(mens[m1])} en {lm(m1)}" + ("" if A["statut"].get(m1) == "Complet" else " (mois incomplet)")
                if m1 in mens.index else "indisponible")
        out.append({"mois": m, "titre": f"{nom_m} : {lm(m)}",
                    "chiffre": (f"{n0(mens[m])} mouvements, soit {pct(mens[m] / A['moyenne_mensuelle'] - 1)} par rapport "
                                f"à la moyenne mensuelle ({n0(A['moyenne_mensuelle'])})."),
                    "lire": "On compare le mois à la moyenne des mois complets.",
                    "montre": ("Aérodromes qui y contribuent le plus : " +
                               ", ".join(f"{a} {n0(v)}" for a, v in contrib.head(3).items()) +
                               f". Même mois un an plus tôt : {prec}."),
                    "verifier": "Cause inconnue : à vérifier par l'expert."})
    return out


def _qualite(df, journal, meta, sources, A):
    j = journal
    q = {"doublons": j[j["probleme"] == "Doublon exact"], "vides": j[j["probleme"] == "Valeur vide"],
         "negatifs": j[j["probleme"] == "Valeur négative"], "retard_vide": j[j["probleme"] == "Retard vide"],
         "consignes": j[j["probleme"].astype(str).str.startswith("Texte ressemblant")],
         "jours_absents": j[j["probleme"].astype(str).str.contains("jour\\(s\\) sans ligne")]}
    q["ecart_brut"] = meta["total_nettoye"] - meta["total_brut"]
    ag = sources.get("agregats")
    q["agregats"] = None
    if ag is not None and {"mois", "aerodrome"} <= set(ag.columns):
        col = next((c for c in ag.columns if "mouvement" in c.lower()), None)
        if col:
            # Comparaison sur le même périmètre (mois x aérodrome présents dans les agrégats)
            a = ag.assign(aerodrome=ag["aerodrome"].astype(str).str.strip().str.upper(), mois=ag["mois"].astype(str))
            a = pd.to_numeric(a.set_index(["mois", "aerodrome"])[col], errors="coerce").groupby(level=[0, 1]).sum()
            cl = df.groupby(["mois", "aerodrome"])["nombre_mouvements"].sum()
            perim = cl.reindex(a.index).fillna(0)
            dbl = q["doublons"].assign(mois=q["doublons"]["date"].astype(str).str[:7])
            dbl_perim = float(sum(v for mo, ae, v in zip(dbl["mois"], dbl["aerodrome"], pd.to_numeric(dbl["valeur"], errors="coerce"))
                                  if (mo, ae) in a.index and pd.notna(v)))
            hors = [mo for mo in A["mois"] if mo not in set(a.index.get_level_values(0))]
            q["agregats"] = {"total": float(a.sum()), "nettoye_perimetre": float(perim.sum()), "doublons": dbl_perim,
                             "ecart": float(a.sum() - perim.sum()),
                             "non_explique": float(a.sum() - perim.sum() - dbl_perim),
                             "mois_hors": [(mo, float(A["mensuel"][mo])) for mo in hors]}
    return q


# ===========================================================================
# Excel
# ===========================================================================
def _police_axes(axe, taille=900):
    cp = CharacterProperties(sz=taille)
    axe.txPr = RichText(p=[ParaXL(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])
    axe.delete = False


def _legende(chart, position="b"):
    chart.legend.position = position
    chart.legend.overlay = False
    cp = CharacterProperties(sz=900)
    chart.legend.txPr = RichText(p=[ParaXL(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])


def _titre_chart(chart, texte):
    chart.title = texte
    chart.title.overlay = False


def _couleur_serie(s, couleur, ligne=True):
    s.graphicalProperties = GraphicalProperties(solidFill=couleur)
    s.graphicalProperties.line = LineProperties(solidFill=couleur, w=22000) if ligne else LineProperties(noFill=True)


def _marqueurs_seuls(s, couleur, taille=8):
    s.marker = Marker(symbol="circle", size=taille)
    s.marker.graphicalProperties = GraphicalProperties(solidFill=couleur)
    s.marker.graphicalProperties.line = LineProperties(solidFill=couleur)
    s.graphicalProperties = GraphicalProperties()
    s.graphicalProperties.line = LineProperties(noFill=True)
    s.smooth = False


def construire_excel(A, registre=None, validation=None):
    """Classeur Rapport_AMT (octets). Les cartes et les graphiques 1 à 6 se recalculent selon les filtres."""
    df = A["df"]
    wb = Workbook()
    tb = wb.active
    tb.title = "Tableau de bord"
    calc = wb.create_sheet("_calcul")
    src = wb.create_sheet("Trafic journalier")

    # ---- Feuille source : données nettoyées, en-têtes en français, colonnes d'aide ----
    colonnes = [("date", "Date"), ("aerodrome", "Aérodrome"), ("pays", "Pays"),
                ("type_mouvement", "Type de mouvement"), ("categorie_vol", "Catégorie de vol"),
                ("origine_destination", "Origine / destination"), ("nombre_mouvements", "Nombre de mouvements"),
                ("retard_moyen_min", "Retard moyen (min)"), ("source", "Source"),
                ("ligne_fichier", "Ligne du fichier"), ("score_anomalie", "Score d'anomalie"),
                ("anomalie", "Anomalie à valider")]
    colonnes = [(c, t) for c, t in colonnes if c in df.columns or c in ("categorie_vol", "retard_moyen_min")]
    entetes = [t for _, t in colonnes] + ["Mois", "Mois complet", "Retard × mouvements", "Mouvements avec retard"]
    src.append(entetes)
    idx = {c: i + 1 for i, (c, _) in enumerate(colonnes)}
    L = {c: get_column_letter(i) for c, i in idx.items()}
    n_col = len(colonnes)
    L_mois, L_comp, L_rxm, L_mar = (get_column_letter(n_col + k) for k in range(1, 5))
    d_trie = df.sort_values(["date", "aerodrome", "type_mouvement"]).reset_index(drop=True)
    for i, x in enumerate(d_trie.itertuples(index=False), start=2):
        x = x._asdict()
        ligne = []
        for c, _ in colonnes:
            v = x.get(c)
            if c == "date":
                v = pd.Timestamp(v).to_pydatetime()
            elif c in ("type_mouvement", "categorie_vol", "origine_destination"):
                v = clair(v)
            elif c == "anomalie":
                v = "Oui" if v == 1 else "Non"
            elif isinstance(v, float) and pd.isna(v):
                v = None
            ligne.append(v)
        g, h = f"{L['nombre_mouvements']}{i}", f"{L['retard_moyen_min']}{i}"
        ligne += [x["mois"], "Oui" if A["statut"].get(x["mois"]) == "Complet" else "Non",
                  f'=IF({h}="","",{h}*{g})', f'=IF({h}="","",{g})']
        src.append(ligne)
    n_lignes = len(d_trie) + 1
    for cell in src[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NUIT)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for col in range(1, len(entetes) + 1):
        src.column_dimensions[get_column_letter(col)].width = 16
    for row in src.iter_rows(min_row=2, max_row=n_lignes, min_col=idx["date"], max_col=idx["date"]):
        row[0].number_format = "dd/mm/yyyy"
    for nom_c, fmt in (("nombre_mouvements", "#,##0"), ("retard_moyen_min", "0.00"), ("score_anomalie", "0.00")):
        if nom_c in idx:
            for row in src.iter_rows(min_row=2, max_row=n_lignes, min_col=idx[nom_c], max_col=idx[nom_c]):
                row[0].number_format = fmt
    for lettre, fmt in ((L_rxm, "#,##0.00"), (L_mar, "#,##0")):
        for row in src[f"{lettre}2:{lettre}{n_lignes}"]:
            row[0].number_format = fmt
    src.auto_filter.ref = f"A1:{L_mar}{n_lignes}"
    src.freeze_panes = "A2"
    if "categorie_vol" not in df.columns:
        for row in src[f"{L['categorie_vol']}2:{L['categorie_vol']}{n_lignes}"]:
            row[0].value = NON_RENSEIGNE

    def plage(lettre):
        return f"'Trafic journalier'!${lettre}$2:${lettre}${n_lignes}"

    MVT, AER, TYP, CAT = plage(L["nombre_mouvements"]), plage(L["aerodrome"]), plage(L["type_mouvement"]), plage(L["categorie_vol"])
    MOI, RXM, MAR = plage(L_mois), plage(L_rxm), plage(L_mar)

    # ---- Autres fichiers sources (une feuille chacun, données nettoyées, en-têtes en français) ----
    traductions = {"mois": "Mois", "aerodrome": "Aérodrome", "mouvements_bruts": "Mouvements bruts",
                   "nombre_lignes": "Nombre de lignes", "note": "Note", "code": "Code", "nom_fictif": "Nom (fictif)",
                   "pays": "Pays", "base_mouvements_jour": "Base de mouvements par jour"}
    for cle, titre in (("agregats", "Agrégats mensuels bruts"), ("referentiel", "Référentiel aérodromes")):
        d = A["sources"].get(cle)
        if d is None:
            continue
        d = d.copy()
        for c in d.columns:
            if c in ("aerodrome", "code"):
                d[c] = d[c].astype(str).str.strip().str.upper()
        ws = wb.create_sheet(titre)
        ws.append([traductions.get(c, c) for c in d.columns])
        for r in d.itertuples(index=False):
            ws.append([None if (isinstance(v, float) and pd.isna(v)) else (v.item() if hasattr(v, "item") else v)
                       for v in r])
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=NUIT)
        for col in range(1, len(d.columns) + 1):
            ws.column_dimensions[get_column_letter(col)].width = 22
        ws.auto_filter.ref = f"A1:{get_column_letter(len(d.columns))}{len(d) + 1}"
        for j, c in enumerate(d.columns, start=1):
            if pd.api.types.is_numeric_dtype(d[c]):
                for row in ws.iter_rows(min_row=2, max_row=len(d) + 1, min_col=j, max_col=j):
                    row[0].number_format = "#,##0"

    # ---- Tableau de bord : bandeau et filtres ----
    tb.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHIJKLMNOPQRST", [2] + [13] * 19):
        tb.column_dimensions[col].width = w
    tb.merge_cells("B1:Q1")
    tb["B1"] = "AMT - Analyse du trafic aérien : tableau de bord"
    tb["B1"].font = Font(bold=True, size=16, color="FFFFFF")
    tb.merge_cells("B2:Q2")
    tb["B2"] = (f"{MENTION} · Fichier : {A['fichier']} · Période du fichier : du {A['date_min']:%d/%m/%Y} au "
                f"{A['date_max']:%d/%m/%Y} · " + _statut_validation(validation))
    tb["B2"].font = Font(size=10, color="FFFFFF")
    for r in (1, 2):
        for c in range(2, 18):
            tb.cell(row=r, column=c).fill = PatternFill("solid", fgColor=NUIT)
    tb.row_dimensions[1].height = 30
    filtres = [("Période", 4), ("Aérodrome", 5), ("Catégorie", 6)]
    for libelle, r in filtres:
        tb[f"B{r}"] = libelle
        tb[f"B{r}"].font = Font(bold=True, color=NUIT)
        tb[f"C{r}"] = "Tous"
        tb[f"C{r}"].fill = PatternFill("solid", fgColor="FFFFFF")
        tb[f"C{r}"].border = Border(*(Side(style="thin", color=BLEU),) * 4)
        tb.merge_cells(f"C{r}:D{r}")
    tb["F4"] = "Choisissez une valeur dans chaque liste (« Tous » par défaut) : les cartes et les graphiques 1 à 6 se recalculent."
    tb["F4"].font = Font(italic=True, size=9, color="555555")

    # Listes des filtres (sur _calcul, colonnes T à V)
    annees = sorted({m[:4] for m in A["mois"]})
    periodes = ["Tous"] + annees + A["mois"]
    aeros = list(A["aeros"]["aerodrome"])
    cats = ["Tous"] + sorted({clair(v) for v in (df["categorie_vol"] if "categorie_vol" in df.columns else [NON_RENSEIGNE])})
    for j, (titre, valeurs) in enumerate((("Périodes", periodes), ("Aérodromes", ["Tous"] + aeros), ("Catégories", cats))):
        lettre = get_column_letter(20 + j)
        calc[f"{lettre}1"] = titre
        for i, v in enumerate(valeurs, start=2):
            calc[f"{lettre}{i}"] = v
        dv = DataValidation(type="list", formula1=f"=_calcul!${lettre}$2:${lettre}${len(valeurs) + 1}",
                            allow_blank=False, showDropDown=False)
        dv.error, dv.errorTitle = "Choisissez une valeur dans la liste.", "Filtre"
        tb.add_data_validation(dv)
        dv.add(f"C{4 + j}")

    # ---- _calcul : critères ----
    calc["A1"] = "Critères (tirés des filtres du tableau de bord)"
    calc["A2"], calc["B2"] = "Période", "=IF('Tableau de bord'!$C$4=\"Tous\",\"*\",IF(LEN('Tableau de bord'!$C$4)=4,'Tableau de bord'!$C$4&\"-*\",'Tableau de bord'!$C$4))"
    calc["A3"], calc["B3"] = "Aérodrome", "=IF('Tableau de bord'!$C$5=\"Tous\",\"*\",'Tableau de bord'!$C$5)"
    calc["A4"], calc["B4"] = "Catégorie", "=IF('Tableau de bord'!$C$6=\"Tous\",\"*\",'Tableau de bord'!$C$6)"
    calc["A5"], calc["B5"] = "Année de référence (dernier mois complet)", A["annee_ref"] or ""
    calc["A6"], calc["B6"] = "Année N affichée", "=IF($B$2=\"*\",$B$5,LEFT($B$2,4))"
    P, AE, CA = "_calcul!$B$2", "_calcul!$B$3", "_calcul!$B$4"

    def sumifs(somme, *extra):
        crit = [f"{MOI},{P}", f"{AER},{AE}", f"{CAT},{CA}"] + list(extra)
        return f"SUMIFS({somme},{','.join(crit)})"

    # ---- _calcul : table mensuelle (ligne 10 et suivantes) ----
    r0 = 10
    tete = ["Mois", "Mois N-1", "Complet", "Complet N-1", "Dans la période", "Mouvements", "Mouvements affichés",
            "Mouvements N-1", "Inclus dans la variation", "Complet dans la période", "Pointe / plus calme",
            "Retard pondéré", "N° du mois", "Libellé"]
    for j, t in enumerate(tete, start=1):
        calc.cell(row=r0 - 1, column=j, value=t).font = Font(bold=True)
    mois_t = A["complets"] or A["mois"][:1]
    r1 = r0 + len(mois_t) - 1
    for k, m in enumerate(mois_t):
        r = r0 + k
        m1 = core._decaler(m, -12)
        calc[f"A{r}"], calc[f"B{r}"] = m, m1
        calc[f"C{r}"] = "Oui" if A["statut"].get(m) == "Complet" else "Non"
        calc[f"D{r}"] = "Oui" if A["statut"].get(m1) == "Complet" else "Non"
        calc[f"E{r}"] = f'=IF(OR($B$2="*",$B$2=LEFT(A{r},4)&"-*",$B$2=A{r}),1,0)'
        calc[f"F{r}"] = f"=SUMIFS({MVT},{MOI},A{r},{AER},{AE},{CAT},{CA})"
        calc[f"G{r}"] = f"=IF(E{r}=1,F{r},-1)"  # hors période : sous l'axe (minimum 0), donc invisible
        calc[f"H{r}"] = f"=SUMIFS({MVT},{MOI},B{r},{AER},{AE},{CAT},{CA})"
        calc[f"I{r}"] = (f'=IF(AND(C{r}="Oui",D{r}="Oui",OR(AND($B$2="*",LEFT(A{r},4)=$B$5),'
                         f'AND($B$2<>"*",E{r}=1))),1,0)')
        calc[f"J{r}"] = f'=IF(AND(E{r}=1,C{r}="Oui"),F{r},"")'
        calc[f"K{r}"] = (f'=IF(J{r}="",-1,IF(OR(J{r}=MAX($J${r0}:$J${r1}),J{r}=MIN($J${r0}:$J${r1})),J{r},-1))')
        calc[f"L{r}"] = (f"=IF(E{r}=1,IFERROR(SUMIFS({RXM},{MOI},A{r},{AER},{AE},{CAT},{CA})/"
                         f"SUMIFS({MAR},{MOI},A{r},{AER},{AE},{CAT},{CA}),-1),-1)")
        calc[f"M{r}"] = int(m[5:])
        calc[f"N{r}"] = f"{MOIS_COURTS[int(m[5:]) - 1]} {m[2:4]}"
    I_, F_, H_, M_ = (f"_calcul!${c}${r0}:${c}${r1}" for c in "IFHM")

    # ---- _calcul : comparaison N / N-1 par mois calendaire (ligne rc) ----
    rc = r1 + 3
    calc[f"A{rc - 1}"], calc[f"B{rc - 1}"], calc[f"C{rc - 1}"] = "Mois", '="N-1 ("&($B$6-1)&")"', '="N ("&$B$6&")"'
    for k in range(12):
        r = rc + k
        calc[f"A{r}"] = MOIS_COURTS[k]
        calc[f"B{r}"] = f"=IF(SUMPRODUCT(({I_}=1)*({M_}={k + 1}))=0,-1,SUMPRODUCT(({I_}=1)*({M_}={k + 1})*{H_}))"
        calc[f"C{r}"] = f"=IF(SUMPRODUCT(({I_}=1)*({M_}={k + 1}))=0,-1,SUMPRODUCT(({I_}=1)*({M_}={k + 1})*{F_}))"

    # ---- _calcul : aérodromes, catégories, sens ----
    ra = rc + 14
    calc[f"A{ra - 1}"], calc[f"B{ra - 1}"] = "Aérodrome", "Mouvements"
    for k, a in enumerate(aeros):
        r = ra + k
        calc[f"A{r}"] = a
        calc[f"B{r}"] = f'=IF(OR($B$3="*",$B$3=A{r}),SUMIFS({MVT},{MOI},{P},{AER},A{r},{CAT},{CA}),0)'
    ra1 = ra + len(aeros) - 1
    rk = ra1 + 3
    calc[f"A{rk - 1}"], calc[f"B{rk - 1}"], calc[f"C{rk - 1}"] = "Catégorie", "Mouvements", "Légende (avec part)"
    for k, c in enumerate(cats[1:]):
        r = rk + k
        calc[f"A{r}"] = c
        calc[f"B{r}"] = f'=IF(OR($B$4="*",$B$4=A{r}),SUMIFS({MVT},{MOI},{P},{AER},{AE},{CAT},A{r}),0)'
    rk1 = rk + len(cats) - 2
    for k in range(len(cats) - 1):
        r = rk + k
        calc[f"C{r}"] = f'=A{r}&" ("&ROUND(IF(SUM($B${rk}:$B${rk1})=0,0,B{r}/SUM($B${rk}:$B${rk1})*100),1)&" %)"'
    rs = rk1 + 3
    calc[f"A{rs - 1}"], calc[f"B{rs - 1}"], calc[f"C{rs - 1}"] = "Sens", "Mouvements", "Légende (avec part)"
    for k, s in enumerate(("Arrivée", "Départ")):
        r = rs + k
        calc[f"A{r}"] = s
        calc[f"B{r}"] = "=" + sumifs(MVT, f"{TYP},A{r}")
        calc[f"C{r}"] = f'=A{r}&" ("&ROUND(IF(SUM($B${rs}:$B${rs + 1})=0,0,B{r}/SUM($B${rs}:$B${rs + 1})*100),1)&" %)"'

    # ---- _calcul : journées atypiques (série journalière concernée, statique) ----
    rj = rs + 4
    pics = A["pics"]
    calc[f"A{rj - 1}"] = "Journées atypiques : série journalière concernée"
    nj = 0
    if pics:
        p0 = pics[0]
        memes = [p for p in pics if (p["aerodrome"], p["type"]) == (p0["aerodrome"], p0["type"])]
        calc[f"A{rj}"], calc[f"B{rj}"] = "Date", f"{p0['aerodrome']} {clair(p0['type'])}"
        for k, p in enumerate(memes):  # colonnes D/E/F : nom du pic (« date : valeur »), date, valeur
            calc.cell(row=rj + 1 + k, column=4, value=f"{p['date']:%d/%m/%Y} : {n0(p['valeur'])}")
            calc.cell(row=rj + 1 + k, column=5, value=p["date"].to_pydatetime()).number_format = "dd/mm/yyyy"
            calc.cell(row=rj + 1 + k, column=6, value=p["valeur"])
        for i, (d, v) in enumerate(p0["serie"].items(), start=1):
            calc.cell(row=rj + i, column=1, value=pd.Timestamp(d).to_pydatetime()).number_format = "dd/mm/yyyy"
            calc.cell(row=rj + i, column=2, value=float(v))
        nj = len(p0["serie"])

    # ---- Cartes ----
    cartes = [("B", "Mouvements (vrai total)", "=" + sumifs(MVT), "#,##0"),
              ("E", "Retard moyen pondéré (min)", f'=IFERROR({sumifs(RXM)}/{sumifs(MAR)},"-")', "0.00"),
              ("H", "Variation N/N-1 (mêmes mois complets)",
               f'=IF(SUM({I_})=0,"N-1 indisponible",SUMPRODUCT({I_},{F_})/SUMPRODUCT({I_},{H_})-1)', "+0.00%;-0.00%"),
              ("K", "Aérodrome principal",
               f'=IF(MAX(_calcul!$B${ra}:$B${ra1})=0,"-",INDEX(_calcul!$A${ra}:$A${ra1},MATCH(MAX(_calcul!$B${ra}:$B${ra1}),_calcul!$B${ra}:$B${ra1},0)))', "@"),
              ("N", "Part des arrivées", f"=IFERROR(_calcul!$B${rs}/SUM(_calcul!$B${rs}:$B${rs + 1}),\"-\")", "0.00%")]
    for col, libelle, formule, fmt in cartes:
        c2 = get_column_letter(tb[f"{col}1"].column + 2)
        tb.merge_cells(f"{col}8:{c2}8")
        tb.merge_cells(f"{col}9:{c2}10")
        tb[f"{col}8"] = libelle
        tb[f"{col}8"].font = Font(bold=True, size=9, color="FFFFFF")
        tb[f"{col}8"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        tb[f"{col}9"] = formule
        tb[f"{col}9"].number_format = fmt
        tb[f"{col}9"].font = Font(bold=True, size=18, color=NUIT)
        tb[f"{col}9"].alignment = Alignment(horizontal="center", vertical="center")
        for r in (8, 9, 10):
            for c in range(tb[f"{col}1"].column, tb[f"{col}1"].column + 3):
                tb.cell(row=r, column=c).fill = PatternFill("solid", fgColor=SARCELLE if r == 8 else GRIS)
    tb.row_dimensions[8].height = 26
    # Ligne du mois incomplet (somme filtrée par aérodrome et catégorie)
    r = 12
    if A["incomplets"]:
        for m, _, j in A["incomplets"]:
            tb[f"B{r}"] = (f'="dont mois incomplet {m} : "&SUMIFS({MVT},{MOI},"{m}",{AER},{AE},{CAT},{CA})'
                           f'&" mouvements sur {j} jour(s), exclu de la variation"')
            tb[f"B{r}"].font = Font(italic=True, color=OCRE, bold=True)
            r += 1
    elif A["partiels_retires"]:
        tb[f"B{r}"] = "Mois partiel retiré de l'analyse : " + "; ".join(
            f"{m} ({n0(v)} mouvements sur {j} jour(s))" for m, j, v, _l in A["partiels_retires"])
        tb[f"B{r}"].font = Font(italic=True, color=OCRE, bold=True)
    else:
        tb[f"B{r}"] = "Aucun mois incomplet dans le fichier."
        tb[f"B{r}"].font = Font(italic=True, color="555555")

    # ---- Graphiques ----
    lignes_mois = Reference(calc, min_col=14, min_row=r0, max_row=r1)

    g1 = BarChart()
    _titre_chart(g1, "1. Évolution mensuelle des mouvements (mois complets)")
    s = Series(Reference(calc, min_col=7, min_row=r0, max_row=r1), title="Mouvements")
    _couleur_serie(s, NUIT, ligne=False)
    g1.series.append(s)
    g1.gapWidth = 60
    g1.set_categories(lignes_mois)
    g1.y_axis.title, g1.x_axis.title = "Mouvements", "Mois"
    g1.y_axis.number_format = "#,##0"
    g1.y_axis.scaling.min = 0
    points = LineChart()
    s2 = Series(Reference(calc, min_col=11, min_row=r0, max_row=r1), title="Mois de pointe / plus calme")
    _marqueurs_seuls(s2, PIC, 10)
    points.series.append(s2)
    points.y_axis.axId = g1.y_axis.axId
    points.y_axis.scaling.min = 0
    g1 += points

    g2 = BarChart()
    _titre_chart(g2, "2. Comparaison N-1 / N (mêmes mois complets)")
    for col, couleur in ((2, OCRE), (3, NUIT)):
        s = Series(Reference(calc, min_col=col, min_row=rc, max_row=rc + 11), title_from_data=False)
        s.tx = None
        s.tx = SeriesLabel(strRef=StrRef(f"_calcul!${get_column_letter(col)}${rc - 1}"))
        _couleur_serie(s, couleur, ligne=False)
        g2.series.append(s)
    g2.set_categories(Reference(calc, min_col=1, min_row=rc, max_row=rc + 11))
    g2.y_axis.title, g2.x_axis.title = "Mouvements", "Mois"
    g2.y_axis.number_format = "#,##0"
    g2.y_axis.scaling.min = 0

    g3 = BarChart()
    g3.type = "bar"
    _titre_chart(g3, "3. Mouvements par aérodrome")
    s = Series(Reference(calc, min_col=2, min_row=ra, max_row=ra1), title="Mouvements")
    _couleur_serie(s, SARCELLE, ligne=False)
    g3.series.append(s)
    g3.set_categories(Reference(calc, min_col=1, min_row=ra, max_row=ra1))
    g3.y_axis.title, g3.x_axis.title = "Mouvements", "Aérodrome"
    g3.y_axis.number_format = "#,##0"
    g3.legend = None

    def anneau(titre, r_deb, r_fin, couleurs):
        g = DoughnutChart(holeSize=55)
        _titre_chart(g, titre)
        s = Series(Reference(calc, min_col=2, min_row=r_deb, max_row=r_fin), title="Mouvements")
        for k in range(r_fin - r_deb + 1):
            pt = DataPoint(idx=k)
            pt.graphicalProperties = GraphicalProperties(solidFill=couleurs[k % len(couleurs)])
            s.dPt.append(pt)
        g.series.append(s)
        g.set_categories(Reference(calc, min_col=3, min_row=r_deb, max_row=r_fin))
        _legende(g, "r")
        return g

    g4 = anneau("4. Mouvements par catégorie de vol", rk, rk1, [NUIT, SARCELLE, OCRE, BLEU])

    g5 = BarChart()
    _titre_chart(g5, "5. Retard moyen pondéré par mois (mois complets)")
    s = Series(Reference(calc, min_col=12, min_row=r0, max_row=r1), title="Retard moyen (min)")
    _couleur_serie(s, OCRE, ligne=False)
    g5.series.append(s)
    g5.gapWidth = 60
    g5.set_categories(lignes_mois)
    g5.y_axis.title, g5.x_axis.title = "Minutes", "Mois"
    g5.y_axis.number_format = "0.00"
    g5.y_axis.scaling.min = 0
    g5.legend = None

    g6 = anneau("6. Arrivées et départs", rs, rs + 1, [SARCELLE, BLEU])

    g7 = ScatterChart()
    g7.style = 13
    if pics:
        _titre_chart(g7, f"7. Journées atypiques : {pics[0]['aerodrome']}, {clair(pics[0]['type']).lower()}s par jour")
        s = Series(Reference(calc, min_col=2, min_row=rj + 1, max_row=rj + nj),
                   Reference(calc, min_col=1, min_row=rj + 1, max_row=rj + nj), title="Mouvements par jour")
        _couleur_serie(s, BLEU)
        s.marker = Marker(symbol="none")
        s.smooth = False
        g7.series.append(s)
        n_memes = len([p for p in pics if (p["aerodrome"], p["type"]) == (pics[0]["aerodrome"], pics[0]["type"])])
        for k in range(n_memes):
            s = Series(Reference(calc, min_col=6, min_row=rj + 1 + k), Reference(calc, min_col=5, min_row=rj + 1 + k),
                       title=None)
            s.tx = SeriesLabel(strRef=StrRef(f"_calcul!$D${rj + 1 + k}"))
            _marqueurs_seuls(s, PIC, 10)
            s.dLbls = DataLabelList()
            s.dLbls.showSerName, s.dLbls.showVal, s.dLbls.showCatName, s.dLbls.showLegendKey = True, False, False, False
            g7.series.append(s)
        g7.x_axis.number_format = "mm/yy"
        g7.x_axis.scaling.min = float((pics[0]["serie"].index.min() - pd.Timestamp("1899-12-30")).days)
        g7.x_axis.scaling.max = float((pics[0]["serie"].index.max() - pd.Timestamp("1899-12-30")).days)
        g7.x_axis.majorUnit = 61
    else:
        _titre_chart(g7, "7. Journées atypiques : aucune journée atypique (|score| > 6)")
    g7.y_axis.title, g7.x_axis.title = "Mouvements", "Date"
    g7.y_axis.number_format = "#,##0"
    g7.y_axis.scaling.min = 0

    places = [(g1, "B14"), (g2, "J14"), (g3, "B32"), (g4, "J32"), (g5, "B50"), (g6, "J50"), (g7, "B68")]
    for g, ancre in places:
        for axe in (getattr(g, "x_axis", None), getattr(g, "y_axis", None)):
            if axe is not None and not isinstance(g, DoughnutChart):
                _police_axes(axe)
        if g.legend is not None and not isinstance(g, DoughnutChart):
            _legende(g, "b")
        g.width, g.height = (31.5 if ancre == "B68" else 15.5), 8.6
        tb.add_chart(g, ancre)
    tb["B86"] = MENTION
    tb["B86"].font = Font(italic=True, color="555555")
    tb["B87"] = ("Graphiques 1 à 6 et cartes : recalculés par formules selon les filtres. Graphique 7 : série "
                 "journalière concernée par la journée atypique, sur tout le fichier. Aucune cause n'est avancée : "
                 "à vérifier par l'expert métier.")
    tb["B87"].font = Font(italic=True, size=9, color="555555")
    tb.page_setup.orientation = "landscape"
    tb.page_setup.paperSize = tb.PAPERSIZE_A4
    tb.sheet_properties.pageSetUpPr.fitToPage = True
    tb.page_setup.fitToWidth, tb.page_setup.fitToHeight = 1, 0
    calc.sheet_state = "hidden"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _statut_validation(validation):
    if validation:
        qui = f" par {validation['nom']}" if validation.get("nom") else ""
        return f"Validé{qui} le {validation['horodatage_utc']} UTC"
    return "Non validé"


# ===========================================================================
# PDF
# ===========================================================================
def _fig_png(fig):
    b = io.BytesIO()
    fig.savefig(b, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    b.seek(0)
    return b


def _style_ax(ax, titre, xl=None, yl=None):
    ax.set_title(titre, fontsize=9, color="#" + NUIT, loc="left", fontweight="bold")
    if xl:
        ax.set_xlabel(xl, fontsize=9)
    if yl:
        ax.set_ylabel(yl, fontsize=9)
    ax.tick_params(labelsize=9)
    for cote in ("top", "right"):
        ax.spines[cote].set_visible(False)
    ax.grid(axis="y", alpha=0.25)


def _milliers(ax, axe="y"):
    f = matplotlib.ticker.FuncFormatter(lambda v, _: n0(v))
    (ax.yaxis if axe == "y" else ax.xaxis).set_major_formatter(f)


def graphiques_pdf(A, taille=(3.7, 2.3)):
    """Les 7 graphiques du tableau de bord (fichier complet), pics marqués en rouge."""
    imgs = []
    c = {k: "#" + v for k, v in (("nuit", NUIT), ("sar", SARCELLE), ("bleu", BLEU), ("ocre", OCRE), ("pic", PIC))}
    mens = A["mensuel"]
    comp = mens[A["complets"]] if A["complets"] else mens
    x = list(range(len(comp)))
    lab = [f"{MOIS_COURTS[int(m[5:]) - 1]}\n{m[2:4]}" for m in comp.index]
    fig, ax = plt.subplots(figsize=taille)
    ax.bar(x, comp.values, color=c["nuit"], width=0.65)
    for m, nom_pt in ((A["pointe"], "pointe"), (A["calme"], "plus calme")):
        if m:
            k = list(comp.index).index(m)
            ax.scatter([k], [comp[m]], color=c["pic"], zorder=3, s=24)
            haut = comp.max() * (1.12 if nom_pt == "pointe" else 1.25)
            ax.annotate(f"{nom_pt} : {n0(comp[m])}", (k, comp[m]), xytext=(k, haut), fontsize=7, color=c["pic"],
                        ha="center", va="bottom", arrowprops={"arrowstyle": "-", "color": c["pic"], "lw": 0.6},
                        bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none"})
    ax.set_ylim(0, comp.max() * 1.42)
    ax.set_xticks(x[::3], lab[::3])
    _style_ax(ax, "1. Évolution mensuelle (mois complets)", "Mois", "Mouvements")
    _milliers(ax)
    imgs.append(_fig_png(fig))

    fig, ax = plt.subplots(figsize=taille)
    if A["mois_variation"]:
        mv = A["mois_variation"]
        xs = range(len(mv))
        ax.bar([i - 0.2 for i in xs], [mens[core._decaler(m, -12)] for m in mv], 0.4, color=c["ocre"],
               label=f"N-1 ({int(A['annee_ref']) - 1})")
        ax.bar([i + 0.2 for i in xs], [mens[m] for m in mv], 0.4, color=c["nuit"], label=f"N ({A['annee_ref']})")
        ax.set_xticks(list(xs), [MOIS_COURTS[int(m[5:]) - 1] for m in mv])
        ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=2, frameon=False)
    else:
        ax.text(0.5, 0.5, "N-1 indisponible", ha="center", va="center", transform=ax.transAxes)
    _style_ax(ax, "2. Comparaison N-1 / N (mêmes mois complets)", None, "Mouvements")
    _milliers(ax)
    imgs.append(_fig_png(fig))

    fig, ax = plt.subplots(figsize=taille)
    ae = A["aeros"].sort_values("total")
    ax.barh(ae["aerodrome"], ae["total"], color=c["sar"])
    _style_ax(ax, "3. Mouvements par aérodrome", "Mouvements", None)
    _milliers(ax, "x")
    imgs.append(_fig_png(fig))

    def anneau(serie, titre, couleurs):
        fig, ax = plt.subplots(figsize=taille)
        tot = serie.sum()
        ax.pie(serie.values, colors=couleurs[:len(serie)], startangle=90, counterclock=False,
               wedgeprops={"width": 0.42, "edgecolor": "white"})
        ax.legend([f"{k} ({n1(v / tot * 100)} %)" for k, v in serie.items()], fontsize=7, loc="center left",
                  bbox_to_anchor=(1.0, 0.5), frameon=False)
        ax.set_title(titre, fontsize=9, color=c["nuit"], loc="left", fontweight="bold")
        return _fig_png(fig)

    pal = [c["nuit"], c["sar"], c["ocre"], c["bleu"], "#7A8BA8"]
    imgs.append(anneau(A["cat_totaux"], "4. Mouvements par catégorie de vol", pal) if len(A["cat_totaux"]) else None)

    fig, ax = plt.subplots(figsize=taille)
    rm = A["retard_mensuel"].reindex(A["complets"]).dropna()
    ax.bar(range(len(rm)), rm.values, color=c["ocre"], width=0.65)
    ax.set_ylim(0, rm.max() * 1.15 if len(rm) else 1)
    ax.set_xticks(list(range(len(rm)))[::3], [f"{MOIS_COURTS[int(m[5:]) - 1]}\n{m[2:4]}" for m in rm.index][::3])
    _style_ax(ax, "5. Retard moyen pondéré par mois (mois complets)", "Mois", "Minutes")
    imgs.append(_fig_png(fig))

    imgs.append(anneau(A["sens"].reindex(["Arrivée", "Départ"]).fillna(0), "6. Arrivées et départs", [c["sar"], c["bleu"]]))

    fig, ax = plt.subplots(figsize=(taille[0] * 2 + 0.4, taille[1]))
    if A["pics"]:
        p0 = A["pics"][0]
        s = p0["serie"]
        ax.plot(s.index, s.values, color=c["bleu"], lw=0.9)
        for p in A["pics"]:
            if (p["aerodrome"], p["type"]) == (p0["aerodrome"], p0["type"]):
                ax.scatter([p["date"]], [p["valeur"]], color=c["pic"], zorder=3, s=26)
                ax.annotate(f"{p['date']:%d/%m/%Y} : {n0(p['valeur'])}", (p["date"], p["valeur"]), fontsize=8,
                            color=c["pic"], xytext=(8, -4), textcoords="offset points")
        titre = f"7. Journées atypiques : {p0['aerodrome']}, {clair(p0['type']).lower()}s par jour (pic en rouge)"
    else:
        ax.plot(range(len(mens)), mens.values, color=c["bleu"])
        titre = "7. Journées atypiques : aucune (|score| > 6)"
    _style_ax(ax, titre, "Date", "Mouvements")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m/%y"))
    imgs.append(_fig_png(fig))
    return imgs


def _graphique_pic(p):
    fig, ax = plt.subplots(figsize=(5.6, 1.7))
    s = p["serie"]
    fen = s[(s.index >= p["date"] - pd.Timedelta(days=20)) & (s.index <= p["date"] + pd.Timedelta(days=20))]
    ax.plot(fen.index, fen.values, color="#" + BLEU, lw=1.2, marker="o", ms=2)
    ax.axhline(p["mediane"], color="#" + SARCELLE, lw=0.8, ls="--")
    ax.text(fen.index[0], p["mediane"], f" médiane {n0(p['mediane'])}", fontsize=7, color="#" + SARCELLE, va="bottom")
    ax.scatter([p["date"]], [p["valeur"]], color="#" + PIC, zorder=3, s=26)
    ax.annotate(f"{p['date']:%d/%m/%Y} : {n0(p['valeur'])}", (p["date"], p["valeur"]), fontsize=8, color="#" + PIC,
                xytext=(8, -6), textcoords="offset points")
    _style_ax(ax, f"{p['aerodrome']}, {clair(p['type']).lower()}s, 20 jours avant et après", "Date", "Mouvements")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d/%m"))
    return _fig_png(fig)


def construire_pdf(A, registre=None, validation=None):
    """PDF Rapport_AMT (octets). Lève une erreur plutôt que de livrer un PDF partiel."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from xml.sax.saxutils import escape

    st_ = getSampleStyleSheet()
    nuit = colors.HexColor("#" + NUIT)
    S = {"titre": ParagraphStyle("t", parent=st_["Title"], textColor=colors.white, fontSize=15, alignment=TA_LEFT,
                                 leading=18, spaceAfter=0),
         "bandeau": ParagraphStyle("b", parent=st_["Normal"], textColor=colors.white, fontSize=8, leading=10),
         "h1": ParagraphStyle("h1", parent=st_["Heading1"], textColor=nuit, fontSize=14, spaceBefore=4, spaceAfter=4),
         "h2": ParagraphStyle("h2", parent=st_["Heading2"], textColor=nuit, fontSize=11.5, spaceBefore=8, spaceAfter=3),
         "temps": ParagraphStyle("tp", parent=st_["Normal"], fontSize=9, leading=12, leftIndent=8, spaceAfter=1.5),
         "p": ParagraphStyle("p", parent=st_["Normal"], fontSize=9, leading=12),
         "petit": ParagraphStyle("s", parent=st_["Normal"], fontSize=7.5, leading=9.5, textColor=colors.HexColor("#555555")),
         "carte_l": ParagraphStyle("cl", parent=st_["Normal"], fontSize=7.5, leading=9, textColor=colors.white,
                                   alignment=1),
         "carte_v": ParagraphStyle("cv", parent=st_["Normal"], fontSize=13, leading=15, textColor=nuit, alignment=1,
                                   fontName="Helvetica-Bold"),
         "cell": ParagraphStyle("c", parent=st_["Normal"], fontSize=7.5, leading=9)}

    def P(t, s="p"):
        return Paragraph(t, S[s])

    def bandeau(titre, sous_titre):
        t = Table([[P(escape(titre), "titre")], [P(escape(sous_titre), "bandeau")]], colWidths=[18 * cm])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), nuit), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        return t

    def tableau(lignes, largeurs, entete=True):
        data = [[P(escape(str(v)), "cell") for v in l] for l in lignes]
        t = Table(data, colWidths=[w * cm for w in largeurs], repeatRows=1 if entete else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C9D1DC")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        if entete:
            style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + GRIS))]
        t.setStyle(TableStyle(style))
        return t

    def section(num, titre, chiffre, lire, montre, verifier, extra=None):
        bloc = [P(f"{num}. {escape(titre)}", "h2"),
                P(f"<b>Le chiffre.</b> {chiffre}", "temps"), P(f"<b>Comment le lire.</b> {lire}", "temps"),
                P(f"<b>Ce qu'il montre.</b> {montre}", "temps"), P(f"<b>Ce qu'il faut vérifier.</b> {verifier}", "temps")]
        return [KeepTogether(bloc)] + (extra or [])

    periode = f"du {A['date_min']:%d/%m/%Y} au {A['date_max']:%d/%m/%Y}"
    var_txt = pct(A["variation"]) if A["variation"] is not None else "N-1 indisponible"
    mv = A["mois_variation"]
    el = [bandeau("AMT - Rapport d'analyse du trafic aérien",
                  f"{MENTION} · Fichier : {A['fichier']} · {periode} · {_statut_validation(validation)}"),
          Spacer(1, 0.25 * cm)]
    # ---- Page 1 : tableau de bord ----
    cartes = [("Mouvements (vrai total)", n0(A["total"])),
              ("Retard moyen pondéré", f"{n2(A['retard'])} min" if A["retard"] is not None else "-"),
              ("Variation N/N-1", var_txt), ("Aérodrome principal", A["principal"] or "-"),
              ("Part des arrivées", pct(A["part_arrivees"], 2, False))]
    t = Table([[P(escape(l), "carte_l") for l, _ in cartes], [P(escape(v), "carte_v") for _, v in cartes]],
              colWidths=[3.6 * cm] * 5)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + SARCELLE)),
                           ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#" + GRIS)),
                           ("LINEAFTER", (0, 0), (-2, -1), 3, colors.white), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 1), (-1, 1), 6), ("BOTTOMPADDING", (0, 1), (-1, 1), 6)]))
    el.append(t)
    for m, v, j in A["incomplets"]:
        el.append(P(f"<font color='#{OCRE}'><b>dont mois incomplet {m} : {n0(v)} mouvements sur {j} jour(s), "
                    f"exclu de la variation</b></font>", "p"))
    for m, j, v, _l in A["partiels_retires"]:
        el.append(P(f"<font color='#{OCRE}'><b>Mois partiel retiré de l'analyse : {m} ({n0(v)} mouvements sur "
                    f"{j} jour(s))</b></font>", "p"))
    if mv:
        el.append(P(f"Variation calculée sur {lm(mv[0])} à {lm(mv[-1])} contre les mêmes mois de "
                    f"{int(A['annee_ref']) - 1}. Fichier complet, sans filtre.", "petit"))
    el.append(Spacer(1, 0.2 * cm))
    imgs = graphiques_pdf(A)
    w, h = 8.9 * cm, 5.25 * cm

    def img(b, ww=w, hh=h):
        return Image(b, width=ww, height=hh) if b is not None else Spacer(ww, hh)
    grille = Table([[img(imgs[0]), img(imgs[1])], [img(imgs[2]), img(imgs[3])], [img(imgs[4]), img(imgs[5])],
                    [img(imgs[6], 18 * cm, 5.0 * cm), ""]], colWidths=[9 * cm, 9 * cm])
    grille.setStyle(TableStyle([("SPAN", (0, 3), (1, 3)), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    el += [grille, P(f"{MENTION}. Pics en rouge. Aucune cause n'est avancée : à vérifier par l'expert métier.", "petit"),
           PageBreak()]

    # ---- Analyse ----
    el.append(P("Analyse", "h1"))
    mens = A["mensuel"]
    ae = A["aeros"]
    q = A["qualite"]
    # 1. Synthèse en 5 phrases
    p1 = f"Le fichier couvre la période {periode} et compte {n0(A['total'])} mouvements après nettoyage."
    p2 = (f"Le mois de pointe est {lm(A['pointe'])} ({n0(mens[A['pointe']])}) et le plus calme {lm(A['calme'])} "
          f"({n0(mens[A['calme']])}), sur les mois complets." if A["pointe"] else "Aucun mois complet : pas de mois de pointe.")
    p3 = (f"Sur {lm(mv[0])} à {lm(mv[-1])}, le trafic varie de {var_txt} par rapport aux mêmes mois de "
          f"{int(A['annee_ref']) - 1}." if mv else "La comparaison avec l'année précédente n'est pas possible (N-1 indisponible).")
    p4 = (f"{A['principal']} est l'aérodrome principal avec {pct(ae.iloc[0]['part'], 1, False)} des mouvements ; "
          f"le retard moyen pondéré est de {n2(A['retard'])} minutes." if A["retard"] is not None else
          f"{A['principal']} est l'aérodrome principal avec {pct(ae.iloc[0]['part'], 1, False)} des mouvements.")
    p5 = (f"{len(A['pics'])} journée(s) atypique(s) reste(nt) à valider par l'expert ; leur cause est inconnue."
          if A["pics"] else "Aucune journée atypique n'est détectée (|score| > 6).")
    el.append(P("1. Synthèse", "h2"))
    el.append(P(" ".join([p1, p2, p3, p4, p5]), "temps"))
    # 2. Volume
    inc = "; ".join(f"{lm(m)} : {n0(v)} mouvements sur {j} jour(s)" for m, v, j in A["incomplets"]) or "aucun"
    ret = "; ".join(f"{lm(m)} ({n0(v)} mouvements sur {j} jour(s))" for m, j, v, _l in A["partiels_retires"])
    el += section(2, "Volume", f"{n0(A['total'])} mouvements au total, soit {n1(A['moyenne_jour'])} par jour en "
                  f"moyenne sur {A['jours']} jours renseignés. "
                  + (f"Mois partiel retiré de l'analyse : {ret}." if ret else f"Mois incomplet : {inc}."),
                  "Le vrai total additionne toutes les lignes valides, mois incomplets compris. La moyenne par jour "
                  "divise ce total par le nombre de jours qui ont des données.",
                  f"Le volume est compté sur {n0(A['lignes'])} lignes. Un mois incomplet compte dans le total "
                  "mais pas dans les tendances ni dans la variation.",
                  "Un jour absent n'est pas un zéro : vérifiez les jours sans ligne signalés en section 9.")
    # 3. Évolution mensuelle
    if A["complets"]:
        comp = mens[A["complets"]]
        remontees = [m for i, m in enumerate(A["complets"][1:], 1) if comp[m] > comp[A["complets"][i - 1]]]
        el += section(3, "Évolution mensuelle et saisonnalité",
                      f"Pointe : {lm(A['pointe'])} ({n0(mens[A['pointe']])}). Plus calme : {lm(A['calme'])} "
                      f"({n0(mens[A['calme']])}). Moyenne mensuelle : {n0(A['moyenne_mensuelle'])}.",
                      f"Seuls les {len(A['complets'])} mois complets ({lm(A['complets'][0])} à {lm(A['complets'][-1])}) "
                      "servent à décrire la tendance.",
                      escape(core.commentaire_tendance(A["df"], A["mois_info"])) +
                      (f" Le total remonte d'un mois sur l'autre en {len(remontees)} occasion(s) : "
                       f"{', '.join(lm(m) for m in remontees[:8])}{'…' if len(remontees) > 8 else ''}." if remontees else ""),
                      "Une saison se lit sur plusieurs années : avec moins de deux ans de données, elle reste à confirmer.")
    # 4. Comparaison N/N-1
    if mv:
        lignes = [["Aérodrome", f"N-1 ({int(A['annee_ref']) - 1})", f"N ({A['annee_ref']})", "Variation"]]
        for r in ae.itertuples():
            lignes.append([r.aerodrome, n0(r.n1), n0(r.n), pct(r.variation) if r.variation is not None else "N-1 indisponible"])
        lignes.append(["Total", n0(A["n1"]), n0(A["n"]), var_txt])
        el += section(4, "Comparaison N / N-1",
                      f"{var_txt} : {n0(A['n'])} mouvements de {lm(mv[0])} à {lm(mv[-1])} contre {n0(A['n1'])} sur "
                      f"les mêmes mois de {int(A['annee_ref']) - 1}.",
                      "On compare les mêmes mois complets d'une année sur l'autre : un mois incomplet est exclu.",
                      "Un écart de quelques dixièmes de pour cent est peu significatif." if abs(A["variation"] or 0) < 0.01
                      else "L'écart dépasse 1 % : il mérite d'être regardé par aérodrome (tableau ci-dessous).",
                      "Vérifiez que les journées atypiques ne pèsent pas sur l'écart (section 8).",
                      [tableau(lignes, [4, 4, 4, 4]), Spacer(1, 0.15 * cm)])
    else:
        el += section(4, "Comparaison N / N-1", "N-1 indisponible.",
                      "Il faut les mêmes mois complets sur deux années consécutives.", "Aucune comparaison possible.",
                      "Ajoutez une année de données si la comparaison est utile.")
    # 5. Aérodromes
    lignes = [["Rang", "Aérodrome", "Nom", "Mouvements", "Part", "Variation N/N-1", "Retard pondéré (min)"]]
    for r in ae.itertuples():
        var_a = pct(r.variation) if mv and r.variation is not None else "N-1 indisponible"
        lignes.append([r.rang, r.aerodrome, r.nom or "-", n0(r.total), pct(r.part, 1, False), var_a,
                       n2(r.retard) if r.retard is not None else "-"])
    el += section(5, "Aérodromes",
                  f"{A['principal']} est premier avec {n0(ae.iloc[0]['total'])} mouvements "
                  f"({pct(ae.iloc[0]['part'], 1, False)}).",
                  "La part rapporte le total de chaque aérodrome au total du fichier ; le rang classe les totaux.",
                  "Le tableau ci-dessous donne pour chacun le volume, l'évolution sur les mêmes mois complets et le retard.",
                  "Les noms viennent du référentiel des aérodromes, quand il est fourni.",
                  [tableau(lignes, [1.1, 2.2, 4.4, 2.4, 1.6, 3, 3.3]), Spacer(1, 0.15 * cm)])
    # 6. Catégories, arrivées et départs
    cat = ", ".join(f"{k} {n0(v)} ({pct(v / A['total'], 1, False)})" for k, v in A["cat_totaux"].items()) or "non renseigné"
    ori = ", ".join(f"{k} {n0(v)} ({pct(v / A['total'], 1, False)})" for k, v in A["origine_totaux"].items())
    sens = ", ".join(f"{k} {n0(v)} ({pct(v / A['total'], 1, False)})" for k, v in A["sens"].items())
    el += section(6, "Catégories, arrivées et départs", f"Catégories : {cat}. Sens : {sens}." +
                  (f" Origine / destination : {ori}." if ori else ""),
                  "Chaque part rapporte les mouvements de la catégorie au total du fichier.",
                  f"Les arrivées représentent {pct(A['part_arrivees'], 2, False)} des mouvements.",
                  "Un écart entre arrivées et départs n'est pas forcément anormal : il dépend des lignes saisies.")
    # 7. Retards
    if A["retard"] is not None:
        rm = A["retard_mensuel"].reindex(A["complets"]).dropna()  # mois complets seulement
        ecart = rm.max() - rm.min()
        el += section(7, "Retards", f"Retard moyen pondéré : {n2(A['retard'])} minutes. Mois le plus élevé : "
                      f"{lm(rm.idxmax())} ({n2(rm.max())} min) ; le plus bas : {lm(rm.idxmin())} ({n2(rm.min())} min), "
                      "sur les mois complets.",
                      "Chaque retard est pondéré par le nombre de mouvements de sa ligne ; un retard vide est exclu.",
                      f"L'écart entre le mois le plus haut et le plus bas est de {n2(ecart)} minute(s)" +
                      (" : il est faible et peu significatif." if ecart < 1 else "."),
                      f"{len(q['retard_vide'])} ligne(s) sans retard ont été exclues des moyennes.")
    # 8. Pics et anomalies
    el.append(P("8. Pics et anomalies", "h2"))
    el.append(P("Un pic est une journée atypique (|score| > 6) ou le mois de pointe / le plus calme. Pour chaque pic, "
                "seul le calcul explique pourquoi c'est un pic. La cause est inconnue, à vérifier par l'expert.", "temps"))
    for k, p in enumerate(A["pics"], start=1):
        e = explication_pic(p)
        el += section(f"8.{k}", e["titre"], e["chiffre"], e["lire"], e["montre"], e["verifier"],
                      [Image(_graphique_pic(p), width=13 * cm, height=3.9 * cm), Spacer(1, 0.1 * cm)])
    for k, e in enumerate(explications_mois(A), start=len(A["pics"]) + 1):
        el += section(f"8.{k}", e["titre"], e["chiffre"], e["lire"], e["montre"], e["verifier"])
    # 9. Qualité des données
    nd, nv, nn = len(q["doublons"]), len(q["vides"]), len(q["negatifs"])
    s_dbl = float(pd.to_numeric(q["doublons"]["valeur"], errors="coerce").sum())
    s_neg = float(pd.to_numeric(q["negatifs"]["valeur"], errors="coerce").sum())
    recon = ""
    if q["agregats"]:
        g = q["agregats"]
        hors = "; ".join(f"{lm(mo)} ({n0(v)})" for mo, v in g["mois_hors"])
        recon = (f" Réconciliation avec les agrégats bruts des organisateurs (ils excluent les valeurs vides et "
                 f"négatives et gardent les doublons) : {n0(g['total'])} contre {n0(g['nettoye_perimetre'])} nettoyés sur "
                 f"les mêmes mois et aérodromes, soit un écart de {n0(g['ecart'])}, dont {n0(g['doublons'])} dus aux "
                 f"doublons retirés ; écart non expliqué : {n0(g['non_explique'])}." +
                 (f" Mois absents des agrégats : {hors}." if hors else ""))
    el += section(9, "Qualité des données",
                  f"{n0(A['meta']['lignes_brutes'])} lignes lues, {n0(A['lignes'])} retenues : {nd} doublon(s), "
                  f"{nv} valeur(s) vide(s), {nn} valeur(s) négative(s) écartés. Total brut du fichier "
                  f"(toutes lignes, doublons et négatifs compris) : {n0(A['meta']['total_brut'])} ; total nettoyé : "
                  f"{n0(A['total'])} ; écart : {n0(q['ecart_brut'])}, dû aux doublons retirés ({n0(s_dbl)}) et aux "
                  f"valeurs négatives exclues ({n0(s_neg)}) ; une valeur vide ne compte dans aucun des deux totaux.",
                  "Le total brut n'est jamais présenté comme correct : il sert seulement à mesurer l'effet du nettoyage.",
                  f"Les lignes écartées ne sont jamais remplacées ni estimées.{recon}",
                  f"{len(q['jours_absents'])} période(s) sans ligne et {len(q['consignes'])} texte(s) ressemblant à une "
                  "consigne (ignorés) sont signalés au journal de nettoyage.")
    j = A["journal"]
    j_aff = j[j["probleme"].isin(["Doublon exact", "Valeur vide", "Valeur négative", "Retard vide", "Anomalie statistique"])
              | j["probleme"].astype(str).str.startswith("Texte")]
    if len(j_aff):
        lignes = [["Date", "Aérodrome", "Problème", "Traitement"]] + [
            [r.date or "-", r.aerodrome or "-", r.probleme, r.traitement] for r in j_aff.itertuples()]
        el += [tableau(lignes, [2.2, 2.2, 5, 8.6]), Spacer(1, 0.15 * cm)]
    # 10. Méthode et glossaire
    el.append(P("10. Méthode et glossaire", "h2"))
    for terme, d in (("Nettoyage", "codes normalisés ; doublons exacts retirés (première ligne gardée) ; valeurs vides "
                      "ou négatives exclues, jamais remplacées."),
                     ("Vrai total", "somme des mouvements valides, mois incomplets compris."),
                     ("Mois complet", "au moins 28 jours renseignés ; un mois partiel est retiré de toute l'analyse."),
                     ("Variation N/N-1", "même mois complets de deux années consécutives : (N ÷ N-1) − 1."),
                     ("Retard pondéré", "somme (retard × mouvements) ÷ somme des mouvements des lignes avec retard."),
                     ("Score d'anomalie", "(valeur − médiane) ÷ (1,4826 × MAD), par aérodrome et sens. "
                      "MAD : écart médian à la médiane. Seuil : 6."),
                     ("Médiane", "valeur du milieu quand on range les journées de la plus petite à la plus grande.")):
        el.append(P(f"<b>{terme}.</b> {d}", "temps"))
    # Dernière page : registre de validation
    el += [PageBreak(), P("Registre de validation", "h1")]
    reg = registre if registre is not None else pd.DataFrame()
    if reg.empty:
        el.append(P("Aucune décision enregistrée.", "p"))
    else:
        lignes = [["Date et heure", "Élément", "Décision", "Nom", "Commentaire"]]
        for r in reg.itertuples():
            quand = getattr(r, "horodatage_utc", None)
            quand = f"{quand} UTC" if isinstance(quand, str) and quand else r.horodatage
            nom = getattr(r, "nom", "")
            lignes.append([quand, r.element, r.decision, nom if isinstance(nom, str) else "",
                           r.commentaire if isinstance(r.commentaire, str) else ""])
        el.append(tableau(lignes, [3.4, 5.6, 2, 2.6, 4.4]))
    el += [Spacer(1, 0.3 * cm), P("Une décision ne modifie jamais les données d'origine. " + MENTION + ".", "petit")]

    def pied(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(1.5 * cm, 1 * cm, f"{MENTION} · {A['fichier']} · empreinte {A['empreinte']}")
        canvas.drawRightString(19.5 * cm, 1 * cm, f"Page {doc.page}")
        canvas.restoreState()

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.2 * cm,
                            bottomMargin=1.5 * cm, title="Rapport AMT", author="AMT")
    doc.build(el, onFirstPage=pied, onLaterPages=pied)
    return out.getvalue()


# ===========================================================================
# Relecture avant livraison
# ===========================================================================
def _recalcul_libreoffice(xlsx):
    """Valeurs des cartes recalculées par LibreOffice (formules réellement évaluées), ou None si indisponible."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    from openpyxl import load_workbook
    with tempfile.TemporaryDirectory() as tmp:
        entree = os.path.join(tmp, "rapport.xlsx")
        with open(entree, "wb") as fh:
            fh.write(xlsx)
        sortie = os.path.join(tmp, "out")
        os.makedirs(sortie)
        profil = "file://" + os.path.join(tmp, "profil")
        try:
            subprocess.run([soffice, f"-env:UserInstallation={profil}", "--headless", "--calc", "--convert-to", "xlsx",
                            "--outdir", sortie, entree], capture_output=True, timeout=120, check=True)
            wb = load_workbook(os.path.join(sortie, "rapport.xlsx"), data_only=True)
        except Exception:
            return None
        tb = wb["Tableau de bord"]
        return {"mouvements": tb["B9"].value, "retard": tb["E9"].value, "variation": tb["H9"].value,
                "principal": tb["K9"].value, "arrivees": tb["N9"].value, "incomplet": tb["B12"].value}


def relire(A, xlsx, pdf):
    """Contrôles avant livraison : [(contrôle, réussi, détail)]."""
    controles = []
    df = A["df"]
    total = float(df["nombre_mouvements"].sum())
    controles.append(("Total recalculé en Python", abs(total - A["total"]) < 1e-6 and abs(total - A["meta"]["total_nettoye"]) < 1e-6,
                      f"{n0(total)} mouvements"))
    for m in [x for x in (A["complets"][:1] + [A["pointe"]]) if x][:2]:
        v = float(df.loc[df["mois"] == m, "nombre_mouvements"].sum())
        controles.append((f"Mois recalculé : {lm(m)}", abs(v - A["mensuel"][m]) < 1e-6, f"{n0(v)} mouvements"))
    with zipfile.ZipFile(io.BytesIO(xlsx)) as z:
        graphiques = [n for n in z.namelist() if n.startswith("xl/charts/chart")]
        anneaux = [n for n in graphiques if b"doughnutChart" in z.read(n)]
        dlblpos = [n for n in anneaux if b"dLblPos" in z.read(n)]
        textes = [n for n in z.namelist() if n.startswith("xl/worksheets/") and re.search(rb"TEXT\(", z.read(n))]
    controles.append(("Archive .xlsx : aucun dLblPos dans un anneau", not dlblpos,
                      f"{len(anneaux)} anneau(x) contrôlé(s), {len(graphiques)} graphique(s)"))
    controles.append(("Archive .xlsx : aucune fonction TEXT(", not textes, "feuilles contrôlées"))
    lo = _recalcul_libreoffice(xlsx)
    if lo is None:
        controles.append(("Formules recalculées (LibreOffice)", None, "LibreOffice indisponible : non vérifié"))
    else:
        ok_t = isinstance(lo["mouvements"], (int, float)) and abs(lo["mouvements"] - A["total"]) < 0.5
        controles.append(("Carte « Mouvements » recalculée = total Python", ok_t, f"{lo['mouvements']}"))
        if A["variation"] is not None:
            ok_v = isinstance(lo["variation"], (int, float)) and abs(lo["variation"] - A["variation"]) < 1e-6
            controles.append(("Carte « Variation N/N-1 » recalculée = Python", ok_v,
                              f"{pct(lo['variation']) if isinstance(lo['variation'], (int, float)) else lo['variation']}"))
        if A["retard"] is not None:
            ok_r = isinstance(lo["retard"], (int, float)) and abs(lo["retard"] - A["retard"]) < 1e-6
            controles.append(("Carte « Retard moyen pondéré » recalculée = Python", ok_r, f"{lo['retard']}"))
        controles.append(("Carte « Aérodrome principal » recalculée = Python", lo["principal"] == A["principal"],
                          f"{lo['principal']}"))
    controles.append(("PDF complet", pdf.startswith(b"%PDF") and b"%%EOF" in pdf[-1024:],
                      f"{len(pdf) // 1024} Ko"))
    controles.append(("Aspect visuel (chevauchements, mots coupés)", None, "non vérifiable automatiquement"))
    return controles
