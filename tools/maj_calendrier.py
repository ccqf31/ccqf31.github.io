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
import functools
import html
import json
import math
import re
import sys
import unicodedata
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
HISTORIQUE = datetime.timedelta(days=3 * 365)    # sorties passées relues pour retrouver les traces OpenRunner
PROGRAMME = datetime.timedelta(days=365)         # bibliothèque : les parcours au programme des 12 prochains mois
SORTIE = Path(__file__).resolve().parent.parent / "data" / "sorties.json"

# « Parcours 029 AH Lacougotte 78km Garmin », « Parcours 012H v2.1 Garmin », « Parcours 57 T L »…
# H : sens horaire ; AH (anciennement T) : sens anti-horaire, noté T dans data/sorties.json
NUMERO = re.compile(r"[Pp]arcou?rs?t?\s*(?:Garmin\s*)?0*(\d{1,3})\s*[-_ ]?\s*(AH|[TH])", re.I)
ID_GARMIN = re.compile(r"course/(\d+)")
VERSION = re.compile(r"\bv\s?(\d+(?:\.\d+)?)", re.I)
ID_OPENRUNNER = re.compile(r"(?:route-details/|[?&]id=|/r/)(\d+)")
APERCU_ALTITUDES = 160      # points du petit profil d'altitude de chaque parcours
APERCU_TOLERANCE = 0.00025  # simplification du tracé (en degrés, environ 25 m)
APERCU_VERSION = 2          # à augmenter si le calcul des aperçus change : ils seront recalculés
COMMUNES_API = "https://geo.api.gouv.fr/departements/{}/communes?fields=nom,mairie,centre,population&format=json"
COMMUNES = Path(__file__).resolve().parent / "communes.json"   # liste lue une fois sur geo.api.gouv.fr
DEPARTEMENTS = ("31", "81", "11", "09", "82")   # communes autour des parcours : Haute-Garonne et départements voisins
VILLAGES_MAX = 6            # villages nommés sur la carte de l'aperçu de chaque tracé
VILLAGES_DISTANCE = 600     # un village est traversé quand le tracé passe à moins de 600 m de sa mairie
VILLAGES_CITE = 2000        # … ou à moins de 2 km quand le nom du parcours le cite (« Gardouch »)
VILLAGES_ECART = 3000       # deux villages nommés sont à plus de 3 km l'un de l'autre, et du départ
# Parcours sans trace dans leur sens, dessinés avec la trace du sens opposé parcourue à l'envers
# (choix du club : le 007 AH est le 007 H dans l'autre sens)
A_L_ENVERS = {"007T": "007H"}


def telecharger(url, essais=3):
    for n in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cyclo-quint-fonsegrives-site"})
            with urllib.request.urlopen(req, timeout=30) as rep:
                return rep.read()
        except urllib.error.HTTPError as err:
            if err.code not in (429, 500, 502, 503) or n == essais - 1:
                raise
        except (urllib.error.URLError, ConnectionError):
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


@functools.lru_cache(maxsize=None)
def communes():
    """[nom, latitude, longitude, population] de chaque commune des DEPARTEMENTS, située par sa mairie.
    La liste est gardée dans tools/communes.json : supprimer ce fichier pour la relire."""
    if COMMUNES.exists():
        return json.loads(COMMUNES.read_text(encoding="utf-8"))
    liste = []
    for dep in DEPARTEMENTS:
        for c in json.loads(telecharger(COMMUNES_API.format(dep))):
            point = (c.get("mairie") or c.get("centre") or {}).get("coordinates")
            if point:
                liste.append([c["nom"], point[1], point[0], c.get("population") or 0])
    COMMUNES.write_text(json.dumps(liste, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return liste


def normaliser(texte):
    """« Saint-Félix-Lauragais » → « saint felix lauragais », « St Julia » → « saint julia » (entre espaces)."""
    t = "".join(c for c in unicodedata.normalize("NFD", texte.lower()) if not unicodedata.combining(c))
    t = re.sub(r"\bste\b", "sainte", re.sub(r"\bst\b", "saint", re.sub(r"[-'’]", " ", t)))
    return " " + " ".join(t.split()) + " "


def cite(commune, nom):
    """Texte du nom (normalisé) du parcours qui cite la commune, ou "" : « St Felix » cite Saint-Félix-Lauragais."""
    n = normaliser(commune)
    court = re.sub(r"(?<=\w) (?:lauragais|(?:sur|de|d|du|des|les|en) \w.*) $", " ", n)   # « saint felix », « auriac »
    return n if n in nom else court if court in nom else ""


def ecart_au_trace(pts, la, lo):
    """Distance (en mètres) du point (la, lo) au tracé pts ((latitude, longitude) en degrés),
    et numéro du tronçon le plus proche."""
    kx, ky = 111320 * math.cos(math.radians(pts[0][0])), 110540    # mètres par degré
    px, py = lo * kx, la * ky
    meilleur = (math.inf, 0)
    for i, ((ya, xa), (yb, xb)) in enumerate(zip(pts, pts[1:])):
        ax, ay, dx, dy = xa * kx, ya * ky, (xb - xa) * kx, (yb - ya) * ky
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy))) if dx or dy else 0
        meilleur = min(meilleur, (math.hypot(ax + t * dx - px, ay + t * dy - py), i))
    return meilleur


def communes_citees(nom):
    """Communes que cite le nom du parcours, [] si la liste des communes manque. Une citation comprise dans une
    autre plus longue ne compte pas : « St Jean Lherm » cite Saint-Jean-Lherm, pas Saint-Jean."""
    try:
        cites = [(cite(c[0], normaliser(nom)), c) for c in communes()] if nom else []
    except Exception as err:
        print(f"Attention : liste des communes indisponible : {err}", file=sys.stderr)
        return []
    cites = [(texte, c) for texte, c in cites if texte]
    return [c for texte, c in cites if not any(texte != autre and texte in autre for autre, _ in cites)]


def villages(pts, nom):
    """Principaux villages d'un tracé ((latitude, longitude) en degrés) : d'abord ceux que cite le nom du
    parcours, puis les plus peuplés, à plus de VILLAGES_ECART du départ et les uns des autres.
    Rangés dans l'ordre du parcours : [nom, latitude × 1e5, longitude × 1e5]."""
    kx, ky = 111320 * math.cos(math.radians(pts[0][0])), 110540
    loin = lambda a, b: math.hypot((a[0] - b[0]) * ky, (a[1] - b[1]) * kx) > VILLAGES_ECART
    marge = VILLAGES_CITE / ky
    la0, la1 = min(p[0] for p in pts) - marge, max(p[0] for p in pts) + marge
    lo0, lo1 = min(p[1] for p in pts) - marge * ky / kx, max(p[1] for p in pts) + marge * ky / kx
    cites = {c[0] for c in communes_citees(nom)}
    candidats = []
    for commune, la, lo, pop in communes():
        if not (la0 <= la <= la1 and lo0 <= lo <= lo1) or not loin((la, lo), pts[0]):
            continue
        d, i = ecart_au_trace(pts, la, lo)
        if d <= (VILLAGES_CITE if commune in cites else VILLAGES_DISTANCE):
            candidats.append((commune in cites, pop, i, commune, la, lo))
    choisis = []
    for v in sorted(candidats, reverse=True):
        if len(choisis) < VILLAGES_MAX and (v[0] or all(loin(v[4:6], c[4:6]) for c in choisis)):
            choisis.append(v)
    return [[v[3], round(v[4] * 1e5), round(v[5] * 1e5)] for v in sorted(choisis, key=lambda v: v[2])]


def doute(pv, km_annonce, cites):
    """Raison de croire que l'aperçu pv n'est pas la trace du parcours, None s'il colle avec le calendrier :
    distance annoncée dans le titre, communes citées dans le nom du parcours."""
    km = pv["len"] / 1000
    if km_annonce and abs(km - km_annonce) > max(5, km_annonce / 10):
        return f"{km:.1f} km au lieu des {km_annonce:g} km annoncés"
    if cites:
        pts = [(la / 1e5, lo / 1e5) for la, lo in decoder(pv["ps"], 2)]
        if all(ecart_au_trace(pts, c[1], c[2])[0] > VILLAGES_CITE for c in cites):
            return f"ne passe pas par {cites[0][0]}"
    return None


def a_l_envers(trace):
    """La même trace parcourue dans l'autre sens : profil et tracé retournés, montée et descente échangées.
    Sans lien OpenRunner : il ouvrirait la trace dans le sens opposé."""
    return dict(trace, el=encoder(decoder(trace["el"], 1)[::-1]), shape=encoder(decoder(trace["shape"], 2)[::-1]),
                ascent=trace["descent"], descent=trace["ascent"], **{"or": None})


def apercu_a_l_envers(pv):
    """Aperçu de la bibliothèque retourné ; sur une boucle, la montée est égale à la descente."""
    return dict(pv, pe=encoder(decoder(pv["pe"], 1)[::-1]), ps=encoder(decoder(pv["ps"], 2)[::-1]))


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
    variantes = {m.group(1) or m.group(2) for m in re.finditer(r"[_ ]([LC])(?:\b|_)|\b(?:AH|[TH])([LC])\b", titre)}
    return {
        "debut": debut.astimezone(TZ), "titre": titre,
        "garmin": garmin[-1] if garmin else None, "openrunner": openrunner[-1] if openrunner else None,
        "km": km if km and 40 <= km <= 200 else None, "dp": dplus if dplus and 100 <= dplus <= 2500 else None,
        "variantes": variantes, "nom": nom_parcours(titre),
    }


def cle_parcours(titre):
    """« 029T » pour « Parcours 029 AH … » : le numéro sur 3 chiffres, puis H (horaire) ou T (anti-horaire)."""
    m = NUMERO.search(titre)
    return m and m.group(1).zfill(3) + ("H" if m.group(2).upper() == "H" else "T")


def nom_parcours(titre):
    """« Lacougotte » pour « Parcours 029 AH Lacougotte 78km Garmin » : le lieu, sans numéro, sens, version ni distance."""
    m = NUMERO.search(titre)
    if not m:
        return None
    reste = titre[m.end():].replace("_", " ")
    reste = re.sub(r"\bv\s?\d+(?:\.\d+)*|\d{2,3}(?:[.,]\d+)?\s*km?\b|\d{3,4}\s*m?\s*(?:dev\+?|D\+)|\d{3,4}\s*m\b|\bGarmin\b"
                   r"|\bmodifi[ée]e?\b|(?<![\w'’-])(?:AH|[HTLC])(?![\w'’-])", " ", reste, flags=re.I)
    nom = " ".join(reste.split()).strip(" -·,:")
    # Un nom de lieu commence par une majuscule : « froid », « travaux » ou « CHANGEMENT D'HORAIRE » sont écartés
    return nom if re.match(r"[A-ZÀ-Ý]", nom) and not nom.isupper() and not re.search(r"\d", nom) else None


def lire_openrunner(lien):
    m = ID_OPENRUNNER.search(lien or "")
    if not m:
        return None
    route = json.loads(telecharger(OPENRUNNER_API.format(m.group(1))))["data"]
    elev = route["elevation"]
    return {
        "name": route["name"], "len": route["length"], "step": elev.get("interval", 100),
        "ascent": elev["ascent"], "descent": elev.get("descent", elev["ascent"]),
        "el": elev["full_encoded"], "shape": route["shape"]["full_encoded"],
        "or": OPENRUNNER_PAGE.format(m.group(1)),
    }


def construire(ics, maintenant, ancien=None):
    cal = icalendar.Calendar.from_ical(ics)
    tous = recurring_ical_events.of(cal).between(maintenant - HISTORIQUE, maintenant + datetime.timedelta(days=400))
    # Tri complet (date, titre, lien) : deux événements à la même heure gardent toujours le même ordre
    evenements = sorted(filter(None, (lire_evenement(e) for e in tous)),
                        key=lambda e: (e["debut"], e["titre"], e["garmin"] or "", e["openrunner"] or ""))

    # Trace OpenRunner déjà associée à chaque parcours Garmin, dans chaque sens (« 432047792T »).
    # La nouvelle liste du club a changé des numéros (l'ancien 17 T est le 029 AH) et ses sorties n'ont
    # souvent qu'un lien Garmin : la trace se retrouve par ce lien, gardé d'une mise à jour à l'autre.
    ancien = ancien or {}
    traces = dict(ancien.get("traces", {}))
    for p in ancien.get("lib", []):
        if p.get("o") and ID_GARMIN.search(p.get("g") or ""):
            traces[ID_GARMIN.search(p["g"]).group(1) + p["s"]] = p["o"]

    def trace_garmin(lien_garmin, cle):
        garmin = ID_GARMIN.search(lien_garmin or "")
        return garmin and traces.get(garmin.group(1) + cle[3])

    # Bibliothèque : les parcours au programme des 12 prochains mois (une entrée par numéro et par sens).
    # Les liens OpenRunner des sorties passées du même numéro ne servent qu'en secours, une fois vérifiés :
    # avec la nouvelle liste du club, un même numéro a pu désigner un autre parcours.
    biblio, passes = {}, {}
    for e in evenements:
        cle = cle_parcours(e["titre"])
        if not cle or e["debut"] >= maintenant + PROGRAMME:
            continue
        if e["debut"] < maintenant:
            if e["openrunner"]:
                passes.setdefault(cle, []).insert(0, e["openrunner"])     # le plus récent d'abord
            continue
        p = biblio.setdefault(cle, {"n": cle[:3], "s": cle[3], "nom": None, "v": set(), "km": None, "dp": None,
                                    "g": None, "o": None, "next": e["debut"]})
        p["v"] |= e["variantes"]
        for champ, cle_ev in (("nom", "nom"), ("km", "km"), ("dp", "dp"), ("g", "garmin"), ("o", "openrunner")):
            if e[cle_ev]:
                p[champ] = e[cle_ev]
    for cle, p in biblio.items():
        p["o"] = p["o"] or trace_garmin(p["g"], cle)

    # Aperçus (profil + tracé) de tous les parcours de la bibliothèque, depuis OpenRunner. Une trace dont la
    # distance ou les communes ne collent pas avec le calendrier vient d'un lien erroné : elle est écartée.
    # Un aperçu déjà calculé pour le même lien OpenRunner est repris tel quel : pas de nouvel appel.
    deja = {p["o"]: p["pv"] for p in ancien.get("lib", [])
            if p.get("o") and p.get("pv", {}).get("v") == APERCU_VERSION}
    page = lambda lien: OPENRUNNER_PAGE.format(ID_OPENRUNNER.search(lien).group(1))
    for cle, p in sorted(biblio.items()):
        km_annonce, cites, garmin = p["km"], communes_citees(p["nom"]), ID_GARMIN.search(p["g"] or "")
        liens = [(page(p["o"]), True)] if p["o"] and ID_OPENRUNNER.search(p["o"]) else []
        liens += [(page(l), False) for l in passes.get(cle, []) if ID_OPENRUNNER.search(l)]
        p["o"], vus = None, set()
        for lien, du_programme in liens:
            # Lien d'une sortie passée : seulement s'il se vérifie (distance annoncée ou commune citée)
            if lien in vus or not (du_programme or km_annonce or cites):
                continue
            vus.add(lien)
            try:
                pv = deja.get(lien) or apercu(lire_openrunner(lien))
            except Exception as err:
                print(f"Attention : aperçu impossible pour le parcours {cle} ({lien}) : {err}", file=sys.stderr)
                continue
            raison = doute(pv, km_annonce, cites)
            if raison:
                print(f"Attention : parcours {cle} « {p['nom'] or ''} » : trace {lien} écartée ({raison}).", file=sys.stderr)
                continue
            p.update(o=lien, pv=pv, km=round(pv["len"] / 1000, 1), dp=pv["up"])
            break
        if garmin:      # la table suit la trace retenue (ou l'oublie si aucune ne convient)
            if p["o"]:
                traces[garmin.group(1) + cle[3]] = p["o"]
            else:
                traces.pop(garmin.group(1) + cle[3], None)

    # Événements à venir
    a_venir = [e for e in evenements if maintenant <= e["debut"] <= maintenant + datetime.timedelta(days=JOURS_A_VENIR)]
    dimanches = [e for e in a_venir if e["debut"].weekday() == 6 and NUMERO.search(e["titre"])][:NB_SORTIES]

    # Détails OpenRunner des prochaines sorties du dimanche, avec la trace vérifiée de la bibliothèque
    profils = {}
    for e in dimanches:
        cle = cle_parcours(e["titre"])
        lien = biblio[cle]["o"]
        envers = not lien and biblio.get(A_L_ENVERS.get(cle), {}).get("o")
        try:
            trace = lire_openrunner(lien or envers)
            trace = a_l_envers(trace) if envers and trace else trace
        except Exception as err:  # une trace introuvable ne bloque pas la mise à jour
            print(f"Attention : trace OpenRunner illisible pour {e['titre']} ({lien}) : {err}", file=sys.stderr)
            trace = None
        if not trace:
            print(f"Attention : pas de trace OpenRunner pour {e['titre']}", file=sys.stderr)
            continue
        v_cal, v_or = VERSION.search(e["titre"]), VERSION.search(trace["name"])
        if v_cal and not envers and (not v_or or v_cal.group(1) != v_or.group(1)):
            print(f"Attention : « {e['titre']} » renvoie vers la trace OpenRunner « {trace['name']} » "
                  "(version différente) : vérifiez le lien.", file=sys.stderr)
        km, dp = round(trace["len"] / 1000, 1), trace["ascent"]
        e["km"], e["dp"], e["openrunner"] = km, dp, trace["or"]
        biblio[cle].update(km=km, dp=dp)
        profils[cle] = {k: trace[k] for k in ("name", "len", "step", "el", "shape", "or")}

    for cle, source in A_L_ENVERS.items():
        p = biblio.get(cle)
        if p and not p["o"] and biblio.get(source, {}).get("pv"):
            p["pv"] = apercu_a_l_envers(biblio[source]["pv"])
            p["km"], p["dp"] = round(p["pv"]["len"] / 1000, 1), p["pv"]["up"]

    # Principaux villages de chaque parcours, d'après le tracé de son aperçu et son nom
    try:
        for p in biblio.values():
            if p.get("pv"):
                p["vi"] = villages([(la / 1e5, lo / 1e5) for la, lo in decoder(p["pv"]["ps"], 2)], p["nom"])
    except Exception as err:  # liste des communes indisponible : on garde les villages de la mise à jour précédente
        print(f"Attention : villages non recalculés : {err}", file=sys.stderr)
        precedents = {q["n"] + q["s"]: q.get("vi") for q in ancien.get("lib", [])}
        for cle, p in biblio.items():
            p["vi"] = p.get("vi") or precedents.get(cle)
    for cle, prof in profils.items():
        if biblio[cle].get("vi"):
            prof["vi"] = biblio[cle]["vi"]

    jour = lambda d: d.strftime("%Y-%m-%d") if d else None
    return {
        "up": [{"t": e["titre"], "nom": e["nom"], "d": e["debut"].strftime("%Y-%m-%dT%H:%M"), "g": e["garmin"],
                "o": e["openrunner"], "km": e["km"], "dp": e["dp"]} for e in a_venir],
        "lib": [{"n": p["n"], "s": p["s"], "nom": p["nom"], "v": "".join(sorted(p["v"])), "km": p["km"], "dp": p["dp"],
                 "g": p["g"], "o": p["o"], "next": jour(p["next"]),
                 **({"pv": p["pv"]} if p.get("pv") else {}), **({"vi": p["vi"]} if p.get("vi") else {})}
                for _, p in sorted(biblio.items())],
        "prof": profils,
        "traces": dict(sorted(traces.items())),
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
