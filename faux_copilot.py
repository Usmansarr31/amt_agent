"""Faux serveur Direct Line (adresse de jeton + API v3) pour tester l'intégration Copilot Studio sans Internet.

Lancement :  python faux_copilot.py [port]          (port 8765 par défaut)
Puis, dans l'application : adresse de jeton  http://localhost:8765/token

Mode secret : POST /v3/directline/tokens/generate avec « Authorization: Bearer <secret> » ; secret accepté :
variable FAUX_COPILOT_SECRET (par défaut « faux-secret-de-test »). Pour l'application :
    COPILOT_DIRECTLINE_SECRET=faux-secret-de-test COPILOT_DIRECTLINE_URL=http://localhost:8765 streamlit run app.py

Comportement, comme un agent Copilot Studio : « typing », premier message, de nouveau « typing », second
message 3 s plus tard, puis une activité « event » au texte vide (fin de réponse). L'agent se souvient du nombre
de questions posées dans la conversation. Questions spéciales pour les tests :
  « lent »        -> accusé de l'envoi retardé de 3 s et lectures retardées de 1,5 s pendant 4 s, réponse à 4 s
                     (comme l'agent réel, qui répond après plus de 20 s) ;
  « sans event »  -> réponse sans activité « event » finale (fin détectée par le silence) ;
  « silence »     -> aucune réponse ;  « reflechit » -> « typing » toutes les secondes sans jamais répondre ;
  « panne »       -> erreur HTTP 500 à l'envoi.
GET /_recus renvoie les activités reçues (pour vérifier que seule la question est envoyée).
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERROU = threading.Lock()
CONVERSATIONS = {}   # id -> {"activites": [(visible_a, activite)], "questions": int}
RECUS = []
SECRET = os.environ.get("FAUX_COPILOT_SECRET", "faux-secret-de-test")


def _ajouter(conv, activite, delai):
    with VERROU:
        CONVERSATIONS[conv]["activites"].append((time.time() + delai, activite))


class Gestionnaire(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, corps):
        brut = json.dumps(corps).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(brut)))
            self.end_headers()
            self.wfile.write(brut)
        except (BrokenPipeError, ConnectionResetError):
            pass  # le client a abandonné cette requête (délai de requête dépassé) : normal en test

    def _autorise(self):
        return self.headers.get("Authorization", "").startswith("Bearer faux-jeton")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/token":
            return self._json(200, {"token": "faux-jeton-" + uuid.uuid4().hex[:6], "expires_in": 3600})
        if u.path == "/_recus":
            with VERROU:
                return self._json(200, RECUS)
        m = re.fullmatch(r"/v3/directline/conversations/([^/]+)/activities", u.path)
        if m:
            if not self._autorise():
                return self._json(403, {"error": "jeton"})
            conv = m.group(1)
            if conv not in CONVERSATIONS:
                return self._json(404, {"error": "conversation"})
            with VERROU:
                lent_jusqu_a = CONVERSATIONS[conv].get("lent_jusqu_a", 0)
            if time.time() < lent_jusqu_a:
                time.sleep(1.5)  # lecture lente : le client ne doit pas en faire une erreur fatale
            depuis = int((parse_qs(u.query).get("watermark") or ["0"])[0])
            maintenant = time.time()
            with VERROU:
                visibles = [a for t, a in CONVERSATIONS[conv]["activites"] if t <= maintenant]
            return self._json(200, {"activities": visibles[depuis:], "watermark": str(len(visibles))})
        self._json(404, {"error": "inconnu"})

    def do_POST(self):
        u = urlparse(self.path)
        longueur = int(self.headers.get("Content-Length") or 0)
        corps = json.loads(self.rfile.read(longueur) or b"{}")
        if u.path == "/v3/directline/conversations":
            if not self._autorise():
                return self._json(403, {"error": "jeton"})
            conv = "conv-" + uuid.uuid4().hex[:8]
            with VERROU:
                CONVERSATIONS[conv] = {"activites": [], "questions": 0}
            return self._json(201, {"conversationId": conv, "token": "faux-jeton-conv", "expires_in": 3600})
        if u.path == "/v3/directline/tokens/generate":
            if self.headers.get("Authorization", "") != f"Bearer {SECRET}":
                return self._json(403, {"error": {"code": "BadArgument", "message": "secret invalide"}})
            return self._json(200, {"token": "faux-jeton-genere", "conversationId": "inutilise", "expires_in": 1800})
        if u.path == "/v3/directline/tokens/refresh":
            return self._json(200, {"token": "faux-jeton-rafraichi"})
        m = re.fullmatch(r"/v3/directline/conversations/([^/]+)/activities", u.path)
        if m and m.group(1) in CONVERSATIONS:
            if not self._autorise():
                return self._json(403, {"error": "jeton"})
            conv, texte = m.group(1), corps.get("text", "")
            with VERROU:
                RECUS.append(corps)
            if "panne" in texte.lower():
                return self._json(500, {"error": "panne simulée"})
            ident = "act-" + uuid.uuid4().hex[:8]
            _ajouter(conv, {**corps, "id": ident}, 0)  # écho de l'activité de l'utilisateur, comme Direct Line
            bot = {"id": "agent", "name": "Agent AMT (faux serveur)"}
            if "lent" in texte.lower():
                with VERROU:
                    CONVERSATIONS[conv]["lent_jusqu_a"] = time.time() + 4
                _ajouter(conv, {"type": "message", "from": bot, "replyToId": ident,
                                "text": f"**Réponse lente du faux serveur** à : « {texte} »"}, 4.0)
                _ajouter(conv, {"type": "event", "from": bot, "replyToId": ident, "text": "",
                                "name": "fin"}, 4.2)
                time.sleep(3)  # accusé de réception de l'envoi retardé
                return self._json(200, {"id": ident})
            if "reflechit" in texte.lower():
                bot = {"id": "agent", "name": "Agent AMT (faux serveur)"}
                for k in range(120):
                    _ajouter(conv, {"type": "typing", "from": bot, "replyToId": ident}, 0.5 + k)
            elif "silence" not in texte.lower():
                with VERROU:
                    CONVERSATIONS[conv]["questions"] += 1
                    n = CONVERSATIONS[conv]["questions"]
                bot = {"id": "agent", "name": "Agent AMT (faux serveur)"}
                _ajouter(conv, {"type": "typing", "from": bot, "replyToId": ident}, 0.5)
                _ajouter(conv, {"type": "message", "from": bot, "replyToId": ident,
                                "text": f"**Réponse du faux serveur de test** à : « {texte} »"}, 1.5)
                _ajouter(conv, {"type": "typing", "from": bot, "replyToId": ident}, 2.0)
                _ajouter(conv, {"type": "message", "from": bot, "replyToId": ident,
                                "text": f"Question n°{n} dans cette conversation ({conv})."}, 5.0)
                if "sans event" not in texte.lower():
                    _ajouter(conv, {"type": "event", "from": bot, "replyToId": ident, "text": "",
                                    "name": "fin"}, 5.2)
            return self._json(200, {"id": ident})
        self._json(404, {"error": "inconnu"})


def demarrer(port=8765):
    serveur = ThreadingHTTPServer(("127.0.0.1", port), Gestionnaire)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    return serveur


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Faux serveur Direct Line : adresse de jeton http://localhost:{port}/token")
    ThreadingHTTPServer(("127.0.0.1", port), Gestionnaire).serve_forever()
