#!/usr/bin/env python3
"""Assemble le site à partir de source/template.html : index.html, manifest.webmanifest et sw.js.

Usage (depuis la racine du dépôt) :
    python3 source/build.py
    python3 source/build.py --apercu FICHIER.html   # en plus, une copie autonome avec les photos intégrées

Le logo, la photo du maillot, l'icône et les logos Android/Apple sont intégrés directement dans
index.html : la page s'affiche d'un seul tenant. Le dernier état du calendrier (data/sorties.json)
y est aussi intégré, en secours : au chargement, la page relit data/sorties.json.
"""
import argparse
import base64
import json
import re
from pathlib import Path

VERSION = "1.3.0"
DATE = "22 octobre 2026"     # date affichée avec la version, dans la section « Application mobile »

RACINE = Path(__file__).resolve().parent.parent
SOURCE = RACINE / "source"
ASSETS = SOURCE / "assets"
PHOTOS_SOURCE = SOURCE / "photos"   # photos originales : JPEG, PNG ou HEIC d'iPhone (+ legendes.json facultatif)
PHOTOS_WEB = RACINE / "photos"      # versions allégées publiées : grande taille et vignettes (photos/mini/)
FORMATS_PHOTO = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}


def b64(chemin):
    return base64.b64encode(chemin.read_bytes()).decode()


def preparer_photos():
    """Allège les photos de source/photos/ pour le web et renvoie la liste affichée par la galerie.

    legendes.json (facultatif) associe un nom de fichier à sa légende : {"sortie-mai.jpg": "Sortie du 12 mai"}.
    Les photos sont affichées dans l'ordre alphabétique des noms de fichiers.
    """
    originaux = sorted(p for p in PHOTOS_SOURCE.glob("*") if p.suffix.lower() in FORMATS_PHOTO) if PHOTOS_SOURCE.exists() else []
    if not originaux:
        return []
    from PIL import Image, ImageOps            # pip install -r source/requirements.txt
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()                  # photos HEIC d'iPhone
    except ImportError:
        pass
    legendes_f = PHOTOS_SOURCE / "legendes.json"
    legendes = json.loads(legendes_f.read_text(encoding="utf-8")) if legendes_f.exists() else {}
    (PHOTOS_WEB / "mini").mkdir(parents=True, exist_ok=True)
    liste, publies = [], set()
    for original in originaux:
        nom = re.sub(r"[^a-z0-9-]+", "-", original.stem.lower()).strip("-") + ".jpg"
        grande, mini = PHOTOS_WEB / nom, PHOTOS_WEB / "mini" / nom
        if not grande.exists() or grande.stat().st_mtime < original.stat().st_mtime:
            im = ImageOps.exif_transpose(Image.open(original)).convert("RGB")
            im.thumbnail((1600, 1600), Image.LANCZOS)
            im.save(grande, "JPEG", quality=82, optimize=True, progressive=True)
            im.thumbnail((640, 640), Image.LANCZOS)
            im.save(mini, "JPEG", quality=76, optimize=True, progressive=True)
        with Image.open(grande) as g, Image.open(mini) as m:
            liste.append({"f": nom, "w": g.width, "h": g.height, "mw": m.width, "mh": m.height,
                          "l": legendes.get(original.name, "")})
        publies.add(nom)
    for ancien in list(PHOTOS_WEB.glob("*.jpg")) + list((PHOTOS_WEB / "mini").glob("*.jpg")):
        if ancien.name not in publies:          # photo retirée de source/photos/
            ancien.unlink()
    return liste


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


def assembler(photos):
    modele = (SOURCE / "template.html").read_text(encoding="utf-8")
    donnees = json.loads((RACINE / "data" / "sorties.json").read_text(encoding="utf-8"))
    donnees_js = json.dumps(donnees, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    corps = (modele.replace("__LOGO__", b64(ASSETS / "logo-quint-fonsegrives.png"))
                   .replace("__MAILLOT__", b64(ASSETS / "maillot-ccqf.jpg"))
                   .replace("__ICON__", b64(RACINE / "icons" / "icon-192.png"))
                   .replace("__DATA__", donnees_js)
                   .replace("__PHOTOS__", json.dumps(photos, ensure_ascii=False).replace("<", "\\u003c"))
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
    return ("<!doctype html>\n<html lang=\"fr\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            f"{APPHEAD}\n{tete}{RESET}\n</head>\n<body>\n{contenu.strip()}\n{SW_REG}\n</body>\n</html>\n")


def main():
    parser = argparse.ArgumentParser(description="Assemble le site du CCQF.")
    parser.add_argument("--apercu", help="écrit aussi une copie autonome de la page, photos intégrées, pour la montrer hors ligne")
    args = parser.parse_args()

    photos = preparer_photos()
    en_ligne = [dict(p, m=f"photos/mini/{p['f']}", u=f"photos/{p['f']}") for p in photos]
    page = assembler(en_ligne)
    (RACINE / "index.html").write_text(page, encoding="utf-8")
    (RACINE / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (RACINE / "sw.js").write_text(SW % {"v": VERSION}, encoding="utf-8")
    print(f"Site assemblé, version {VERSION} : index.html ({len(page.encode()) // 1024} Ko), "
          f"{len(photos)} photo(s), manifest.webmanifest, sw.js")
    if args.apercu:
        integrees = [dict(p, m="data:image/jpeg;base64," + b64(PHOTOS_WEB / "mini" / p["f"]),
                          u="data:image/jpeg;base64," + b64(PHOTOS_WEB / p["f"])) for p in photos]
        apercu = assembler(integrees)
        Path(args.apercu).write_text(apercu, encoding="utf-8")
        print(f"Aperçu autonome : {args.apercu} ({len(apercu.encode()) // 1024} Ko)")


if __name__ == "__main__":
    main()
