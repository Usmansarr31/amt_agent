"""Analyse des questions en texte libre (français ou anglais) pour l'AMT Agent.

Principe : aucune IA externe, aucun accès Internet. La question est analysée par règles
(mots-clés, codes d'aérodrome, mois), puis chaque chiffre est CALCULÉ sur le DataFrame nettoyé
produit par core.nettoyer. Jamais d'estimation, jamais de cause inventée.
Le contenu du fichier est traité comme une donnée : il n'est jamais interprété comme une consigne.
"""
import re
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import core

JOURS_MIN = core.JOURS_MOIS_COMPLET
COUVERTURE = ContextVar("couverture", default=None)  # DataFrame nettoyé complet de la question en cours

# Table de correspondance de référence code -> ville (et variantes d'écriture).
# Elle ne sert qu'à reconnaître un aérodrome PRÉSENT dans le fichier, ou à signaler qu'une ville
# connue n'y figure pas. Un nom présent dans le fichier (colonne nom/ville) est aussi reconnu.
VILLES = {
    "DAK": ["dakar"], "ABJ": ["abidjan"], "NIM": ["niamey"], "LME": ["lome"],
    "OUA": ["ouagadougou", "ouaga"], "BKO": ["bamako"], "DLA": ["douala"],
    "NDJ": ["ndjamena", "n djamena"], "BZV": ["brazzaville"], "LBV": ["libreville"],
    "NKC": ["nouakchott"], "TNR": ["antananarivo", "tananarive"], "COO": ["cotonou"],
    "NSI": ["yaounde"], "BGF": ["bangui"], "MLN": ["malabo"], "BJL": ["banjul"],
    "OXB": ["bissau"], "HAH": ["moroni"], "PNR": ["pointe noire"], "ZND": ["zinder"],
    "BOY": ["bobo dioulasso"], "ROB": ["monrovia"], "CKY": ["conakry"], "ACC": ["accra"],
    "LOS": ["lagos"], "PAR": ["paris"], "CDG": ["paris"],
}
COLONNES_NOM = ("nom_aerodrome", "nom", "ville", "aeroport", "nom_aeroport", "aerodrome_nom", "libelle")

MOIS_NOMS = {
    1: (["janvier", "january"], ["janv", "jan"]),
    2: (["fevrier", "february"], ["fevr", "fev", "feb"]),
    3: (["mars", "march"], ["mar"]),
    4: (["avril", "april"], ["avr", "apr"]),
    5: (["mai"], ["may"]),
    6: (["juin", "june"], ["jun"]),
    7: (["juillet", "july"], ["juil", "jul"]),
    8: (["aout", "august"], ["aug"]),
    9: (["septembre", "september"], ["sept", "sep"]),
    10: (["octobre", "october"], ["oct"]),
    11: (["novembre", "november"], ["nov"]),
    12: (["decembre", "december"], ["dec"]),
}
MOIS_EN = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]

MOTS_EN = {"what", "was", "is", "the", "how", "many", "much", "traffic", "between", "and", "of", "in", "for",
           "show", "give", "average", "delay", "delays", "which", "were", "did", "month", "airport", "airports",
           "arrivals", "departures", "only", "forecast", "why", "please", "me", "tell", "with", "from", "to",
           "your", "my", "rules", "movements", "flights", "passengers", "weather", "should", "we", "s", "at",
           "are", "there", "any", "list", "evolution", "change", "trend", "next", "all", "tomorrow", "slot"}
MOTS_FR = {"quel", "quelle", "quels", "quelles", "est", "le", "la", "les", "de", "du", "des", "combien", "entre",
           "et", "en", "sur", "pour", "moyen", "retard", "retards", "mois", "aerodrome", "aerodromes", "arrivees",
           "departs", "seulement", "donne", "montre", "affiche", "pourquoi", "au", "aux", "tes", "ton", "ta",
           "je", "tu", "vous", "prevision", "trafic", "mouvements", "passagers", "baisse", "hausse", "cause",
           "y", "il", "a", "t", "on", "faut", "demain", "proposer", "creneau", "tous", "toute", "periode", "d", "l"}

R = {  # motifs sur texte normalisé (minuscules, sans accents)
    "injection": [
        r"\b(ignore[rsz]?|oublie[rsz]?|forget|disregard|contourne[rsz]?|bypass|override|desactive[rsz]?|disable)\b.{0,40}"
        r"\b(regles?|rules?|consignes?|instructions?|restrictions?|limites?|garde ?fous|guardrails?|filtres?|filters?)\b",
        r"\b(code source|source code|ton code|votre code|ton propre code|your code|ton programme|your program|"
        r"system prompt|prompt systeme|invite systeme|tes instructions|vos instructions|your instructions|"
        r"jailbreak|mode developpeur|developer mode|dan mode)\b",
        r"\b(revele[rsz]?|montre[rsz]?|affiche[rsz]?|donne[rsz]?|show|reveal|print|display|dump|give)\b.{0,30}"
        r"\b(code|prompt|regles internes|configuration|secrets?|mot de passe|password|cle api|api key)\b",
        r"\b(tu es maintenant|you are now|agis comme|act as|pretend|fais semblant)\b",
    ],
    "operationnel_fort": r"\b(creneaux?|slots?|pistes?|runways?|separation|clearance|derout\w*|divert\w*|reroute\w*|"
                         r"regulation de flux|flow control|securite|safety|niveau de vol|flight level|holding)\b|"
                         r"\b(annuler|retarder|cancel|delay)\b.{0,20}\b(vols?|flights?|avions?|aircraft)\b",
    "decision": r"\b(faut il|dois je|devons nous|doit on|should (i|we)|recommande\w*|recommend\w*|propose[rsz]?|"
                r"decide[rsz]?|autorise[rsz]?|authori[sz]e|allouer|allocate|attribuer|assign)\b",
    "objet_op": r"\b(vols?|flights?|atterriss\w*|decoll\w*|landing|take ?off|avions?|aircraft|demain|tomorrow|"
                r"aujourd hui|today|ce soir|tonight|controleurs?|controllers?|atc|effectifs?|staffing)\b",
    "cause": r"\b(causes?|pourquoi|why|raisons?|reasons?|expliqu\w*|explain\w*|motifs?|due to|du a|origine de la (baisse|hausse|variation|chute))\b",
    "prevision": r"\b(prevision\w*|prevoi\w*|prevoy\w*|prevoir|forecast\w*|predict\w*|projection\w*|projet\w*|"
                 r"estim\w*|anticip\w*|mois prochain|prochain mois|next month|futur\w*|future)\b",
    "anomalie": r"\b(anomal\w*|outliers?|atypiques?|aberrant\w*|a valider|to validate|unusual|suspect\w*)\b",
    "retard": r"\b(retards?|delays?|ponctualit\w*|punctualit\w*|on time)\b",
    "comparaison": r"\b(compar\w*|versus|vs|ecarts?|differences?|differ\w*|contre)\b",
    "evolution": r"\b(evolu\w*|variations?|varie|change[sd]?|progress\w*|hausse|baisse|augment\w*|diminu\w*|grow\w*|"
                 r"increase\w*|decrease\w*|drop\w*|trend\w*|tendances?)\b",
    "total": r"\b(total\w*|trafic|traffic|mouvements?|movements?|combien|how many|nombre|volume|activite|activity|"
             r"vols?|flights?|somme|sum)\b",
    "tous": r"\b(tous les aerodromes|tous aerodromes|ensemble des aerodromes|all (the )?airports|all aerodromes|"
            r"reseau|network|global\w*|tous)\b",
    "toute_periode": r"\b(toute la periode|sur la periode|whole period|entire period|all period|overall|"
                     r"depuis le debut|en tout|au total)\b",
    "arrivee": r"\b(arrivees?|arrivals?|arrive|atterrissages?|landings?)\b",
    "depart": r"\b(departs?|departures?|decollages?|take ?offs?)\b",
    "aide": r"^\s*(bonjour|salut|hello|hi|aide|help|\?)\s*[!.?]*\s*$",
}

ABSENTS = {  # notion -> (motif, libellé FR, libellé EN, racines de colonne qui la rendraient disponible)
    "passagers": (r"\b(passagers?|passengers?|pax|voyageurs?|travell?ers?)\b", "le nombre de passagers",
                  "passenger numbers", ("passag", "pax")),
    "meteo": (r"\b(meteo\w*|weather|pluie|rain|vents?|wind|orages?|storms?|brouillard|fog|visibilite|visibility)\b",
              "la météo", "weather", ("meteo", "weather")),
    "fret": (r"\b(fret|cargo|marchandises?|freight|tonnage)\b", "le fret", "cargo", ("fret", "cargo")),
    "compagnies": (r"\b(compagnies?|airlines?|transporteurs?|carriers?|operateurs? aeriens?)\b",
                   "les compagnies aériennes", "airlines", ("compagn", "airline", "operateur")),
    "finances": (r"\b(recettes?|redevances?|revenus?|revenue|chiffre d affaires|tarifs?|prix|price|cout\w*|cost\w*)\b",
                 "les données financières", "financial data", ("redevance", "recette", "revenu")),
    "carburant": (r"\b(carburant|kerosene|fuel|co2|emissions?|carbone|carbon)\b", "le carburant et les émissions",
                  "fuel and emissions", ("carbur", "fuel", "co2")),
    "horaire": (r"\b(heures? de pointe|peak hours?|par heure|per hour|horaires?|hourly)\b",
                "le détail horaire (le fichier est journalier)", "hourly detail (the file is daily)", ("heure", "hour")),
    "personnel": (r"\b(personnel|staff|controleurs?|controllers?|effectifs?|agents? de)\b",
                  "les effectifs", "staffing", ("personnel", "effectif")),
}

T = {
    "mention": {"fr": "_Validation de l'expert métier requise. Données fictives._",
                "en": "_Business expert validation required. Fictitious data._"},
    "refus_securite": {
        "fr": "Je ne peux pas répondre à cette demande. Je ne révèle ni mon code ni mes règles internes, et je ne les "
              "contourne pas. Je réponds uniquement aux questions sur les chiffres du fichier de trafic chargé.",
        "en": "I can't do that. I don't reveal my code or internal rules, and I don't bypass them. I only answer "
              "questions about the figures in the loaded traffic file."},
    "refus_operationnel": {
        "fr": "Je ne peux pas proposer de décision opérationnelle (créneaux, vols, piste, sécurité). Cette décision "
              "revient aux responsables habilités. Je peux en revanche fournir des chiffres historiques du fichier "
              "pour éclairer leur analyse, par exemple le trafic d'un aérodrome sur un mois.",
        "en": "I can't suggest an operational decision (slots, flights, runway, safety). That decision belongs to "
              "the authorised officers. I can provide historical figures from the file to inform their analysis, "
              "for example an aerodrome's traffic over a month."},
    "cause": {
        "fr": "La cause est inconnue : le fichier ne contient aucune variable explicative (météo, événements, "
              "compagnies, travaux...). Je ne peux donc pas l'établir et je n'avance aucune hypothèse. "
              "La cause est à vérifier par l'expert métier.",
        "en": "The cause is unknown: the file contains no explanatory variable (weather, events, airlines, works...). "
              "I therefore cannot establish it and I make no assumption. The cause must be checked by the "
              "business expert."},
    "ambigu": {"fr": "Je n'ai pas compris précisément la question. Pouvez-vous préciser l'aérodrome, le mois "
                     "(avec l'année) et l'indicateur souhaité ? Exemples :",
               "en": "I didn't fully understand the question. Could you specify the aerodrome, the month "
                     "(with the year) and the indicator? Examples:"},
}


@dataclass
class Reponse:
    texte: str
    langue: str
    type: str
    chiffres: dict = field(default_factory=dict)


# ---------------- Outils de texte ----------------
def normaliser(texte):
    t = unicodedata.normalize("NFKD", str(texte))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[’'`´]", " ", t)
    t = re.sub(r"[^a-z0-9\-/.]+", " ", t)
    t = re.sub(r"(?<!\d)[./](?!\d)", " ", t)  # garde 2025/01, retire la ponctuation
    return re.sub(r"\s+", " ", t).strip()


def langue_de(question):
    mots = normaliser(question).split()
    en = sum(m in MOTS_EN for m in mots)
    fr = sum(m in MOTS_FR for m in mots)
    if any(n in mots for noms, _ in MOIS_NOMS.values() for n in noms if n in
           ("january", "february", "march", "april", "june", "july", "august", "september", "october",
            "november", "december")):
        en += 1
    return "en" if en > fr else "fr"


def a(motif, q):
    return re.search(motif, q) is not None


def nb(x, lg, dec=0):
    return f"{x:,.{dec}f}" if lg == "en" else core.fmt_nombre(x, dec)


def signe(x, lg, dec=0):
    return ("+" if x > 0 else "") + nb(x, lg, dec)


def pct(ecart, base, lg):
    if not base:
        return "n/a" if lg == "en" else "non calculable"
    v = ecart / base * 100
    return f"{'+' if v > 0 else ''}{v:,.2f}%" if lg == "en" else f"{'+' if v > 0 else ''}{core.fmt_nombre(v, 2)} %"


def lib_mois(m, lg):
    an, n = m.split("-")
    return f"{MOIS_EN[int(n) - 1]} {an}" if lg == "en" else core.libelle_mois(m)


def nettoyer_affichage(s):
    return re.sub(r"[^\w\- ]", "", str(s))[:30]


# ---------------- Reconnaissance des entités ----------------
def index_aerodromes(df):
    """alias normalisé -> code du fichier. Construit uniquement à partir des codes présents."""
    idx = {}
    codes = sorted(df["aerodrome"].unique())
    for code in codes:
        prefixe = code.split("-")[0]
        for alias in {code, prefixe}:
            idx[normaliser(alias)] = code
        for v in VILLES.get(prefixe, []):
            idx[v] = code
        for col in COLONNES_NOM:
            if col in df.columns:
                for nom in df.loc[df["aerodrome"] == code, col].dropna().astype(str).unique():
                    if len(normaliser(nom)) >= 3:
                        idx[normaliser(nom)] = code
    if "pays" in df.columns:  # un pays qui ne correspond qu'à un aérodrome du fichier
        p = df.groupby(df["pays"].astype(str).map(normaliser))["aerodrome"].unique()
        for pays, cs in p.items():
            if len(cs) == 1 and len(pays) >= 3:
                idx.setdefault(pays, cs[0])
    return idx


def trouver_aerodromes(q, question, df):
    idx = index_aerodromes(df)
    trouves = []
    for alias, code in idx.items():
        m = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", q)
        if m:
            trouves.append((m.start(), code))
    ordre = []
    for _, code in sorted(trouves):
        if code not in ordre:
            ordre.append(code)
    inconnus = []
    presents = {c.split("-")[0] for c in df["aerodrome"].unique()}
    for prefixe, villes in VILLES.items():
        if prefixe in presents:
            continue
        for v in villes:
            if re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", q) and v not in idx:
                inconnus.append(v.title())
    for code in re.findall(r"\b([A-Za-z]{3}-SIM|[A-Z]{3}-[A-Z]{3})\b", question):
        if code.upper() not in df["aerodrome"].values:
            inconnus.append(code.upper())
    # Nom propre inconnu placé après une préposition : « à Paris », « at Lagos »
    connus = set(idx) | {n for noms, courts in MOIS_NOMS.values() for n in noms + courts}
    for mot in re.findall(r"\b(?:à|a|de|d'|du|at|in|for|of|pour|sur)\s+([A-Z][a-zA-Zéèêàôûç\-]{2,})", question):
        mn = normaliser(mot)
        if mn not in connus and mn not in MOTS_FR and mn not in MOTS_EN and not mn.isdigit() \
                and mn not in ("arrivee", "depart", "total", "anomalies", "validation"):
            inconnus.append(nettoyer_affichage(mot))
    return ordre, list(dict.fromkeys(inconnus))


def trouver_mois(q, df):
    """Renvoie (mois 'AAAA-MM' dans l'ordre, années seules, mois sans année ambigus)."""
    disponibles = set(df["mois"].unique())
    occurrences = []  # (position, numero_mois, annee ou None, fin)
    for mt in re.finditer(r"\b(20\d{2})[-/.](0?[1-9]|1[0-2])\b", q):
        occurrences.append((mt.start(), int(mt.group(2)), int(mt.group(1)), mt.end()))
    for mt in re.finditer(r"\b(0?[1-9]|1[0-2])[-/](20\d{2})\b", q):
        occurrences.append((mt.start(), int(mt.group(1)), int(mt.group(2)), mt.end()))
    for num, (longs, courts) in MOIS_NOMS.items():
        for nom in longs:
            for mt in re.finditer(rf"\b{nom}\b(?:\s+(20\d{{2}}))?", q):
                occurrences.append((mt.start(), num, int(mt.group(1)) if mt.group(1) else None, mt.end()))
        for nom in courts:  # abréviations : seulement suivies d'une année
            for mt in re.finditer(rf"\b{nom}\b\s+(20\d{{2}})", q):
                occurrences.append((mt.start(), num, int(mt.group(1)), mt.end()))
    occurrences.sort()
    pris = [(o[0], o[3]) for o in occurrences]
    annees = [(mt.start(), int(mt.group(1))) for mt in re.finditer(r"\b(20\d{2})\b", q)
              if not any(d <= mt.start() < f for d, f in pris)]
    mois, ambigus = [], []
    for pos, num, an, _ in occurrences:
        if an is None:
            apres = [y for p, y in annees if p > pos]
            if apres:
                an = apres[0]
            else:
                ans = sorted({int(m[:4]) for m in disponibles if int(m[5:]) == num})
                if len(ans) == 1:
                    an = ans[0]
                else:
                    ambigus.append(num)
                    continue
        cle = f"{an}-{num:02d}"
        if cle not in mois:
            mois.append(cle)
    annees_seules = [y for p, y in annees
                     if not any(o[2] is None and o[0] < p for o in occurrences)]
    return mois, sorted(set(annees_seules)), ambigus


def type_mouvement(q, df):
    types = set(df["type_mouvement"].astype(str).str.upper().unique())
    arr, dep = a(R["arrivee"], q), a(R["depart"], q)
    if arr and not dep:
        return next((t for t in types if t.startswith("ARR")), "ARRIVEE")
    if dep and not arr:
        return next((t for t in types if t.startswith("DEP")), "DEPART")
    return None


COLONNES_CATEGORIES = {"categorie_vol": ("catégorie de vol", "flight category"),
                       "origine_destination": ("origine/destination", "origin/destination")}
# Valeur du fichier -> motif (texte normalisé). Seules les valeurs présentes dans le fichier sont utilisées.
SYNONYMES_CATEGORIES = {
    "categorie_vol": {
        "NON_REGULIER": r"\bnon[ -]?reguliers?\b|\bnon[ -]?scheduled\b|\bunscheduled\b|\bcharters?\b",
        "REGULIER": r"(?<!non )(?<!non-)(?<!non)\breguliers?\b|(?<!non )(?<!non-)\bscheduled\b",
        "AVIATION_GENERALE": r"\baviation generale\b|\bgeneral aviation\b",
    },
    "origine_destination": {
        "DOMESTIQUE": r"\bdomestiques?\b|\bdomestic\b|\binterieurs?\b|\bnationa(l|le|ux|les)\b",
        "INTERNATIONAL": r"\binternationa(l|le|ux|les)\b",
        "REGION_CENTRE": r"\bregion(ale)? centre\b|\bcentre region\b|\bcentral region\b|\bregion center\b",
        "REGION_OUEST": r"\bregion(ale)? ouest\b|\bouest region\b|\bwest(ern)? region\b|\bregion west\b",
    },
}
DEMANDES_REPARTITION = {
    "categorie_vol": r"\b(categories?|category|categorie de vol|types? de vols?)\b",
    "origine_destination": r"\b(origines?|destinations?|origin|provenance|regions?|regionau?x|regional\w*)\b",
}


def trouver_categories(q, df):
    """Filtres sur categorie_vol / origine_destination cités dans la question, s'ils existent dans le fichier."""
    out = {}
    for col, valeurs in SYNONYMES_CATEGORIES.items():
        if col not in df.columns:
            continue
        presentes = set(df[col].dropna().astype(str).str.upper().unique())
        for val, motif in valeurs.items():
            brut = normaliser(val.replace("_", " "))
            if val in presentes and (a(motif, q) or a(rf"\b{re.escape(brut)}\b", q)):
                out[col] = val
                break
    return out


# ---------------- Calculs tracés ----------------
def filtrer(df, aeros=None, mois=None, typ=None):
    d = df
    if aeros:
        d = d[d["aerodrome"].isin(aeros)]
    if mois:
        d = d[d["mois"].isin(mois)]
    if typ:
        d = d[d["type_mouvement"].astype(str).str.upper() == typ]
    return d


def lib_type(typ, lg):
    if typ is None:
        return "all" if lg == "en" else "tous"
    if typ.startswith("ARR"):
        return "arrivals only" if lg == "en" else "arrivées seulement"
    return "departures only" if lg == "en" else "départs seulement"


def methode(d, aeros, mois, typ, lg, periode_txt=None):
    aer = ", ".join(aeros) if aeros else ("all aerodromes" if lg == "en" else "tous les aérodromes")
    per = periode_txt or (", ".join(mois) if mois else ("whole file period" if lg == "en" else "toute la période du fichier"))
    jours = d["date"].dt.normalize().nunique()
    if lg == "en":
        return (f"**Method**: sum of `nombre_mouvements` on the cleaned data (duplicates removed, empty and negative "
                f"values excluded, never imputed). Filters: aerodrome = {aer}; period = {per}; type = {lib_type(typ, lg)}. "
                f"Rows used: {len(d):,}. Days with data: {jours}.")
    return (f"**Méthode** : somme de `nombre_mouvements` sur les données nettoyées (doublons retirés, valeurs vides et "
            f"négatives exclues, jamais imputées). Filtres : aérodrome = {aer} ; période = {per} ; type = "
            f"{lib_type(typ, lg)}. Lignes retenues : {nb(len(d), lg)}. Jours renseignés : {jours}.")


def alertes_incompletes(d, mois_info, lg):
    """Mois de la sélection ayant moins de 28 jours distincts renseignés.
    La couverture est mesurée sur toutes les lignes des aérodromes concernés (avant filtre de type ou de
    catégorie) : un vol régulier absent un jour n'est pas un jour sans données."""
    base = COUVERTURE.get()
    out = []
    for m, g in d.groupby("mois"):
        if base is not None:
            g = base[(base["mois"] == m) & base["aerodrome"].isin(g["aerodrome"].unique())]
        j = g["date"].dt.normalize().nunique()
        if j < JOURS_MIN:
            out.append(f"⚠ Incomplete month: {lib_mois(m, lg)} ({j} days out of the {JOURS_MIN} required); "
                       f"compare with caution." if lg == "en" else
                       f"⚠ Mois incomplet : {lib_mois(m, lg)} ({j} jours sur {JOURS_MIN} requis) ; "
                       f"à comparer avec prudence.")
    return out


def detail_types(d, lg):
    s = d.groupby(d["type_mouvement"].astype(str).str.upper())["nombre_mouvements"].sum()
    parts = []
    for t, v in s.items():
        nom = ("arrivals" if lg == "en" else "arrivées") if t.startswith("ARR") else \
              ("departures" if lg == "en" else "départs") if t.startswith("DEP") else t.lower()
        parts.append(f"{nb(v, lg)} {nom}")
    return ", ".join(parts)


def assembler(lg, typ_rep, corps, chiffres=None):
    texte = "\n\n".join([c for c in corps if c] + [T["mention"][lg]])
    return Reponse(texte, lg, typ_rep, chiffres or {})


# ---------------- Exemples dynamiques ----------------
def exemples(df, mois_info, lg="fr"):
    aeros = sorted(df["aerodrome"].unique())
    complets = [m for m in sorted(df["mois"].unique()) if mois_info["statut"].get(m) == "Complet"] or sorted(df["mois"].unique())
    a1, a2 = aeros[0].split("-")[0], aeros[min(1, len(aeros) - 1)].split("-")[0]
    m1, m2 = complets[0], complets[min(1, len(complets) - 1)]
    if lg == "en":
        return [f"What was {a1}'s traffic in {lib_mois(m1, 'en')}?",
                f"Total of all airports in {lib_mois(m2, 'en')}",
                f"Compare {a1} and {a2} in {lib_mois(m1, 'en')}",
                f"Average delay at {a1}", "Which anomalies need validation?",
                f"{a1} arrivals only in {lib_mois(m1, 'en')}",
                f"Evolution of {a1} between {lib_mois(m1, 'en')} and {lib_mois(m2, 'en')}"]
    return [f"Quel est le total de {a1} en {lib_mois(m1, 'fr')} ?",
            f"Total de tous les aérodromes en {lib_mois(m2, 'fr')}",
            f"Compare {a1} et {a2} en {lib_mois(m1, 'fr')}",
            f"Retard moyen de {a1}", "Quelles anomalies sont à valider ?",
            f"Arrivées seulement à {a1} en {lib_mois(m1, 'fr')}",
            f"Évolution de {a1} entre {lib_mois(m1, 'fr')} et {lib_mois(m2, 'fr')}"]


def rep_ambigue(df, mois_info, lg, precision=None):
    ex = "\n".join(f"- {e}" for e in exemples(df, mois_info, lg))
    return assembler(lg, "ambigu", [precision, T["ambigu"][lg], ex])


# ---------------- Réponses par type de question ----------------
def rep_total(df, mois_info, aeros, mois, typ, lg, periode_txt=None, repartir=()):
    d = filtrer(df, aeros, mois, typ)
    total = d["nombre_mouvements"].sum()
    if lg == "en":
        qui = " + ".join(aeros) if aeros else "all aerodromes"
        tete = f"**{qui}**, {periode_txt or (', '.join(lib_mois(m, lg) for m in mois) if mois else 'whole period')}" \
               f"{'' if typ is None else ' (' + lib_type(typ, lg) + ')'}: **{nb(total, lg)} movements**"
    else:
        qui = " + ".join(aeros) if aeros else "tous les aérodromes"
        tete = f"**{qui}**, {periode_txt or (', '.join(lib_mois(m, lg) for m in mois) if mois else 'toute la période')}" \
               f"{'' if typ is None else ' (' + lib_type(typ, lg) + ')'} : **{nb(total, lg)} mouvements**"
    corps = [tete]
    if typ is None:
        corps.append(("Breakdown: " if lg == "en" else "Détail : ") + detail_types(d, lg) + ".")
    if not aeros or len(aeros) > 1:
        par = d.groupby("aerodrome")["nombre_mouvements"].sum()
        corps.append("\n".join(f"- {k} : {nb(v, lg)}" if lg == "fr" else f"- {k}: {nb(v, lg)}" for k, v in par.items()))
    if mois and len(mois) > 1:
        par_m = d.groupby("mois")["nombre_mouvements"].sum()
        corps.append(("By month:\n" if lg == "en" else "Par mois :\n") +
                     "\n".join(f"- {lib_mois(k, lg)} : {nb(v, lg)}" for k, v in par_m.items()))
    for col in repartir:
        s = d.groupby(d[col].astype(str))["nombre_mouvements"].sum().sort_values(ascending=False)
        titre = COLONNES_CATEGORIES[col][1 if lg == "en" else 0]
        corps.append((f"By {titre}:\n" if lg == "en" else f"Par {titre} :\n") + "\n".join(
            f"- {k} : {nb(v, lg)} ({pct(v, total, lg).lstrip('+')})" if lg == "fr" else
            f"- {k}: {nb(v, lg)} ({pct(v, total, lg).lstrip('+')})" for k, v in s.items()))
    corps.append(methode(d, aeros, mois, typ, lg, periode_txt))
    corps += alertes_incompletes(d, mois_info, lg)
    return assembler(lg, "total", corps, {"total": float(total),
                                          "par_type": d.groupby("type_mouvement")["nombre_mouvements"].sum().to_dict()})


def rep_comparaison_aeros(df, mois_info, aeros, mois, typ, lg, periode_txt=None):
    a1, a2 = aeros[:2]
    d = filtrer(df, [a1, a2], mois, typ)
    v1 = d[d["aerodrome"] == a1]["nombre_mouvements"].sum()
    v2 = d[d["aerodrome"] == a2]["nombre_mouvements"].sum()
    ecart = v2 - v1
    per = periode_txt or (", ".join(lib_mois(m, lg) for m in mois) if mois else
                          ("whole period" if lg == "en" else "toute la période"))
    if lg == "en":
        corps = [f"Comparison over {per}{'' if typ is None else ' (' + lib_type(typ, lg) + ')'}:",
                 f"| Aerodrome | Movements |\n|---|---|\n| {a1} | {nb(v1, lg)} |\n| {a2} | {nb(v2, lg)} |",
                 f"Difference ({a2} - {a1}): **{signe(ecart, lg)}** ({pct(ecart, v1, lg)} relative to {a1})."]
    else:
        corps = [f"Comparaison sur {per}{'' if typ is None else ' (' + lib_type(typ, lg) + ')'} :",
                 f"| Aérodrome | Mouvements |\n|---|---|\n| {a1} | {nb(v1, lg)} |\n| {a2} | {nb(v2, lg)} |",
                 f"Écart ({a2} - {a1}) : **{signe(ecart, lg)}** ({pct(ecart, v1, lg)} par rapport à {a1})."]
    corps += [methode(d, [a1, a2], mois, typ, lg, periode_txt), *alertes_incompletes(d, mois_info, lg),
              "The file does not explain this difference: cause unknown, to be checked by the expert." if lg == "en"
              else "Le fichier n'explique pas cet écart : cause inconnue, à vérifier par l'expert métier."]
    return assembler(lg, "comparaison", corps, {"valeurs": [float(v1), float(v2)], "ecart": float(ecart)})


def rep_comparaison_mois(df, mois_info, aeros, m1, m2, typ, lg, evolution=False):
    d = filtrer(df, aeros, [m1, m2], typ)
    v1 = d[d["mois"] == m1]["nombre_mouvements"].sum()
    v2 = d[d["mois"] == m2]["nombre_mouvements"].sum()
    ecart = v2 - v1
    qui = " + ".join(aeros) if aeros else ("all aerodromes" if lg == "en" else "tous les aérodromes")
    if lg == "en":
        corps = [f"{'Evolution' if evolution else 'Comparison'} for **{qui}**"
                 f"{'' if typ is None else ' (' + lib_type(typ, lg) + ')'}:",
                 f"| Month | Movements |\n|---|---|\n| {lib_mois(m1, lg)} | {nb(v1, lg)} |\n| {lib_mois(m2, lg)} | {nb(v2, lg)} |",
                 f"Change ({lib_mois(m2, lg)} vs {lib_mois(m1, lg)}): **{signe(ecart, lg)}** ({pct(ecart, v1, lg)})."]
    else:
        corps = [f"{'Évolution' if evolution else 'Comparaison'} pour **{qui}**"
                 f"{'' if typ is None else ' (' + lib_type(typ, lg) + ')'} :",
                 f"| Mois | Mouvements |\n|---|---|\n| {lib_mois(m1, lg)} | {nb(v1, lg)} |\n| {lib_mois(m2, lg)} | {nb(v2, lg)} |",
                 f"Écart ({lib_mois(m2, lg)} par rapport à {lib_mois(m1, lg)}) : **{signe(ecart, lg)}** ({pct(ecart, v1, lg)})."]
    corps += [methode(d, aeros, [m1, m2], typ, lg), *alertes_incompletes(d, mois_info, lg),
              "The file does not explain this change: cause unknown, to be checked by the expert." if lg == "en"
              else "Le fichier n'explique pas cet écart : cause inconnue, à vérifier par l'expert métier."]
    return assembler(lg, "evolution" if evolution else "comparaison", corps,
                     {"valeurs": [float(v1), float(v2)], "ecart": float(ecart)})


def rep_retard(df, mois_info, aeros, mois, typ, lg):
    if "retard_moyen_min" not in df.columns:
        return assembler(lg, "absent", ["The file has no `retard_moyen_min` column: delays are not in the data."
                                        if lg == "en" else "Le fichier ne contient pas de colonne `retard_moyen_min` : "
                                        "les retards ne sont pas dans les données."])
    d = filtrer(df, aeros, mois, typ)
    vides = int(d["retard_moyen_min"].isna().sum())
    x = d.dropna(subset=["retard_moyen_min"])
    if x.empty or x["nombre_mouvements"].sum() == 0:
        return assembler(lg, "absent", ["No delay value for this selection." if lg == "en"
                                        else "Aucune valeur de retard renseignée pour cette sélection."])
    r = core.retard_pondere(x).sort_values("retard_moyen_pondere_min", ascending=False)
    if len(r) == 1 or aeros and len(aeros) == 1:
        v = r["retard_moyen_pondere_min"].iloc[0]
        tete = (f"Weighted average delay at **{r['aerodrome'].iloc[0]}**: **{nb(v, lg, 2)} min**" if lg == "en" else
                f"Retard moyen pondéré de **{r['aerodrome'].iloc[0]}** : **{nb(v, lg, 2)} min**")
    else:
        tete = ("Weighted average delay by aerodrome:\n" if lg == "en" else "Retard moyen pondéré par aérodrome :\n") + \
               "\n".join(f"- {k} : {nb(v, lg, 2)} min" for k, v in zip(r["aerodrome"], r["retard_moyen_pondere_min"]))
    met = (f"**Method**: average of `retard_moyen_min` weighted by `nombre_mouvements`; rows with empty delay excluded "
           f"({vides}), never replaced by zero. Rows used: {len(x):,}. Period: "
           f"{', '.join(mois) if mois else 'whole file'}; type: {lib_type(typ, lg)}." if lg == "en" else
           f"**Méthode** : moyenne de `retard_moyen_min` pondérée par `nombre_mouvements` ; lignes à retard vide "
           f"exclues ({vides}), jamais remplacées par zéro. Lignes retenues : {nb(len(x), lg)}. Période : "
           f"{', '.join(mois) if mois else 'tout le fichier'} ; type : {lib_type(typ, lg)}.")
    return assembler(lg, "retard", [tete, met, *alertes_incompletes(d, mois_info, lg)],
                     {"retards": dict(zip(r["aerodrome"], r["retard_moyen_pondere_min"]))})


def rep_anomalies(df, mois_info, aeros, mois, typ, lg):
    d = filtrer(df, aeros, mois, typ)
    an = d[d["anomalie"] == 1].sort_values("date")
    if an.empty:
        tete = "No statistical anomaly on this selection." if lg == "en" else "Aucune anomalie statistique sur cette sélection."
        lignes = ""
    else:
        tete = (f"**{len(an)} anomaly(ies) to validate**:" if lg == "en" else f"**{len(an)} anomalie(s) à valider** :")
        lignes = "\n".join(
            f"- {x.date:%d/%m/%Y} | {x.aerodrome} | {x.type_mouvement} | {nb(x.nombre_mouvements, lg)} | score "
            f"{nb(x.score_anomalie, lg, 1)} | {'file row' if lg == 'en' else 'ligne du fichier'} {int(x.ligne_fichier)}"
            for x in an.itertuples())
    met = (f"**Method**: score = (value - median) / (1.4826 x MAD) per aerodrome and movement type; anomaly if "
           f"|score| > {core.SEUIL_ANOMALIE:g}. Anomalies stay in the totals. Rows examined: {len(d):,}." if lg == "en" else
           f"**Méthode** : score = (valeur - médiane) / (1,4826 x MAD) par aérodrome et type de mouvement ; anomalie si "
           f"|score| > {core.SEUIL_ANOMALIE:g}. Les anomalies restent dans les totaux. Lignes examinées : {nb(len(d), lg)}.")
    cause = ("Cause unknown: to be checked and validated by the business expert (tab « Anomalies »)." if lg == "en" else
             "Cause inconnue : à vérifier et valider par l'expert métier (onglet « Anomalies »).")
    return assembler(lg, "anomalies", [tete, lignes, met, cause if not an.empty else None],
                     {"anomalies": [(x.date.date().isoformat(), x.aerodrome, x.type_mouvement, float(x.nombre_mouvements))
                                    for x in an.itertuples()]})


def mois_suivant(m, k=1):
    p = pd.Period(m, freq="M") + k
    return p.strftime("%Y-%m")


def rep_prevision(df, mois_info, aeros, mois, typ, lg):
    d = filtrer(df, aeros, None, typ)
    complets = [m for m in sorted(d["mois"].unique()) if mois_info["statut"].get(m) == "Complet"]
    serie = d[d["mois"].isin(complets)].groupby("mois")["nombre_mouvements"].sum()
    if len(serie) < 3:
        return assembler(lg, "prevision", ["Not enough complete months (at least 3) for an indicative forecast."
                                           if lg == "en" else "Pas assez de mois complets (au moins 3) pour une "
                                           "prévision indicative."])
    dernier = serie.index[-1]
    cible = next((m for m in mois if m > dernier), mois_suivant(dernier))
    h = (pd.Period(cible, freq="M") - pd.Period(dernier, freq="M")).n
    mm3 = serie.iloc[-3:].mean()
    pente, origine = np.polyfit(np.arange(len(serie)), serie.values.astype(float), 1)
    lin = origine + pente * (len(serie) - 1 + h)
    qui = " + ".join(aeros) if aeros else ("all aerodromes" if lg == "en" else "tous les aérodromes")
    if lg == "en":
        corps = [f"**INDICATIVE FORECAST, for strategic planning only**, {qui}, {lib_mois(cible, lg)}"
                 f"{'' if typ is None else ' (' + lib_type(typ, lg) + ')'}:",
                 f"- 3-month moving average ({lib_mois(serie.index[-3], lg)} to {lib_mois(dernier, lg)}): "
                 f"**{nb(mm3, lg)}**\n- Linear trend over {len(serie)} complete months (slope {signe(pente, lg, 1)} "
                 f"per month): **{nb(lin, lg)}**",
                 f"**Method**: monthly totals of complete months only ({lib_mois(serie.index[0], lg)} to "
                 f"{lib_mois(dernier, lg)}), incomplete months excluded; least-squares line.",
                 "**Limits**: no seasonality, no external events, few data points, fictitious data, anomalies included. "
                 "This is not a measured figure and must not be used for any operational decision."]
    else:
        corps = [f"**PRÉVISION INDICATIVE, réservée à la planification stratégique**, {qui}, {lib_mois(cible, lg)}"
                 f"{'' if typ is None else ' (' + lib_type(typ, lg) + ')'} :",
                 f"- Moyenne mobile sur 3 mois ({lib_mois(serie.index[-3], lg)} à {lib_mois(dernier, lg)}) : "
                 f"**{nb(mm3, lg)}**\n- Tendance linéaire sur {len(serie)} mois complets (pente {signe(pente, lg, 1)} "
                 f"par mois) : **{nb(lin, lg)}**",
                 f"**Méthode** : totaux mensuels des seuls mois complets ({lib_mois(serie.index[0], lg)} à "
                 f"{lib_mois(dernier, lg)}), mois incomplets exclus ; droite des moindres carrés.",
                 "**Limites** : pas de saisonnalité, pas d'événements extérieurs, peu de points, données fictives, "
                 "anomalies incluses. Ce n'est pas un chiffre mesuré et il ne doit servir à aucune décision opérationnelle."]
    return assembler(lg, "prevision", corps, {"moyenne_mobile": float(mm3), "tendance": float(lin), "cible": cible})


def rep_cause(df, mois_info, aeros, mois, typ, lg):
    corps = [T["cause"][lg]]
    if len(mois) == 1:
        m = mois[0]
        prec = mois_suivant(m, -1)
        d = filtrer(df, aeros, [prec, m], typ)
        if prec in set(d["mois"]):
            v0 = d[d["mois"] == prec]["nombre_mouvements"].sum()
            v1 = d[d["mois"] == m]["nombre_mouvements"].sum()
            qui = " + ".join(aeros) if aeros else ("all aerodromes" if lg == "en" else "tous les aérodromes")
            corps.append(
                f"Measured fact only ({qui}): {lib_mois(m, lg)} = {nb(v1, lg)} vs {nb(v0, lg)} in {lib_mois(prec, lg)}, "
                f"change {signe(v1 - v0, lg)} ({pct(v1 - v0, v0, lg)})." if lg == "en" else
                f"Constat chiffré seulement ({qui}) : {lib_mois(m, lg)} = {nb(v1, lg)} contre {nb(v0, lg)} en "
                f"{lib_mois(prec, lg)}, écart {signe(v1 - v0, lg)} ({pct(v1 - v0, v0, lg)}).")
            corps.append(methode(d, aeros, [prec, m], typ, lg))
            corps += alertes_incompletes(d, mois_info, lg)
    return assembler(lg, "cause_inconnue", corps)


# ---------------- Point d'entrée ----------------
def repondre(question, df, mois_info):
    """Analyse une question libre et renvoie une Reponse calculée sur le DataFrame nettoyé."""
    question = str(question or "")[:500]
    lg = langue_de(question)
    q = normaliser(question)
    if not q or a(R["aide"], q):
        return rep_ambigue(df, mois_info, lg, "Ask me a question about the traffic file." if lg == "en"
                           else "Posez-moi une question sur le fichier de trafic.")

    # 1. Sécurité : aucune demande de révéler le code ou de contourner les règles
    if any(a(p, q) for p in R["injection"]):
        return assembler(lg, "refus_securite", [T["refus_securite"][lg]])
    # 2. Décision opérationnelle : refus
    if a(R["operationnel_fort"], q) or (a(R["decision"], q) and a(R["objet_op"], q)):
        return assembler(lg, "refus_operationnel", [T["refus_operationnel"][lg]])

    aeros, inconnus = trouver_aerodromes(q, question, df)
    mois, annees, ambigus = trouver_mois(q, df)
    typ = type_mouvement(q, df)
    extra = trouver_categories(q, df)
    repartir = [c for c, motif in DEMANDES_REPARTITION.items() if c in df.columns and a(motif, q)]
    d_q = df
    for col, val in extra.items():
        d_q = d_q[d_q[col].astype(str) == val]
    jeton = COUVERTURE.set(df)
    try:
        rep = _analyser(question, q, lg, d_q, mois_info, aeros, inconnus, mois, annees, ambigus, typ,
                        repartir, bool(extra))
    finally:
        COUVERTURE.reset(jeton)
    if extra and rep.type not in ("absent", "ambigu", "cause_inconnue"):
        lignes = "; ".join(f"{COLONNES_CATEGORIES[c][1 if lg == 'en' else 0]} = {v}" for c, v in extra.items())
        note = (f"**Additional filter**: {lignes} (rows of other values excluded)." if lg == "en"
                else f"**Filtre supplémentaire** : {lignes} (les lignes des autres valeurs sont exclues).")
        corps, mention = rep.texte.rsplit("\n\n", 1)
        rep.texte = f"{corps}\n\n{note}\n\n{mention}"
        rep.chiffres["filtres"] = extra
    return rep


def _analyser(question, q, lg, df, mois_info, aeros, inconnus, mois, annees, ambigus, typ, repartir, filtre_extra):
    """Suite de l'analyse, sur le DataFrame déjà restreint aux catégories demandées."""
    # 3. Cause : jamais inventée
    if a(R["cause"], q):
        return rep_cause(df, mois_info, aeros, mois, typ, lg)
    # 4. Information absente du fichier
    colonnes = " ".join(normaliser(c) for c in df.columns)
    for _, (motif, fr, en, racines) in ABSENTS.items():
        if a(motif, q) and not any(r_ in colonnes for r_ in racines):
            dispo = ", ".join(f"`{c}`" for c in df.columns if c not in ("ligne_fichier", "score_anomalie", "anomalie", "mois", "statut"))
            return assembler(lg, "absent", [
                f"This information ({en}) is not in the file, so I cannot give it and I do not estimate it. "
                f"Available columns: {dispo}." if lg == "en" else
                f"Cette information ({fr}) n'est pas dans le fichier : je ne peux pas la donner et je ne l'estime pas. "
                f"Colonnes disponibles : {dispo}."])
    if inconnus:
        liste = ", ".join(sorted(df["aerodrome"].unique()))
        return assembler(lg, "absent", [
            f"Aerodrome not found in the file: {', '.join(inconnus)}. No figure is available and none is estimated. "
            f"Aerodromes in the file: {liste}." if lg == "en" else
            f"Aérodrome absent du fichier : {', '.join(inconnus)}. Aucun chiffre n'est disponible et aucun n'est "
            f"estimé. Aérodromes présents : {liste}."])

    prevision = a(R["prevision"], q)
    # 5. Mois ambigus ou hors fichier
    if ambigus:
        noms = ", ".join((MOIS_EN[n - 1] if lg == "en" else core.NOMS_MOIS_FR[n - 1]) for n in ambigus)
        return rep_ambigue(df, mois_info, lg, f"Which year do you mean for {noms}? The file covers several years."
                           if lg == "en" else f"Quelle année pour {noms} ? Le fichier couvre plusieurs années.")
    disponibles = set(df["mois"].unique())
    absents = [m for m in mois if m not in disponibles]
    if absents and not prevision:
        cov = f"{lib_mois(min(disponibles), lg)} – {lib_mois(max(disponibles), lg)}"
        return assembler(lg, "absent", [
            f"No data for {', '.join(lib_mois(m, lg) for m in absents)} in the file (coverage: {cov}). "
            f"No estimate is made." if lg == "en" else
            f"Pas de données pour {', '.join(lib_mois(m, lg) for m in absents)} dans le fichier (couverture : {cov}). "
            f"Aucune estimation n'est faite."])

    periode_txt = None
    if not mois and annees:  # « en 2025 » : tous les mois disponibles de l'année
        mois = sorted(m for m in disponibles if int(m[:4]) in annees)
        if not mois:
            return assembler(lg, "absent", ["No data for this year in the file." if lg == "en"
                                            else "Pas de données pour cette année dans le fichier."])
        periode_txt = (f"year {', '.join(map(str, annees))} ({lib_mois(mois[0], lg)} to {lib_mois(mois[-1], lg)})"
                       if lg == "en" else
                       f"année {', '.join(map(str, annees))} ({lib_mois(mois[0], lg)} à {lib_mois(mois[-1], lg)})")
    if a(R["toute_periode"], q) and not mois:
        periode_txt = None

    # 6. Intentions
    if prevision:
        return rep_prevision(df, mois_info, aeros, [m for m in mois if m not in disponibles] or mois, typ, lg)
    if a(R["anomalie"], q):
        return rep_anomalies(df, mois_info, aeros, mois, typ, lg)
    if a(R["retard"], q):
        return rep_retard(df, mois_info, aeros, mois, typ, lg)

    comparer, evoluer = a(R["comparaison"], q), a(R["evolution"], q)
    if len(aeros) >= 2 and (comparer or evoluer or not a(R["total"], q)):
        return rep_comparaison_aeros(df, mois_info, aeros, mois, typ, lg, periode_txt)
    if len(mois) >= 2 and (comparer or evoluer) and periode_txt is None:
        return rep_comparaison_mois(df, mois_info, aeros, mois[0], mois[1], typ, lg, evolution=evoluer and not comparer)
    if evoluer and len(mois) < 2:
        complets = [m for m in sorted(filtrer(df, aeros, None, typ)["mois"].unique())
                    if mois_info["statut"].get(m) == "Complet"]
        if len(complets) >= 2:
            r = rep_comparaison_mois(df, mois_info, aeros, complets[0], complets[-1], typ, lg, evolution=True)
            note = ("First and last complete months of the file were used; incomplete months excluded." if lg == "en"
                    else "Premier et dernier mois complets du fichier utilisés ; mois incomplets exclus.")
            r.texte = note + "\n\n" + r.texte
            return r
    if comparer:
        return rep_ambigue(df, mois_info, lg, "To compare, name two aerodromes or two months." if lg == "en"
                           else "Pour comparer, indiquez deux aérodromes ou deux mois.")

    if len(mois) >= 2 and periode_txt is None:  # « de janvier à mars 2025 » : période inclusive
        debut, fin = sorted(mois[:2])
        mois = sorted(m for m in disponibles if debut <= m <= fin)
        periode_txt = (f"{lib_mois(debut, lg)} to {lib_mois(fin, lg)}" if lg == "en"
                       else f"{lib_mois(debut, lg)} à {lib_mois(fin, lg)}")
    if aeros or mois or typ or repartir or filtre_extra or a(R["total"], q) or a(R["tous"], q):
        return rep_total(df, mois_info, aeros, mois, typ, lg, periode_txt, repartir)
    return rep_ambigue(df, mois_info, lg)
