"""Client Direct Line pour l'agent Copilot Studio (bibliothèque standard uniquement).

Deux façons d'obtenir le jeton Direct Line :
  - mode secret : variable d'environnement COPILOT_DIRECTLINE_SECRET -> POST .../v3/directline/tokens/generate
    avec « Authorization: Bearer <secret> » (Copilot Studio > Paramètres > Sécurité > Sécurité du canal web) ;
  - adresse de jeton (token endpoint) de l'agent publié sans authentification : COPILOT_TOKEN_URL ou l'interface.
Le mode secret est prioritaire. Ensuite : ouverture de la conversation -> envoi d'une activité « message » ->
lecture régulière des activités jusqu'à la réponse complète (les activités « typing » signalent que l'agent
travaille encore).

Le secret n'est lu que dans l'environnement, au moment de l'appel : il n'est jamais lu depuis un fichier,
jamais conservé (ni dans la conversation, ni dans la session Streamlit), jamais écrit dans un fichier, un journal,
une trace ou un message d'erreur, et jamais affiché.

Seule la question de l'utilisateur est envoyée : aucune donnée du fichier ne quitte l'application.
Toutes les erreurs sont converties en ErreurAgent, avec un message clair en français.
"""
import base64
import binascii
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

DIRECTLINE_DEFAUT = "https://directline.botframework.com"
VAR_SECRET = "COPILOT_DIRECTLINE_SECRET"
def _delai_max_env(defaut=180, mini=30, maxi=600):
    """Délai d'attente de la réponse : variable AMT_AGENT_DELAI (secondes, entre 30 et 600), sinon 180 s."""
    try:
        return min(max(int(os.environ.get("AMT_AGENT_DELAI", defaut)), mini), maxi)
    except ValueError:
        return defaut


DELAI_MAX = _delai_max_env()  # secondes : délai maximal pour une réponse complète (envoi + lectures)
INTERVALLE = 2.0         # secondes entre deux lectures des activités (avec le watermark)
SILENCE_FIN = 5.0        # secondes sans nouvelle activité après la réponse de l'agent : réponse complète
DELAI_REQUETE = 30       # secondes : jeton et ouverture de conversation
DELAI_LECTURE = 60       # secondes : envoi de la question et lecture des activités (l'agent peut être lent)
ECHECS_RESEAU_MAX = 3    # échecs réseau réels consécutifs avant d'abandonner pendant l'attente
DUREE_JETON = 25 * 60    # secondes avant de rafraîchir le jeton de conversation


class ErreurAgent(Exception):
    """Erreur présentable à l'utilisateur (message en français)."""


class DelaiReseau(ErreurAgent):
    """Une requête n'a pas répondu dans son délai : l'agent est peut-être seulement lent (non fatal en attente)."""


class ErreurReseau(ErreurAgent):
    """Le service est réellement injoignable (connexion refusée, nom inconnu, coupure...)."""


class ErreurServeur(ErreurAgent):
    """Erreur HTTP 5xx : peut être passagère."""


class ReponseEnRetard(ErreurAgent):
    """Délai dépassé sans réponse : l'agent travaille peut-être encore. La réponse pourra être récupérée plus
    tard dans la même conversation avec attendre_reponse(conv, id_envoi)."""

    def __init__(self, message, id_envoi):
        super().__init__(message)
        self.id_envoi = id_envoi


@dataclass
class Conversation:
    base: str                  # .../v3/directline
    id: str
    jeton: str = field(repr=False)   # jamais affiché ni journalisé
    utilisateur: str = ""
    watermark: str | None = None
    obtenu_le: float = field(default_factory=time.time)


@dataclass
class ReponseAgent:
    textes: list               # textes des messages de l'agent, tels quels
    pieces_jointes: list       # types des pièces jointes non textuelles (cartes...)
    conversation_id: str
    duree: float
    complete: bool = True      # False : délai maximal atteint alors que l'agent travaillait encore
    fichiers: list = field(default_factory=list)  # pièces jointes brutes : {"nom", "type", "url"|"contenu"}
    id_envoi: str | None = None  # identifiant de la question : sert à récupérer la suite d'une réponse incomplète


MENTION_INDISPONIBLE = "fichier non disponible depuis cette interface"
TYPES_TEXTE = ("text/", "application/json", "application/xml", "application/csv")


def nom_sur(nom):
    """Nom de fichier sans chemin ni caractère de contrôle (jamais utilisé tel quel pour écrire sur disque)."""
    nom = Path(str(nom or "").replace("\\", "/")).name
    nom = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", nom).strip(" .")
    return nom[:150] or "fichier_agent"


def _contenu_inline(contenu, type_):
    """Champ « content » d'une pièce jointe : texte (CSV, JSON...), base64 ou objet JSON -> octets."""
    if isinstance(contenu, (dict, list)):
        return json.dumps(contenu, ensure_ascii=False, indent=2).encode("utf-8")
    if not isinstance(contenu, str):
        return None
    if not type_.startswith(TYPES_TEXTE):
        try:
            return base64.b64decode(contenu, validate=True)
        except (binascii.Error, ValueError):
            pass
    return contenu.encode("utf-8")


def _fichier_joint(pj):
    """Pièce jointe d'une activité de l'agent : {"nom", "type", "url"} ou {"nom", "type", "contenu"},
    ou None pour une carte (affichage interactif, pas un fichier). Rien n'est téléchargé ici."""
    type_ = pj.get("contentType") or "application/octet-stream"
    if type_.startswith("application/vnd.microsoft.card."):
        return None
    f = {"nom": nom_sur(pj.get("name")), "type": type_}
    url = pj.get("contentUrl") or ""
    if url:
        return {**f, "url": url}
    contenu = _contenu_inline(pj.get("content"), type_)
    return {**f, "contenu": contenu} if contenu is not None else None


def _hote_local(url):
    return urllib.parse.urlparse(url).hostname in ("localhost", "127.0.0.1", "::1")


def verifier_adresse(url):
    """https obligatoire (http accepté seulement en local, pour un serveur de test)."""
    url = (url or "").strip()
    if not url:
        raise ErreurAgent("Adresse de jeton manquante : renseignez-la dans « Configuration de l'agent » "
                          "ou dans la variable d'environnement COPILOT_TOKEN_URL.")
    p = urllib.parse.urlparse(url)
    if p.scheme == "https" or (p.scheme == "http" and _hote_local(url)):
        return url
    raise ErreurAgent("Adresse de jeton refusée : elle doit commencer par https://.")


def mode_secret():
    """Vrai si la variable du secret existe (même vide : l'erreur « secret manquant » est alors signalée)."""
    return VAR_SECRET in os.environ


def _requete(methode, url, jeton=None, corps=None, refus=None, delai=DELAI_REQUETE):
    entetes = {"Accept": "application/json"}
    donnees = None
    if jeton:
        entetes["Authorization"] = f"Bearer {jeton}"
    if corps is not None:
        donnees = json.dumps(corps).encode("utf-8")
        entetes["Content-Type"] = "application/json"
    elif methode == "POST":
        donnees = b""
    req = urllib.request.Request(url, data=donnees, headers=entetes, method=methode)
    try:
        with urllib.request.urlopen(req, timeout=delai) as r:
            brut = r.read()
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (401, 403) and refus:
            raise ErreurAgent(refus.format(code=e.code)) from None
        if e.code in (401, 403):
            raise ErreurAgent(f"Accès refusé par le service de l'agent (HTTP {e.code}). Vérifiez l'adresse de "
                              "jeton et que l'agent est publié sans authentification.") from None
        if e.code == 404:
            raise ErreurAgent("Adresse introuvable (HTTP 404) : vérifiez l'adresse de jeton de l'agent.") from None
        if e.code >= 500:
            raise ErreurServeur(f"Le service de l'agent a renvoyé une erreur (HTTP {e.code}).") from None
        raise ErreurAgent(f"Le service de l'agent a renvoyé une erreur (HTTP {e.code}).") from None
    except (TimeoutError, socket.timeout):
        raise DelaiReseau(f"Le service de l'agent n'a pas répondu en {delai:g} secondes.") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            raise DelaiReseau(f"Le service de l'agent n'a pas répondu en {delai:g} secondes.") from None
        raise ErreurReseau(f"Impossible de joindre le service de l'agent ({e.reason}). "
                           "Vérifiez la connexion Internet et l'adresse.") from None
    except OSError as e:
        raise ErreurReseau(f"Impossible de joindre le service de l'agent ({e.strerror or type(e).__name__}). "
                           "Vérifiez la connexion Internet et l'adresse.") from None
    if not brut:
        return {}
    try:
        return json.loads(brut)
    except ValueError:
        raise ErreurAgent("Réponse illisible du service de l'agent (JSON attendu).") from None


TAILLE_MAX_FICHIER = 20 * 1024 * 1024   # octets : fichier joint par l'agent


def fichier_directline(conv, url):
    """Vrai si l'adresse est une pièce jointe hébergée par Direct Line (même domaine que la conversation ou
    *.botframework.com) : elle se télécharge avec le jeton de la conversation, jamais ouverte dans le navigateur."""
    u, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(conv.base)
    if u.scheme != b.scheme or u.scheme not in ("https", "http"):
        return False
    if u.scheme == "http" and not _hote_local(url):
        return False
    return u.netloc == b.netloc or (u.scheme == "https" and (u.hostname or "").endswith(".botframework.com"))


def _donnees_data(url, taille_max):
    entete, _, donnees = url.partition(",")
    if len(donnees) > taille_max * 4 // 3 + 4:
        raise ErreurAgent(f"Fichier joint trop volumineux (plus de {taille_max // (1024 * 1024)} Mo).")
    try:
        if entete.endswith(";base64"):
            return base64.b64decode(donnees, validate=True)
        return urllib.parse.unquote_to_bytes(donnees)
    except (binascii.Error, ValueError):
        raise ErreurAgent("Fichier joint illisible.") from None


def telecharger_fichier(conv, url, taille_max=TAILLE_MAX_FICHIER, delai=DELAI_LECTURE):
    """Contenu d'une pièce jointe désignée par une adresse :
    - data: décodée sur place ;
    - Direct Line : téléchargée avec le jeton de la conversation (en-tête Authorization) ; si le jeton contenu
      dans l'adresse (?t=...) est refusé, nouvel essai sans lui ;
    - autre https:// : téléchargée sans aucun jeton.
    Les messages d'erreur ne contiennent jamais l'adresse (elle peut porter un jeton) ni le jeton."""
    if url.startswith("data:"):
        return _donnees_data(url, taille_max)
    if url.startswith("sandbox:"):
        raise ErreurAgent(MENTION_INDISPONIBLE.capitalize() + " (fichier resté dans l'environnement de l'agent).")
    avec_jeton = fichier_directline(conv, url)
    if not avec_jeton and not url.startswith("https://"):
        raise ErreurAgent("Fichier joint refusé : adresse non sécurisée (https:// attendu).")
    u = urllib.parse.urlsplit(url)
    sans_t = urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "t"])))
    code = None
    for essai in dict.fromkeys([url, sans_t] if avec_jeton else [url]):
        entetes = {"Authorization": f"Bearer {conv.jeton}"} if avec_jeton else {}
        req = urllib.request.Request(essai, headers=entetes)
        try:
            with urllib.request.urlopen(req, timeout=delai) as r:
                contenu = r.read(taille_max + 1)
        except urllib.error.HTTPError as e:
            e.close()
            code = e.code
            if e.code in (400, 401, 403):  # Direct Line : « Invalid token or secret » si le jeton t= est périmé
                continue
            break
        except (TimeoutError, socket.timeout):
            raise DelaiReseau("Le fichier joint n'a pas pu être téléchargé à temps.") from None
        except (urllib.error.URLError, OSError):
            raise ErreurReseau("Impossible de télécharger le fichier joint (réseau).") from None
        if len(contenu) > taille_max:
            raise ErreurAgent(f"Fichier joint trop volumineux (plus de {taille_max // (1024 * 1024)} Mo).")
        return contenu
    raise ErreurAgent(f"Le fichier joint a été refusé par le serveur qui l'héberge (HTTP {code}).")


def recuperer_fichiers(conv, fichiers, taille_max=TAILLE_MAX_FICHIER):
    """Récupère tout de suite (le jeton expire) chaque pièce jointe : {"nom", "type", "contenu"} ou
    {"nom", "type", "erreur"}. Aucune adresse n'est conservée : elle peut porter un jeton."""
    resultat = []
    for f in fichiers:
        base_f = {"nom": nom_sur(f.get("nom")), "type": f.get("type") or "application/octet-stream"}
        try:
            contenu = f["contenu"] if f.get("contenu") is not None else telecharger_fichier(conv, f["url"], taille_max)
            if len(contenu) > taille_max:
                raise ErreurAgent(f"Fichier joint trop volumineux (plus de {taille_max // (1024 * 1024)} Mo).")
            resultat.append({**base_f, "contenu": contenu})
        except ErreurAgent as ex:
            resultat.append({**base_f, "erreur": str(ex)})
    return resultat


_LIEN_MD_SANDBOX = re.compile(r"\[([^\]]*)\]\(\s*<?(sandbox:[^)>\s]*)>?(?:\s+\"[^\"]*\")?\s*\)")
_LIEN_SANDBOX = re.compile(r"sandbox:[^\s)\]>\"'`]+")


def remplacer_liens_sandbox(texte, noms_joints):
    """Remplace les liens sandbox:/... (fichiers restés dans l'environnement de l'agent, inaccessibles au
    navigateur) par le nom de la pièce jointe correspondante en texte simple, sinon par la mention
    « fichier non disponible depuis cette interface ». Le reste du texte n'est pas modifié."""
    noms = {n.lower(): n for n in noms_joints}

    def cible(lien):
        fichier = urllib.parse.unquote(lien.rstrip(".,;:!?")).rsplit("/", 1)[-1].lower()
        return noms.get(fichier)

    def md(m):
        nom = cible(m.group(2))
        if nom:
            return nom
        libelle = m.group(1).strip()
        return f"{libelle} ({MENTION_INDISPONIBLE})" if libelle and "sandbox:" not in libelle else MENTION_INDISPONIBLE

    def brut(m):
        lien = m.group(0)
        fin = lien[len(lien.rstrip(".,;:!?")):]  # ponctuation finale gardée dans le texte
        return (cible(lien) or MENTION_INDISPONIBLE) + fin

    return _LIEN_SANDBOX.sub(brut, _LIEN_MD_SANDBOX.sub(md, texte))


def adresse_directline(token_url, directline_url=None):
    """Domaine Direct Line : variable ou paramètre explicite, sinon réglage régional de l'agent
    (Copilot Studio), sinon le domaine mondial."""
    if directline_url:
        return directline_url.rstrip("/")
    if _hote_local(token_url):  # serveur de test local : même adresse que le jeton
        p = urllib.parse.urlparse(token_url)
        return f"{p.scheme}://{p.netloc}"
    m = re.match(r"(https?://[^/]+/powervirtualagents)/", token_url)
    if m:
        try:
            r = _requete("GET", m.group(1) + "/regionalchannelsettings?api-version=2022-03-01-preview")
            dl = (r.get("channelUrlsById") or {}).get("directline")
            if dl:
                return dl.rstrip("/")
        except ErreurAgent:
            pass
    return DIRECTLINE_DEFAUT


def _jeton_par_secret(directline_url):
    """Échange le secret (lu dans l'environnement, ici seulement) contre un jeton Direct Line."""
    secret = os.environ.get(VAR_SECRET, "").strip()
    if not secret:
        raise ErreurAgent(f"Secret Direct Line manquant : la variable {VAR_SECRET} est vide.")
    domaine = (directline_url or DIRECTLINE_DEFAUT).rstrip("/")
    p = urllib.parse.urlparse(domaine)
    if not (p.scheme == "https" or (p.scheme == "http" and _hote_local(domaine))):
        raise ErreurAgent("Domaine Direct Line refusé : il doit commencer par https://.")
    try:
        r = _requete("POST", domaine + "/v3/directline/tokens/generate", secret,
                     refus="Secret Direct Line refusé (HTTP {code}) : vérifiez la valeur de " + VAR_SECRET
                           + " (Copilot Studio > Paramètres > Sécurité > Sécurité du canal web) et le domaine "
                           "Direct Line.")
    except ErreurAgent as e:  # garde-fou : le secret ne peut jamais apparaître dans un message
        raise ErreurAgent(str(e).replace(secret, "***")) from None
    finally:
        secret = None
    if not r.get("token"):
        raise ErreurAgent("Le service Direct Line n'a pas renvoyé de jeton pour ce secret.")
    return r["token"], domaine


def ouvrir_conversation(token_url, directline_url=None):
    if mode_secret():
        jeton, domaine = _jeton_par_secret(directline_url)
    else:
        if not (token_url or "").strip():
            raise ErreurAgent("Agent non configuré : lancez l'application avec la variable d'environnement "
                              + VAR_SECRET + " (secret Direct Line de l'agent).")
        token_url = verifier_adresse(token_url)
        r = _requete("GET", token_url)
        jeton = r.get("token")
        if not jeton:
            raise ErreurAgent("L'adresse de jeton n'a pas renvoyé de jeton Direct Line.")
        domaine = adresse_directline(token_url, directline_url)
    base = domaine + "/v3/directline"
    c = _requete("POST", base + "/conversations", jeton)
    if not c.get("conversationId"):
        raise ErreurAgent("Le service de l'agent n'a pas ouvert de conversation.")
    return Conversation(base=base, id=c["conversationId"], jeton=c.get("token") or jeton,
                        utilisateur="expert-" + uuid.uuid4().hex[:8])


def _rafraichir(conv):
    if time.time() - conv.obtenu_le > DUREE_JETON:
        r = _requete("POST", conv.base + "/tokens/refresh", conv.jeton)
        conv.jeton = r.get("token") or conv.jeton
        conv.obtenu_le = time.time()


def demander(conv, question, delai_max=None, intervalle=INTERVALLE, silence_fin=SILENCE_FIN,
             delai_lecture=DELAI_LECTURE, sur_etat=None, sur_progression=None):
    """Envoie la question (et rien d'autre) puis lit les activités (watermark) toutes les `intervalle` secondes,
    pendant `delai_max` secondes au plus.

    Réponse complète : un message de l'agent suivi d'une activité « event » ; ou, une fois un message reçu,
    `silence_fin` secondes sans nouvelle activité (et l'agent n'est pas en train d'écrire : « typing »).
    Un délai de requête dépassé n'est pas une erreur : l'agent peut être lent, on continue de lire.
    Seuls des échecs réseau réels et répétés donnent « Impossible de joindre le service ».
    Délai dépassé sans réponse : ReponseEnRetard (la réponse reste récupérable par attendre_reponse)."""
    debut = time.time()
    _rafraichir(conv)
    url_act = f"{conv.base}/conversations/{urllib.parse.quote(conv.id)}/activities"
    id_envoi = None
    try:
        envoi = _requete("POST", url_act, conv.jeton, {
            "type": "message", "from": {"id": conv.utilisateur, "name": "Expert"},
            "locale": "fr-FR", "text": question}, delai=delai_lecture)
        id_envoi = envoi.get("id")
    except DelaiReseau:
        pass  # l'envoi a pu aboutir alors que l'accusé de réception tarde : on lit les activités
    return attendre_reponse(conv, id_envoi, delai_max, intervalle, silence_fin, delai_lecture, sur_etat,
                            sur_progression, debut)


def attendre_reponse(conv, id_envoi, delai_max=None, intervalle=INTERVALLE, silence_fin=SILENCE_FIN,
                     delai_lecture=DELAI_LECTURE, sur_etat=None, sur_progression=None, debut=None):
    """Lit les activités de la conversation jusqu'à la réponse complète de l'agent à la question `id_envoi`.
    Sert aussi à récupérer une réponse arrivée après le délai : la lecture reprend au watermark, rien n'est
    renvoyé à l'agent. sur_progression(secondes) est appelé à chaque lecture (affichage du temps d'attente)."""
    delai_max = DELAI_MAX if delai_max is None else delai_max
    debut = time.time() if debut is None else debut
    _rafraichir(conv)
    url_act = f"{conv.base}/conversations/{urllib.parse.quote(conv.id)}/activities"
    textes, pieces, derniere_activite, ecrit_encore, fin_signalee, echecs = [], [], None, False, False, 0
    fichiers = []
    while True:
        params = f"?watermark={urllib.parse.quote(conv.watermark)}" if conv.watermark else ""
        try:
            r = _requete("GET", url_act + params, conv.jeton, delai=delai_lecture)
            echecs = 0
        except DelaiReseau:
            r = {}  # lecture lente : pas une erreur, on relit au tour suivant
        except (ErreurReseau, ErreurServeur):
            echecs += 1
            if echecs >= ECHECS_RESEAU_MAX:
                raise
            r = {}
        conv.watermark = r.get("watermark") or conv.watermark
        for a in r.get("activities", []):
            if (a.get("from") or {}).get("id") == conv.utilisateur:
                continue  # écho de la question
            if a.get("replyToId") and id_envoi and a["replyToId"] != id_envoi:
                continue  # réponse à une question précédente
            derniere_activite = time.time()
            typ = a.get("type")
            if typ == "typing":
                ecrit_encore = True
                if sur_etat:
                    sur_etat("reflechit")
            elif typ == "message":
                ecrit_encore = False
                if a.get("text"):
                    textes.append(a["text"])
                for pj in a.get("attachments") or []:
                    f = _fichier_joint(pj)
                    if f:
                        fichiers.append(f)
                    else:
                        pieces.append(pj.get("contentType", "inconnu"))
            elif typ in ("event", "endOfConversation") and (textes or pieces or fichiers):
                fin_signalee, ecrit_encore = True, False  # message de l'agent puis « event » : réponse complète
        if textes or pieces or fichiers:
            if fin_signalee or (not ecrit_encore and time.time() - derniere_activite >= silence_fin):
                return ReponseAgent(textes, pieces, conv.id, round(time.time() - debut, 1), fichiers=fichiers,
                                    id_envoi=id_envoi)
        if time.time() - debut >= delai_max:
            if textes or pieces or fichiers:  # réponse partielle reçue : on la montre, signalée comme incomplète
                return ReponseAgent(textes, pieces, conv.id, round(time.time() - debut, 1), complete=False,
                                    fichiers=fichiers, id_envoi=id_envoi)
            etat = " (il indiquait encore être en train de répondre)" if ecrit_encore else ""
            raise ReponseEnRetard(f"Délai dépassé : l'agent n'a pas répondu en {delai_max:g} secondes{etat}. "
                                  "Il travaille peut-être encore : sa réponse pourra être récupérée.", id_envoi)
        if sur_progression:
            sur_progression(int(time.time() - debut))
        time.sleep(intervalle)


def configuration_env():
    return os.environ.get("COPILOT_TOKEN_URL", ""), os.environ.get("COPILOT_DIRECTLINE_URL", "")
