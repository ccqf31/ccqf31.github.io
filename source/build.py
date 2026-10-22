#!/usr/bin/env python3
"""Assemble le site à partir de source/template.html : index.html, manifest.webmanifest et sw.js.

Usage (depuis la racine du dépôt) :
    python3 source/build.py

Le logo, la photo du maillot, l'icône et les logos Android/Apple sont intégrés directement dans
index.html : la page s'affiche d'un seul tenant. Le dernier état du calendrier (data/sorties.json)
y est aussi intégré, en secours : au chargement, la page relit data/sorties.json.
"""
import base64
import json
import re
from pathlib import Path

VERSION = "1.3.0"
DATE = "22 octobre 2026"     # date affichée avec la version, dans la section « Application mobile »

RACINE = Path(__file__).resolve().parent.parent
SOURCE = RACINE / "source"
ASSETS = SOURCE / "assets"


def b64(chemin):
    return base64.b64encode(chemin.read_bytes()).decode()


def logo_svg(nom):
    chemin = re.search(r'<path d="[^"]+"/>', (ASSETS / f"{nom}.svg").read_text()).group(0)
    return f'<svg viewBox="0 0 24 24" aria-hidden="true">{chemin}</svg>'


# Balises de l'application installable (Android, iPhone) et icônes
APPHEAD = """<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="icons/favicon-32.png">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="CCQF">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#007E4E" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0A4E34" media="(prefers-color-scheme: dark)">"""

RESET = """<style>
/* Réinitialisation de base */
html{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
body{margin:0}
img{max-width:100%}
[hidden]{display:none!important}
</style>"""

SW_REG = """<script>
if("serviceWorker" in navigator&&(location.protocol==="https:"||location.hostname==="localhost"||location.hostname==="127.0.0.1")){
  navigator.serviceWorker.register("sw.js").catch(function(){});
}
</script>"""

MANIFEST = {
    "id": "./", "name": "CCQF · Cyclo-Club Quint-Fonsegrives", "short_name": "CCQF",
    "description": "Sorties, parcours et calendrier du CCQF, Cyclo-Club Quint-Fonsegrives.",
    "lang": "fr", "dir": "ltr", "start_url": "./index.html", "scope": "./", "display": "standalone",
    "background_color": "#F4F7F5", "theme_color": "#007E4E",
    "icons": [
        {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Service worker : réseau d'abord (le site reste à jour), copie locale si pas de réseau
SW = """/* Service worker du CCQF · Cyclo-Club Quint-Fonsegrives, version %(v)s */
const CACHE = "ccqf-%(v)s";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./data/sorties.json",
  "./icons/icon-192.png", "./icons/icon-512.png", "./icons/apple-touch-icon.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("./index.html")))
  );
});
"""


def main():
    modele = (SOURCE / "template.html").read_text(encoding="utf-8")
    donnees = json.loads((RACINE / "data" / "sorties.json").read_text(encoding="utf-8"))
    donnees_js = json.dumps(donnees, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    corps = (modele.replace("__LOGO__", b64(ASSETS / "logo-quint-fonsegrives.png"))
                   .replace("__MAILLOT__", b64(ASSETS / "maillot-ccqf.jpg"))
                   .replace("__ICON__", b64(RACINE / "icons" / "icon-192.png"))
                   .replace("__DATA__", donnees_js)
                   .replace("__VERSION__", VERSION).replace("__DATE__", DATE)
                   .replace("__ANDROID__", logo_svg("android")).replace("__APPLE__", logo_svg("apple")))
    reste = re.findall(r"__[A-Z]+__", re.sub(r"data:[^\"]+", "", corps))
    if reste:
        raise SystemExit(f"Repères non remplacés dans le modèle : {sorted(set(reste))}")

    # Le modèle commence par <title>, les métadonnées et la feuille de style : ils vont dans <head>
    fin_style = corps.index("</style>") + len("</style>")
    tete, contenu = corps[:fin_style], corps[fin_style:]
    tete = tete.replace('<meta name="claude:viewport-fit-sides" content="cover">\n', "")
    tete = re.sub(r'<meta name="theme-color"[^>]*>\n', "", tete)
    page = ("<!doctype html>\n<html lang=\"fr\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            f"{APPHEAD}\n{tete}{RESET}\n</head>\n<body>\n{contenu.strip()}\n{SW_REG}\n</body>\n</html>\n")

    (RACINE / "index.html").write_text(page, encoding="utf-8")
    (RACINE / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RACINE / "sw.js").write_text(SW % {"v": VERSION}, encoding="utf-8")
    print(f"Site assemblé, version {VERSION} : index.html ({len(page.encode()) // 1024} Ko), manifest.webmanifest, sw.js")


if __name__ == "__main__":
    main()
