#!/usr/bin/env python3
"""
MaFo-Watcher
============
Überwacht https://www.mafo-service-schmidt.de/Studien und meldet neue Studien
sofort per Telegram (und optional als Desktop-Benachrichtigung).

Erkennt beide Sparten:
  - "aktuelle Studien aus Frankfurt / Rhein-Main"  -> section = "regional"
  - "aktuelle Online-Studien bundesweit"           -> section = "online"

Jede Studie hat auf der Seite eine eindeutige numerische ID (z.B. #10766).
Diese IDs werden in seen_studies.json gespeichert; alles Unbekannte = neu.

Nutzung:
    python mafo_watcher.py --init     # einmalig: aktuellen Stand merken, NICHT benachrichtigen
    python mafo_watcher.py            # normaler Durchlauf (per cron/Task Scheduler)
    python mafo_watcher.py --test     # Testnachricht senden, prüft die Telegram-Config
    python mafo_watcher.py --list     # aktuelle Studien nur anzeigen, nichts speichern
"""

import argparse
import html
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests. Bitte installieren mit:  pip install requests")


# ---------------------------------------------------------------------------
# KONFIGURATION
# ---------------------------------------------------------------------------
# Am besten als Umgebungsvariablen setzen. Alternativ hier direkt eintragen.

TELEGRAM_TOKEN = os.environ.get("MAFO_TG_TOKEN", "")   # z.B. "8123456:AAF..."
TELEGRAM_CHAT_ID = os.environ.get("MAFO_TG_CHAT", "")  # z.B. "123456789"

# Welche Sparten interessieren dich? "both" | "regional" | "online"
WATCH_SECTIONS = os.environ.get("MAFO_SECTIONS", "both").lower()

# Optional: nur benachrichtigen, wenn der Titel eines dieser Wörter enthält.
# Leer lassen = alles melden. Beispiel: ["online", "produkttest", "test"]
ONLY_KEYWORDS: list[str] = []

# Optional: nie benachrichtigen, wenn der Titel eines dieser Wörter enthält.
EXCLUDE_KEYWORDS: list[str] = ["instagram", "newsletter", "folgen sie uns"]

# Desktop-Popup zusätzlich zu Telegram versuchen?
DESKTOP_NOTIFY = os.environ.get("MAFO_DESKTOP", "1") == "1"

# ---------------------------------------------------------------------------

BASE_URL = "https://www.mafo-service-schmidt.de"
STUDIES_URL = f"{BASE_URL}/Studien"
STATE_FILE = Path(__file__).resolve().parent / "seen_studies.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

# Marker-Text der zweiten Überschrift auf der Seite. Alles ab dieser Position
# im HTML gehört zur bundesweiten Online-Sparte.
ONLINE_SECTION_MARKERS = [
    r"Online-Studien\s+bundesweit",
    r"bundesweit",
]


# ---------------------------------------------------------------------------
# Seite holen und parsen
# ---------------------------------------------------------------------------

def fetch_html(url: str = STUDIES_URL, retries: int = 3) -> str:
    """Holt das HTML der Studienseite, mit einfachem Retry bei Netzproblemen."""
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:  # Netzfehler, Timeout, 5xx ...
            last_err = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Seite nicht erreichbar: {last_err}")


def _strip_tags(fragment: str) -> str:
    """Entfernt HTML-Tags und normalisiert Whitespace."""
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _online_section_start(page: str) -> int:
    """Position im HTML, ab der die bundesweite Online-Sparte beginnt."""
    for pattern in ONLINE_SECTION_MARKERS:
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            return m.start()
    return len(page)  # kein Marker gefunden -> alles gilt als 'regional'


def parse_studies(page: str) -> list[dict]:
    """
    Findet alle Studien-Einträge.

    Die Seite verlinkt jede Studie über einen Anker der Form href="#10766".
    Der Linktext ist der Studientitel. Die Zuordnung zur Sparte erfolgt über
    die Position im HTML relativ zur 'bundesweit'-Überschrift.
    """
    split_pos = _online_section_start(page)
    studies: dict[str, dict] = {}

    pattern = re.compile(
        r'<a\b[^>]*href=["\']#(\d{3,8})["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for m in pattern.finditer(page):
        study_id = m.group(1)
        title = _strip_tags(m.group(2))

        if not title or len(title) < 5:
            continue
        if study_id in studies:  # erster Treffer gewinnt
            continue

        studies[study_id] = {
            "id": study_id,
            "title": title,
            "section": "online" if m.start() >= split_pos else "regional",
            "url": f"{STUDIES_URL}#{study_id}",
            "found_at": datetime.now().isoformat(timespec="seconds"),
        }

    # Höchste ID zuerst = neueste Studien oben
    return sorted(studies.values(), key=lambda s: int(s["id"]), reverse=True)


def is_relevant(study: dict) -> bool:
    """Filtert nach Sparte und Stichwörtern."""
    if WATCH_SECTIONS in ("regional", "online") and study["section"] != WATCH_SECTIONS:
        return False

    title_lc = study["title"].lower()

    if any(bad.lower() in title_lc for bad in EXCLUDE_KEYWORDS):
        return False
    if ONLY_KEYWORDS and not any(good.lower() in title_lc for good in ONLY_KEYWORDS):
        return False
    return True


# ---------------------------------------------------------------------------
# Zustand speichern
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen_ids": [], "last_check": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Kaputte Datei nicht stillschweigend als "nichts gesehen" behandeln,
        # sonst kommt beim nächsten Lauf ein Schwall alter Studien rein.
        backup = STATE_FILE.with_suffix(".json.broken")
        try:
            STATE_FILE.replace(backup)
            print(f"[!] Statusdatei defekt, gesichert als {backup.name}")
        except OSError:
            pass
        return {"seen_ids": [], "last_check": None}


def save_state(seen_ids) -> None:
    payload = {
        "seen_ids": sorted(set(seen_ids), key=int),
        "last_check": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)  # atomar, überlebt einen Abbruch mittendrin


# ---------------------------------------------------------------------------
# Benachrichtigungen
# ---------------------------------------------------------------------------

SECTION_LABEL = {
    "regional": "📍 Frankfurt / Rhein-Main",
    "online": "💻 Online bundesweit",
}


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if r.status_code != 200:
            print(f"[!] Telegram-Fehler {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[!] Telegram nicht erreichbar: {e}")
        return False


def send_desktop(title: str, body: str) -> None:
    """Best-effort Desktop-Popup. Schlägt es fehl, ist das kein Drama."""
    if not DESKTOP_NOTIFY:
        return
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
                check=False, timeout=10,
            )
        elif system == "Linux":
            subprocess.run(["notify-send", title, body], check=False, timeout=10)
        elif system == "Windows":
            ps = (
                'powershell -NoProfile -Command "'
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n = New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon = [System.Drawing.SystemIcons]::Information;"
                "$n.Visible = $true;"
                f"$n.ShowBalloonTip(20000, '{title}', '{body[:200]}', 'Info');"
                'Start-Sleep -Seconds 8"'
            )
            subprocess.run(ps, shell=True, check=False, timeout=30)
    except Exception:
        pass


def notify(new_studies: list[dict]) -> None:
    count = len(new_studies)
    heading = "🔔 <b>1 neue Studie bei MaFo Schmidt</b>" if count == 1 \
        else f"🔔 <b>{count} neue Studien bei MaFo Schmidt</b>"

    lines = [heading, ""]
    for s in new_studies:
        label = SECTION_LABEL.get(s["section"], s["section"])
        lines.append(f'{label}\n<a href="{s["url"]}">{html.escape(s["title"])}</a>')
        lines.append("")
    lines.append(f'➡️ <a href="{STUDIES_URL}">Alle Studien / direkt bewerben</a>')

    message = "\n".join(lines)

    if not send_telegram(message):
        print("[i] Telegram nicht konfiguriert oder fehlgeschlagen – nur Konsole/Desktop.")

    first = new_studies[0]["title"]
    body = first if count == 1 else f"{first} (+{count - 1} weitere)"
    send_desktop("Neue MaFo-Studie!", body)

    print(f"\n{'=' * 60}")
    print(f"{count} NEUE STUDIE(N) – {datetime.now():%d.%m.%Y %H:%M}")
    print("=" * 60)
    for s in new_studies:
        print(f"  [{s['section']:8}] {s['title']}")
        print(f"             {s['url']}")
    print()


# ---------------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------------

def run(init: bool = False, list_only: bool = False) -> int:
    page = fetch_html()
    studies = parse_studies(page)

    if not studies:
        print("[!] Keine Studien gefunden – vermutlich hat sich der Seitenaufbau "
              "geändert. Es wurde NICHTS gespeichert, damit keine Studie verloren geht.")
        return 2

    relevant = [s for s in studies if is_relevant(s)]

    if list_only:
        print(f"Gefunden: {len(studies)} Einträge, davon {len(relevant)} relevant\n")
        for s in studies:
            mark = "*" if is_relevant(s) else " "
            print(f"{mark} {s['id']}  [{s['section']:8}]  {s['title']}")
        return 0

    state = load_state()
    seen = set(state.get("seen_ids", []))

    if init or not seen:
        all_ids = [s["id"] for s in studies]
        save_state(all_ids)
        print(f"[✓] Startzustand gespeichert: {len(all_ids)} Studien als 'bekannt' "
              f"markiert.\n    Ab jetzt wird nur noch bei ECHT neuen Studien gemeldet.")
        return 0

    new = [s for s in relevant if s["id"] not in seen]

    if new:
        notify(new)
    else:
        print(f"[{datetime.now():%d.%m %H:%M}] Nichts Neues "
              f"({len(relevant)} Studien online).")

    # Alle gesehenen IDs merken, auch die herausgefilterten – sonst tauchen sie
    # bei geänderten Filtereinstellungen als "neu" auf.
    save_state(seen | {s["id"] for s in studies})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Überwacht neue Studien bei MaFo-Service-Schmidt.")
    ap.add_argument("--init", action="store_true",
                    help="Aktuellen Stand als bekannt speichern, ohne zu benachrichtigen")
    ap.add_argument("--list", action="store_true", dest="list_only",
                    help="Aktuelle Studien nur anzeigen")
    ap.add_argument("--test", action="store_true",
                    help="Testnachricht an Telegram senden")
    args = ap.parse_args()

    if args.test:
        ok = send_telegram(
            "✅ <b>MaFo-Watcher Test</b>\n\nDie Verbindung steht. "
            f'Ab jetzt bekommst du hier neue Studien gemeldet.\n\n'
            f'➡️ <a href="{STUDIES_URL}">Zu den Studien</a>'
        )
        send_desktop("MaFo-Watcher", "Testbenachrichtigung")
        print("[✓] Telegram-Nachricht gesendet." if ok
              else "[✗] Telegram fehlgeschlagen – Token/Chat-ID prüfen.")
        return 0 if ok else 1

    try:
        return run(init=args.init, list_only=args.list_only)
    except RuntimeError as e:
        print(f"[!] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
