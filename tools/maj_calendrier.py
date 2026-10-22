#!/usr/bin/env python3
"""Met à jour data/sorties.json à partir du calendrier Google « FRF Cyclo » et des traces OpenRunner.

Le site lit ce fichier à chaque ouverture : les 4 prochaines sorties du dimanche, leur lien Garmin,
leur lien OpenRunner, leur distance, leur dénivelé, leur profil d'altitude et leur tracé.

Usage :
    python3 tools/maj_calendrier.py                 # lit le calendrier en ligne
    python3 tools/maj_calendrier.py --ics fichier   # lit un export .ics local (essais)

Le fichier n'est réécrit que si les données ont changé. En cas d'erreur (réseau, calendrier
indisponible), le fichier existant est conservé tel quel et le script se termine en erreur.
"""
import argparse
import datetime
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

ICS_URL = "https://calendar.google.com/calendar/ical/frfcyclo%40gmail.com/public/basic.ics"
OPENRUNNER_API = "https://api.openrunner.com/api/v2/routes/{}"
OPENRUNNER_PAGE = "https://www.openrunner.com/route-details/{}"
TZ = ZoneInfo("Europe/Paris")
NB_SORTIES = 4                                   # sorties du dimanche détaillées dans la page
JOURS_A_VENIR = 60                               # fenêtre de la liste des événements à venir
HISTORIQUE = datetime.timedelta(days=3 * 365)    # parcours roulés depuis 3 ans dans la bibliothèque
SORTIE = Path(__file__).resolve().parent.parent / "data" / "sorties.json"

# « Parcours 012H v2.1 Garmin », « Parcours 57 T L », « Parcours Garmin 04_H »…
NUMERO = re.compile(r"[Pp]arcou?rs?t?\s*(?:Garmin\s*)?0*(\d{1,3})\s*[-_ ]?\s*([TH])", re.I)
VERSION = re.compile(r"\bv\s?(\d+(?:\.\d+)?)", re.I)
ID_OPENRUNNER = re.compile(r"(?:route-details/|[?&]id=|/r/)(\d+)")
APERCU_ALTITUDES = 160      # points du petit profil d'altitude de chaque parcours
APERCU_TOLERANCE = 0.00025  # simplification du tracé (en degrés, environ 25 m)
APERCU_VERSION = 2          # à augmenter si le calcul des aperçus change : ils seront recalculés


def telecharger(url, essais=3):
    for n in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cyclo-quint-fonsegrives-site"})
            with urllib.request.urlopen(req, timeout=30) as rep:
                return rep.read()
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503) or n == essais - 1:
                raise
        except urllib.error.URLError:
            if n == essais - 1:
                raise
        time.sleep(5 * (n + 1))


def desenrober(lien):
    """https://www.google.com/url?q=https://www.openrunner.com/… → https://www.openrunner.com/…"""
    if "google." in lien and "q=" in lien:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(lien).query).get("q")
        if q:
            return q[0]
    return lien


def encoder(valeurs):
    """Encodage « polyline » (le même que celui d'OpenRunner), pour des listes de tuples d'entiers."""
    sortie, precedent = [], [0] * len(valeurs[0])
    for point in valeurs:
        for i, v in enumerate(point):
            d, precedent[i] = v - precedent[i], v
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                sortie.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            sortie.append(chr(d + 63))
    return "".join(sortie)


def decoder(texte, dim):
    points, i, acc = [], 0, [0] * dim
    while i < len(texte):
        for k in range(dim):
            r = sh = 0
            while True:
                b = ord(texte[i]) - 63
                i += 1
                r |= (b & 0x1F) << sh
                sh += 5
                if b < 0x20:
                    break
            acc[k] += ~(r >> 1) if r & 1 else r >> 1
        points.append(tuple(acc))
    return points


def simplifier(points, tolerance):
    """Douglas-Peucker : garde la forme du tracé avec beaucoup moins de points."""
    if len(points) < 3:
        return points
    garder, pile = {0, len(points) - 1}, [(0, len(points) - 1)]
    while pile:
        a, b = pile.pop()
        (ya, xa), (yb, xb) = points[a], points[b]
        dx, dy, loin, idx = xb - xa, yb - ya, 0, None
        norme = (dx * dx + dy * dy) ** 0.5
        for i in range(a + 1, b):
            y, x = points[i]
            # Un parcours en boucle revient à son point de départ : on mesure alors la distance au point lui-même
            d = abs(dy * (x - xa) - dx * (y - ya)) / norme if norme > 1e-9 else ((x - xa) ** 2 + (y - ya) ** 2) ** 0.5
            if d > loin:
                loin, idx = d, i
        if idx is not None and loin > tolerance:
            garder.add(idx)
            pile += [(a, idx), (idx, b)]
    return [points[i] for i in sorted(garder)]


def apercu(trace):
    """Version allégée d'une trace pour les fiches de la bibliothèque : profil et tracé simplifiés."""
    alt = [v for (v,) in decoder(trace["el"], 1)]
    pas = max(1, -(-len(alt) // APERCU_ALTITUDES))
    alt_reduit = alt[::pas]
    pts = [(la / 1e5, lo / 1e5) for la, lo in decoder(trace["shape"], 2)]
    pts = simplifier(pts, APERCU_TOLERANCE)
    return {
        "v": APERCU_VERSION, "len": trace["len"], "up": trace["ascent"], "min": min(alt), "max": max(alt),
        "st": trace["step"] * pas, "pe": encoder([(v,) for v in alt_reduit]),
        "ps": encoder([(round(la * 1e5), round(lo * 1e5)) for la, lo in pts]),
    }


def lire_evenement(ev):
    debut = ev.get("DTSTART").dt
    if not isinstance(debut, datetime.datetime):
        return None                                   # événement « journée entière » : ignoré
    titre = " ".join(str(ev.get("SUMMARY", "")).split())
    brut = html.unescape(str(ev.get("DESCRIPTION", "") or ""))
    texte = titre + " " + " ".join(re.sub(r"<[^>]+>", " ", brut).split())
    liens = re.findall(r"https?://[^\s\"<>]+", brut)
    garmin = [l for l in liens if "garmin" in l]
    openrunner = [desenrober(l) for l in liens if "openrunner" in l]
    km = re.search(r"(\d{2,3}(?:[.,]\d{1,3})?)\s*[Kk]m", texte)
    dplus = re.search(r"(?:d\s*:\s*|-\s*)(\d{3,4})\s*m\b|(\d{3,4})\s*dev", texte)
    km = float(km.group(1).replace(",", ".")) if km else None
    dplus = int(dplus.group(1) or dplus.group(2)) if dplus else None
    variantes = {m.group(1) or m.group(2) for m in re.finditer(r"[_ ]([LC])(?:\b|_)|\b[TH]([LC])\b", titre)}
    return {
        "debut": debut.astimezone(TZ), "titre": titre,
        "garmin": garmin[-1] if garmin else None, "openrunner": openrunner[-1] if openrunner else None,
        "km": km if km and 40 <= km <= 200 else None, "dp": dplus if dplus and 100 <= dplus <= 2500 else None,
        "variantes": variantes,
    }


def lire_openrunner(lien):
    m = ID_OPENRUNNER.search(lien or "")
    if not m:
        return None
    route = json.loads(telecharger(OPENRUNNER_API.format(m.group(1))))["data"]
    elev = route["elevation"]
    return {
        "name": route["name"], "len": route["length"], "step": elev.get("interval", 100),
        "ascent": elev["ascent"], "el": elev["full_encoded"], "shape": route["shape"]["full_encoded"],
        "or": OPENRUNNER_PAGE.format(m.group(1)),
    }


def construire(ics, maintenant, ancien=None):
    cal = icalendar.Calendar.from_ical(ics)
    tous = recurring_ical_events.of(cal).between(maintenant - HISTORIQUE, maintenant + datetime.timedelta(days=400))
    # Tri complet (date, titre, lien) : deux événements à la même heure gardent toujours le même ordre
    evenements = sorted(filter(None, (lire_evenement(e) for e in tous)),
                        key=lambda e: (e["debut"], e["titre"], e["garmin"] or "", e["openrunner"] or ""))

    # Bibliothèque des parcours numérotés (une entrée par numéro et par sens)
    biblio = {}
    for e in evenements:
        m = NUMERO.search(e["titre"])
        if not m:
            continue
        cle = m.group(1).zfill(3) + m.group(2).upper()
        p = biblio.setdefault(cle, {"n": cle[:3], "s": cle[3], "v": set(), "km": None, "dp": None,
                                    "g": None, "o": None, "next": None, "last": None})
        p["v"] |= e["variantes"]
        for champ, cle_ev in (("km", "km"), ("dp", "dp"), ("g", "garmin"), ("o", "openrunner")):
            if e[cle_ev]:
                p[champ] = e[cle_ev]
        if e["debut"] < maintenant:
            p["last"] = e["debut"]
        elif not p["next"]:
            p["next"] = e["debut"]

    # Événements à venir
    a_venir = [e for e in evenements if maintenant <= e["debut"] <= maintenant + datetime.timedelta(days=JOURS_A_VENIR)]
    dimanches = [e for e in a_venir if e["debut"].weekday() == 6 and NUMERO.search(e["titre"])][:NB_SORTIES]

    # Détails OpenRunner des prochaines sorties du dimanche
    profils = {}
    for e in dimanches:
        m = NUMERO.search(e["titre"])
        cle = m.group(1).zfill(3) + m.group(2).upper()
        lien = e["openrunner"] or biblio[cle]["o"]
        try:
            trace = lire_openrunner(lien)
        except Exception as err:  # une trace introuvable ne bloque pas la mise à jour
            print(f"Attention : trace OpenRunner illisible pour {e['titre']} ({lien}) : {err}", file=sys.stderr)
            trace = None
        if not trace:
            print(f"Attention : pas de trace OpenRunner pour {e['titre']}", file=sys.stderr)
            continue
        v_cal, v_or = VERSION.search(e["titre"]), VERSION.search(trace["name"])
        if v_cal and (not v_or or v_cal.group(1) != v_or.group(1)):
            print(f"Attention : « {e['titre']} » renvoie vers la trace OpenRunner « {trace['name']} » "
                  "(version différente) : vérifiez le lien.", file=sys.stderr)
        km, dp = round(trace["len"] / 1000, 1), trace["ascent"]
        e["km"], e["dp"], e["openrunner"] = km, dp, trace["or"]
        biblio[cle].update(km=km, dp=dp, o=trace["or"])
        profils[cle] = {k: trace[k] for k in ("name", "len", "step", "el", "shape", "or")}

    # Aperçus (profil + tracé) de tous les parcours de la bibliothèque, depuis OpenRunner.
    # Un aperçu déjà calculé pour le même lien OpenRunner est repris tel quel : pas de nouvel appel.
    deja = {p["o"]: p["pv"] for p in (ancien or {}).get("lib", [])
            if p.get("o") and p.get("pv", {}).get("v") == APERCU_VERSION}
    for cle, p in sorted(biblio.items()):
        if not p["o"] or not ID_OPENRUNNER.search(p["o"]):
            continue
        lien = OPENRUNNER_PAGE.format(ID_OPENRUNNER.search(p["o"]).group(1))
        if lien in deja:
            p["pv"] = deja[lien]
        else:
            try:
                p["pv"] = apercu(lire_openrunner(lien))
                time.sleep(0.3)
            except Exception as err:
                print(f"Attention : aperçu impossible pour le parcours {cle} ({lien}) : {err}", file=sys.stderr)
                continue
        p["o"] = lien
        p["km"], p["dp"] = round(p["pv"]["len"] / 1000, 1), p["pv"]["up"]

    jour = lambda d: d.strftime("%Y-%m-%d") if d else None
    return {
        "up": [{"t": e["titre"], "d": e["debut"].strftime("%Y-%m-%dT%H:%M"), "g": e["garmin"],
                "o": e["openrunner"], "km": e["km"], "dp": e["dp"]} for e in a_venir],
        "lib": [{"n": p["n"], "s": p["s"], "v": "".join(sorted(p["v"])), "km": p["km"], "dp": p["dp"],
                 "g": p["g"], "o": p["o"], "next": jour(p["next"]), "last": jour(p["last"]),
                 **({"pv": p["pv"]} if p.get("pv") else {})}
                for _, p in sorted(biblio.items())],
        "prof": profils,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ics", help="fichier .ics local à utiliser à la place du calendrier en ligne")
    parser.add_argument("--maintenant", help="date de référence AAAA-MM-JJTHH:MM (essais)")
    args = parser.parse_args()

    ics = Path(args.ics).read_bytes() if args.ics else telecharger(ICS_URL)
    maintenant = (datetime.datetime.fromisoformat(args.maintenant).replace(tzinfo=TZ)
                  if args.maintenant else datetime.datetime.now(TZ))
    ancien = json.loads(SORTIE.read_text(encoding="utf-8")) if SORTIE.exists() else {}
    donnees = construire(ics, maintenant, ancien)
    if not donnees["up"]:
        sys.exit("Aucune sortie à venir dans le calendrier : fichier conservé.")

    if {k: v for k, v in ancien.items() if k != "generated"} == donnees:
        print("Calendrier inchangé : rien à faire.")
        return
    donnees = {"generated": maintenant.strftime("%Y-%m-%dT%H:%M"), **donnees}
    SORTIE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SORTIE.with_suffix(".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(SORTIE)
    sundays = [e for e in donnees["up"] if e["km"]][:NB_SORTIES]
    print(f"{SORTIE.name} mis à jour : " + " ; ".join(f"{e['d']} {e['t']} ({e['km']} km)" for e in sundays))


if __name__ == "__main__":
    main()
