"""Moteur de nettoyage et de calcul de l'AMT Agent (version Streamlit).
Règles identiques à celles de l'agent Copilot Studio."""
import csv
import io
import re
import xml.etree.ElementTree as ET

import pandas as pd

COLS_OBLIGATOIRES = ["date", "aerodrome", "type_mouvement", "nombre_mouvements"]
MOTIFS_CONSIGNE = re.compile(
    r"(?:ignore|consigne|instruction|assistant|prompt|ajoute|affiche|r[eè]gles pr[eé]c[eé]dentes)", re.I)
SEUIL_ANOMALIE = 6.0
JOURS_MOIS_COMPLET = 28


FORMATS = ["csv", "xlsx", "xls", "xml", "pdf"]


def _vers_csv(lignes):
    """Tableau de textes (en-tête + lignes) -> CSV en mémoire, relu ensuite exactement comme un fichier CSV :
    un fichier XML ou PDF donne donc les mêmes valeurs (vides, nombres, textes) que le CSV équivalent."""
    out = io.StringIO()
    csv.writer(out).writerows(lignes)
    return out.getvalue().encode("utf-8")


def _normaliser_entete(c):
    return re.sub(r"\s+", "_", str(c or "").strip().lower())


def _xml_vers_csv(raw):
    """Un enregistrement par élément enfant de la racine ; champs en sous-éléments ou en attributs.
    Bibliothèque standard : les entités externes ne sont jamais chargées et expat limite l'expansion
    des entités internes. Le contenu reste une donnée, jamais une consigne."""
    def nom_court(tag):
        return _normaliser_entete(str(tag).split("}")[-1])

    def est_enregistrement(e):
        return "date" in {nom_court(k) for k in e.attrib} | {nom_court(x.tag) for x in e}

    noeud = ET.fromstring(raw)
    while not any(est_enregistrement(e) for e in noeud) and len(noeud) == 1:
        noeud = noeud[0]  # racine > conteneur(s) > enregistrements
    enregistrements = [e for e in noeud if est_enregistrement(e)] or [e for e in noeud if isinstance(e.tag, str)]
    lignes, colonnes = [], []
    for e in enregistrements:
        champs = {nom_court(k): v for k, v in e.attrib.items()}
        for x in e:
            champs[nom_court(x.tag)] = (x.text or "").strip()
        for k in champs:
            if k not in colonnes:
                colonnes.append(k)
        lignes.append(champs)
    if not lignes:
        raise ValueError("Fichier XML sans enregistrement")
    return _vers_csv([colonnes] + [[ch.get(c, "") for c in colonnes] for ch in lignes])


def _pdf_vers_csv(raw):
    """Tableaux du PDF (toutes les pages). L'en-tête est la première ligne qui contient date, aerodrome,
    type_mouvement et nombre_mouvements ; il peut être répété en haut de chaque page."""
    import pdfplumber
    entete, lignes = None, []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            tableaux = page.extract_tables() or []
            if not tableaux:  # repli : texte séparé par ; ou tabulation
                texte = page.extract_text() or ""
                tableaux = [[re.split(r"\s*[;\t]\s*", l.strip()) for l in texte.splitlines() if l.strip()]]
            for tab in tableaux:
                for row in tab:
                    cellules = [("" if v is None else str(v).replace("\n", " ").strip()) for v in row]
                    norm = [_normaliser_entete(v) for v in cellules]
                    if all(c in norm for c in COLS_OBLIGATOIRES):
                        entete = norm
                        continue
                    if entete and len(cellules) == len(entete):
                        lignes.append(cellules)
    if entete is None:
        raise ValueError("Aucun tableau de trafic trouvé dans le PDF (en-tête attendu : "
                         + ", ".join(COLS_OBLIGATOIRES) + ")")
    return _vers_csv([entete] + lignes)


def lire_fichier(fichier, nom):
    """Lit un CSV, un Excel (feuille de trafic journalier), un XML ou un PDF (tableaux)."""
    nom = nom.lower()
    if nom.endswith((".xlsx", ".xls")):
        xl = pd.ExcelFile(fichier)
        feuille = next((s for s in xl.sheet_names if "trafic" in s.lower() and "journ" in s.lower()), xl.sheet_names[0])
        return pd.read_excel(xl, sheet_name=feuille)
    if hasattr(fichier, "read"):
        raw = fichier.read()
    else:
        with open(fichier, "rb") as fh:
            raw = fh.read()
    if nom.endswith(".xml"):
        raw = _xml_vers_csv(raw)
    elif nom.endswith(".pdf"):
        raw = _pdf_vers_csv(raw)
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=enc, sep=None, engine="python")
        except Exception:
            continue
    raise ValueError("Fichier illisible")


def nettoyer(brut):
    manque = [c for c in COLS_OBLIGATOIRES if c not in brut.columns]
    if manque:
        raise ValueError("Colonnes manquantes : " + ", ".join(manque))
    df = brut.copy()
    df["ligne_fichier"] = df.index + 2
    df["aerodrome"] = df["aerodrome"].astype(str).str.strip().str.upper()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    journal = []

    def log(date, aero, typ, probleme, valeur, traitement, ligne=None):
        journal.append({"date": None if pd.isna(date) else pd.Timestamp(date).date().isoformat(),
                        "aerodrome": aero, "type_mouvement": typ, "probleme": probleme,
                        "valeur": valeur, "traitement": traitement, "ligne_fichier": ligne})

    brut_mois = df.groupby(df["date"].dt.strftime("%Y-%m"))["nombre_mouvements"].sum().rename("total_brut")
    total_brut = float(df["nombre_mouvements"].sum())
    nb_brut = len(df)

    # Sécurité : texte ressemblant à une consigne = donnée, jamais instruction
    for c in df.select_dtypes(include=["object", "str"]).columns:
        if c in ("aerodrome", "type_mouvement"):
            continue
        masque = df[c].astype(str).str.len().gt(40) & df[c].astype(str).str.contains(MOTIFS_CONSIGNE)
        for _, x in df[masque].iterrows():
            log(x["date"], x["aerodrome"], x["type_mouvement"], f"Texte ressemblant à une consigne (colonne {c})",
                None, "Texte ignoré, traité comme une donnée", int(x["ligne_fichier"]))

    cles = [c for c in df.columns if c != "ligne_fichier"]
    dup = df.duplicated(subset=cles, keep="first")
    for _, x in df[dup].iterrows():
        log(x["date"], x["aerodrome"], x["type_mouvement"], "Doublon exact", x["nombre_mouvements"],
            "Doublon retiré, première ligne conservée", int(x["ligne_fichier"]))
    df = df[~dup]

    invalide = df["nombre_mouvements"].isna() | (df["nombre_mouvements"] < 0)
    for _, x in df[invalide].iterrows():
        vide = pd.isna(x["nombre_mouvements"])
        log(x["date"], x["aerodrome"], x["type_mouvement"], "Valeur vide" if vide else "Valeur négative",
            None if vide else x["nombre_mouvements"], "Exclue des totaux, jamais remplacée par zéro",
            int(x["ligne_fichier"]))
    df = df[~invalide].copy()

    if "retard_moyen_min" in df.columns:
        for _, x in df[df["retard_moyen_min"].isna()].iterrows():
            log(x["date"], x["aerodrome"], x["type_mouvement"], "Retard vide", x["nombre_mouvements"],
                "Exclu des moyennes de retard", int(x["ligne_fichier"]))

    # Anomalies : médiane et MAD par (aérodrome, type de mouvement)
    g = df.groupby(["aerodrome", "type_mouvement"])["nombre_mouvements"]
    med = g.transform("median")
    mad = g.transform(lambda s: (s - s.median()).abs().median())
    df["score_anomalie"] = ((df["nombre_mouvements"] - med) / (1.4826 * mad.where(mad > 0))).round(1)
    df["anomalie"] = (df["score_anomalie"].abs() > SEUIL_ANOMALIE).astype(int)
    for _, x in df[df["anomalie"] == 1].iterrows():
        log(x["date"], x["aerodrome"], x["type_mouvement"], "Anomalie statistique", x["nombre_mouvements"],
            f"Conservée dans les totaux, à valider (score {x['score_anomalie']})", int(x["ligne_fichier"]))

    # Jours absents par aérodrome (un jour absent n'est pas un zéro)
    for aero, d in df.groupby("aerodrome"):
        presents = set(d["date"].dt.normalize())
        tous = pd.date_range(d["date"].min(), d["date"].max(), freq="D")
        absents = [j for j in tous if j not in presents]
        if absents:
            debut = prev = absents[0]
            for j in absents[1:] + [None]:
                if j is None or (j - prev).days > 1:
                    n = (prev - debut).days + 1
                    log(debut, aero, None, f"{n} jour(s) sans ligne" + ("" if n == 1 else f" jusqu'au {prev.date().isoformat()}"),
                        None, "Aucun zéro ajouté", None)
                    debut = j
                if j is not None:
                    prev = j

    df["mois"] = df["date"].dt.strftime("%Y-%m")
    jours = df.groupby("mois")["date"].nunique().rename("jours_couverts")
    mois = jours.to_frame()
    mois["statut"] = ["Complet" if n >= JOURS_MOIS_COMPLET else "INCOMPLET" for n in mois["jours_couverts"]]
    df = df.merge(mois[["statut"]], left_on="mois", right_index=True, how="left")

    journal = pd.DataFrame(journal, columns=["date", "aerodrome", "type_mouvement", "probleme", "valeur",
                                             "traitement", "ligne_fichier"])
    meta = {"total_brut": total_brut, "lignes_brutes": nb_brut, "total_nettoye": float(df["nombre_mouvements"].sum()),
            "lignes_propres": len(df), "brut_par_mois": brut_mois, "date_min": df["date"].min(), "date_max": df["date"].max()}
    return df, journal, mois, meta


def nettoyer_sans_mois_partiels(brut):
    """Nettoie (règles de nettoyer inchangées), puis retire de l'analyse les mois partiels (moins de
    JOURS_MOIS_COMPLET jours renseignés) : leurs lignes sont écartées du fichier source et le nettoyage est
    refait, pour que tous les chiffres (totaux bruts et nettoyés, journal, qualité) portent sur le même
    périmètre. meta["mois_partiels_retires"] liste (mois, jours renseignés, mouvements, lignes) retirés."""
    df, journal, mois, meta = nettoyer(brut)
    partiels = mois.index[mois["statut"] == "INCOMPLET"].tolist()
    if not partiels:
        return df, journal, mois, {**meta, "mois_partiels_retires": []}
    retires = [(m, int(mois.loc[m, "jours_couverts"]),
                float(df.loc[df["mois"] == m, "nombre_mouvements"].sum()), int((df["mois"] == m).sum()))
               for m in partiels]
    mois_brut = pd.to_datetime(brut["date"], errors="coerce").dt.strftime("%Y-%m")
    if mois_brut.isin(partiels).all():  # rien d'autre dans le fichier : on garde l'analyse telle quelle
        return df, journal, mois, {**meta, "mois_partiels_retires": []}
    df, journal, mois, meta = nettoyer(brut[~mois_brut.isin(partiels)])  # index gardé : n° de ligne d'origine
    return df, journal, mois, {**meta, "mois_partiels_retires": retires}


def totaux_mensuels(df, mois):
    p = df.pivot_table(index="mois", columns="aerodrome", values="nombre_mouvements", aggfunc="sum", fill_value=0)
    p["Total"] = p.sum(axis=1)
    out = mois.join(p, how="right")
    return out.reset_index().rename(columns={"index": "mois"})


def retard_pondere(df):
    if "retard_moyen_min" not in df.columns:
        return pd.DataFrame(columns=["aerodrome", "retard_moyen_pondere_min"])
    x = df.dropna(subset=["retard_moyen_min"])
    s = (x["nombre_mouvements"] * x["retard_moyen_min"]).groupby(x["aerodrome"]).sum() / x.groupby("aerodrome")["nombre_mouvements"].sum()
    return s.round(2).rename("retard_moyen_pondere_min").reset_index()


# ---------- Commentaire de tendance (calcul pur, aucun modèle externe) ----------
NOMS_MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
                "septembre", "octobre", "novembre", "décembre"]


def fmt_nombre(x, decimales=0):
    """Format français : espace pour les milliers, virgule décimale."""
    s = f"{x:,.{decimales}f}".replace(",", " ")
    return s.replace(".", ",")


def libelle_mois(m):
    """'2025-01' -> 'janvier 2025'."""
    a, n = m.split("-")
    return f"{NOMS_MOIS_FR[int(n) - 1]} {a}"


def _variation(v0, v1):
    ecart = v1 - v0
    pct = f" ({'+' if ecart >= 0 else ''}{fmt_nombre(ecart / v0 * 100, 1)} %)" if v0 else ""
    return f"{'+' if ecart >= 0 else ''}{fmt_nombre(ecart)}{pct}"


def commentaire_tendance(df, mois):
    """Paragraphe factuel de tendance, calculé sur les mois complets du DataFrame nettoyé.
    Aucune cause n'est avancée : les variations sont décrites, pas expliquées."""
    if df.empty:
        return "Aucune donnée sur la sélection : pas de commentaire de tendance."
    statut = mois["statut"].to_dict()
    presents = sorted(df["mois"].unique())
    complets = [m for m in presents if statut.get(m) == "Complet"]
    exclus = [m for m in presents if statut.get(m) != "Complet"]
    phrases = []
    if not complets:
        phrases.append("Aucun mois complet sur la sélection : la tendance ne peut pas être décrite.")
    else:
        d = df[df["mois"].isin(complets)]
        tot = d.groupby("mois")["nombre_mouvements"].sum()
        pic, bas = tot.idxmax(), tot.idxmin()
        phrases.append(f"Sur {len(complets)} mois complets ({libelle_mois(complets[0])} à {libelle_mois(complets[-1])}), "
                       f"le mois de pointe est {libelle_mois(pic)} avec {fmt_nombre(tot[pic])} mouvements "
                       f"et le mois le plus bas est {libelle_mois(bas)} avec {fmt_nombre(tot[bas])} mouvements.")
        if len(complets) >= 2:
            phrases.append(f"Entre {libelle_mois(complets[0])} ({fmt_nombre(tot.iloc[0])}) et {libelle_mois(complets[-1])} "
                           f"({fmt_nombre(tot.iloc[-1])}), le total varie de {_variation(tot.iloc[0], tot.iloc[-1])}.")
            par_aero = []
            for aero, g in d.groupby("aerodrome"):
                s = g.groupby("mois")["nombre_mouvements"].sum()
                if len(s) >= 2:
                    periode = "" if (s.index[0], s.index[-1]) == (complets[0], complets[-1]) else \
                        f" de {libelle_mois(s.index[0])} à {libelle_mois(s.index[-1])}"
                    par_aero.append(f"{aero} {_variation(s.iloc[0], s.iloc[-1])}{periode}, pointe en {libelle_mois(s.idxmax())}")
            if par_aero:
                phrases.append("Par aérodrome : " + " ; ".join(par_aero) + ".")
    if exclus:
        jours = mois["jours_couverts"].to_dict()
        phrases.append("Mois incomplets exclus de ce commentaire : " +
                       ", ".join(f"{libelle_mois(m)} ({int(jours.get(m, 0))} jour{'s' if jours.get(m, 0) > 1 else ''} renseigné{'s' if jours.get(m, 0) > 1 else ''})" for m in exclus) + ".")
    nb_ano = int(df["anomalie"].sum()) if "anomalie" in df.columns else 0
    if nb_ano:
        phrases.append(f"{nb_ano} anomalie(s) statistique(s) restent incluses dans ces totaux, en attente de validation.")
    phrases.append("Ces constats décrivent les chiffres sans les expliquer : la cause des variations est inconnue, "
                   "à vérifier par l'expert métier.")
    return " ".join(phrases)


# ---------- Répartition par catégorie de vol / origine-destination ----------
COLONNES_REPARTITION = {"categorie_vol": "Catégorie de vol", "origine_destination": "Origine / destination"}


def repartition(df, colonne):
    """Mouvements par aérodrome (lignes) et valeur de la colonne (colonnes), avec total et part en %.
    Calcul direct sur le DataFrame nettoyé ; une valeur vide est comptée sous « (non renseigné) »."""
    if colonne not in df.columns or df.empty:
        return pd.DataFrame()
    x = df.assign(**{colonne: df[colonne].astype("object").where(df[colonne].notna(), "(non renseigné)").astype(str)})
    p = x.pivot_table(index="aerodrome", columns=colonne, values="nombre_mouvements", aggfunc="sum", fill_value=0)
    p.loc["Total"] = p.sum()
    p["Total"] = p.sum(axis=1)
    part = (p.loc["Total"] / p.loc["Total", "Total"] * 100).round(1)
    p.loc["Part (%)"] = part
    p.columns.name = None
    return p.reset_index().rename(columns={"aerodrome": "Aérodrome"})


# ---------------------------------------------------------------------------
# Comparaison de deux périodes
# ---------------------------------------------------------------------------
def _decaler(mois, n):
    p = pd.Period(mois, freq="M") + n
    return str(p)


def periodes_comparables(mois_info, duree_max=6):
    """Deux périodes par défaut de même durée, en mois complets consécutifs : les derniers mois complets
    (au plus duree_max) et les mêmes mois un an plus tôt s'ils sont complets ; sinon la période qui précède
    immédiatement, en réduisant la durée si nécessaire. Renvoie ((a1, a2), (b1, b2)) ou None."""
    complets = set(mois_info.index[mois_info["statut"] == "Complet"])
    if not complets:
        return None
    fin = max(complets)
    n = 0  # longueur de la suite de mois complets consécutifs qui finit au dernier mois complet
    while _decaler(fin, -n) in complets:
        n += 1
    for duree in range(min(duree_max, n), 0, -1):
        b = (_decaler(fin, -duree + 1), fin)
        a = (_decaler(b[0], -12), _decaler(fin, -12))
        if all(_decaler(a[0], i) in complets for i in range(duree)):
            return a, b
    duree = min(duree_max, n // 2)
    if duree == 0:
        return None
    b = (_decaler(fin, -duree + 1), fin)
    return (_decaler(b[0], -duree), _decaler(b[0], -1)), b


def nb_mois(debut, fin):
    return (pd.Period(fin, freq="M") - pd.Period(debut, freq="M")).n + 1


def comparer_periodes(df, a, b):
    """Total et moyenne par jour de chaque période, par aérodrome. La moyenne par jour divise le total par le
    nombre de jours renseignés pour l'aérodrome dans la période (un jour absent n'est jamais compté comme zéro)."""
    def bilan(debut, fin):
        p = df[df["mois"].between(debut, fin)]
        g = p.groupby("aerodrome")
        return g["nombre_mouvements"].sum(), g["date"].nunique()

    ta, ja = bilan(*a)
    tb, jb = bilan(*b)
    lib_a, lib_b = f"A ({a[0]} à {a[1]})", f"B ({b[0]} à {b[1]})"
    t = pd.DataFrame({f"{lib_a} total": ta, f"{lib_a} jours": ja, f"{lib_a} moyenne par jour": ta / ja,
                      f"{lib_b} total": tb, f"{lib_b} jours": jb, f"{lib_b} moyenne par jour": tb / jb})
    t["Écart (total)"] = t[f"{lib_b} total"] - t[f"{lib_a} total"]
    t["Écart % (total)"] = t["Écart (total)"] / t[f"{lib_a} total"].where(t[f"{lib_a} total"] != 0) * 100
    ma, mb = t[f"{lib_a} moyenne par jour"], t[f"{lib_b} moyenne par jour"]
    t["Écart % (moyenne par jour)"] = (mb - ma) / ma.where(ma != 0) * 100
    for c in t.columns:
        if "moyenne" in c or "%" in c:
            t[c] = t[c].round(2)
    return t.rename_axis("Aérodrome").reset_index()
