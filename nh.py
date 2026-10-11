"""nhentai downloader.

  python nh.py links.txt        download every link/ID in the file
  python nh.py URL_OR_ID ...    download these
  python nh.py --watch          download any nhentai link you copy
  python nh.py --forget ID ...  remove IDs from the database so they download again

Downloaded IDs are kept in downloaded.db (next to this script); a known ID is skipped
without any network request. Existing .cbz files in the output folder are imported into it
at startup (the ID is read from their ComicInfo.xml).

Uses the cookie + user-agent saved by `nhentai --cookie ... --useragent ...`.
curl_cffi (a browser-compatible TLS client) is required: the nhentai CLI's own HTTP client
gets 403 responses even with a valid cookie.
Windows only (config path and clipboard access).
"""
import json, os, queue, re, sqlite3, subprocess, sys, threading, time, zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape
from concurrent.futures import ThreadPoolExecutor
from curl_cffi import requests

OUT = os.path.join(os.path.expanduser("~"), "Downloads", "nhentai")
PAGE_THREADS = 16  # images fetched in parallel per gallery; measured: 24+ starts getting throttled
CFG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", ".nhentai", "config.json")
try:
    cfg = json.load(open(CFG))
    H = {"Cookie": cfg["cookie"], "User-Agent": cfg["useragent"]}
except (OSError, ValueError, KeyError):
    sys.exit("No saved cookie/user-agent found. Run: nhentai --cookie \"cf_clearance=...\" --useragent \"...\" (see README).")

DB = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloaded.db"), check_same_thread=False)
DB.execute("CREATE TABLE IF NOT EXISTS done (id INTEGER PRIMARY KEY, title TEXT, file TEXT, ts TEXT)")
DB.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY, ts TEXT)")  # watch-mode backlog, survives restarts


class AuthError(Exception):
    """nhentai answered 403: cf_clearance expired or the IP changed."""


def is_done(gid):
    return DB.execute("SELECT 1 FROM done WHERE id=?", (int(gid),)).fetchone() is not None


def mark_done(gid, title, file):
    with DB:
        DB.execute("INSERT OR REPLACE INTO done VALUES (?,?,?,datetime('now'))", (int(gid), title, file))
        DB.execute("DELETE FROM queue WHERE id=?", (int(gid),))


def enqueue(gid):
    with DB:
        DB.execute("INSERT OR IGNORE INTO queue VALUES (?, datetime('now'))", (int(gid),))


def pending():
    return [str(r[0]) for r in DB.execute("SELECT id FROM queue ORDER BY ts, id") if not is_done(r[0])]


def sync_existing():
    """Import .cbz files already in OUT (ID comes from ComicInfo.xml's <Web> url)."""
    new = 0
    for f in os.listdir(OUT) if os.path.isdir(OUT) else []:
        if not f.lower().endswith(".cbz"):
            continue
        try:
            with zipfile.ZipFile(os.path.join(OUT, f)) as z:
                m = re.search(r"/g/(\d+)/", z.read("ComicInfo.xml").decode("utf-8", "replace"))
        except Exception:
            continue
        if m and not is_done(m.group(1)):
            mark_done(m.group(1), f[:-4], f)
            new += 1
    if new:
        print(f"Imported {new} existing .cbz file(s) into the database.\n")


def get(url, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, headers=H, impersonate="chrome", timeout=30)
        except Exception:
            r = None
        if r is not None:
            if r.status_code == 200:
                return r
            if r.status_code == 403 and "api" in url:
                raise AuthError
        time.sleep(1 + i)
    return None


LANG = {"japanese": "ja", "english": "en", "chinese": "zh"}


def comic_info(g):
    """ComicInfo.xml: read by Komga, Kavita, Mihon, YACReader etc."""
    by = lambda t: [x["name"] for x in g["tags"] if x["type"] == t]
    d = datetime.fromtimestamp(g["upload_date"], timezone.utc)
    f = {
        "Title": g["title"]["pretty"],
        "Writer": ", ".join(by("artist")),
        "Penciller": ", ".join(by("artist")),
        "Publisher": ", ".join(by("group")),
        "Tags": ", ".join(by("tag")),
        "Characters": ", ".join(by("character")),
        "Genre": ", ".join(by("category")),
        "Notes": "Parody: " + ", ".join(by("parody")) if by("parody") else "",
        "Web": f"https://nhentai.net/g/{g['id']}/",
        "LanguageISO": next((LANG[l] for l in by("language") if l in LANG), ""),
        "Year": d.year, "Month": d.month, "Day": d.day,
        "PageCount": len(g["pages"]),
        "AgeRating": "Adults Only 18+",
        "Manga": "YesAndRightToLeft",
    }
    body = "".join(f"  <{k}>{escape(str(v))}</{k}>\n" for k, v in f.items() if v != "")
    return f'<?xml version="1.0" encoding="utf-8"?>\n<ComicInfo>\n{body}</ComicInfo>\n'


STATS = {"done": 0, "skipped": 0, "failed": 0}
FAILED = []


def summary(waiting=0):
    line = f"  Session: {STATS['done']} done, {STATS['skipped']} skipped, {STATS['failed']} failed, {waiting} waiting"
    if FAILED:
        line += f"  | failed IDs: {' '.join(FAILED)}"
    print(line + "\n")


def fail(gid, msg):
    STATS["failed"] += 1
    FAILED.append(gid)
    print(f"\n[{gid}] FAILED: {msg}")


def download(gid):
    if is_done(gid):
        STATS["skipped"] += 1
        with DB:
            DB.execute("DELETE FROM queue WHERE id=?", (int(gid),))
        print(f"[{gid}] skipped, already in database")
        return
    r = get(f"https://nhentai.net/api/v2/galleries/{gid}")
    if not r:
        return fail(gid, "could not fetch gallery info")
    g = r.json()
    title = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "", g["title"]["pretty"]).strip(" .")[:100].strip(" .") or str(gid)
    if title.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        title = f"{title} ({gid})"  # reserved Windows device names
    out = os.path.join(OUT, f"{title}.cbz")
    if os.path.exists(out):  # same title, different gallery: don't overwrite it
        out = os.path.join(OUT, f"{title} ({gid}).cbz")
    pages = g["pages"]
    print(f"[{gid}] {title}")
    n, lock = [0], threading.Lock()

    def page(p):
        img = get(f"https://i1.nhentai.net/{p['path']}")
        with lock:
            n[0] += 1
            print(f"\r  downloading {n[0]}/{len(pages)} ({n[0] * 100 // len(pages)}%)", end="", flush=True)
        return img.content if img else None

    with ThreadPoolExecutor(PAGE_THREADS) as ex:
        data = list(ex.map(page, pages))
    if None in data:
        return fail(gid, f"{data.count(None)} of {len(pages)} pages failed, nothing saved (copy the link again to retry)")
    os.makedirs(OUT, exist_ok=True)
    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:  # images are already compressed
        z.writestr("ComicInfo.xml", comic_info(g))
        for p, b in zip(pages, data):
            z.writestr(f"{p['number']:03d}{os.path.splitext(p['path'])[1]}", b)
    os.replace(tmp, out)
    mark_done(gid, title, os.path.basename(out))
    STATS["done"] += 1
    print(f"\r  OK {len(pages)}/{len(pages)} pages saved: {os.path.basename(out)}")


def ids_in(text, bare=True):
    """Gallery IDs from nhentai.net/g/<id> links; with bare=True also lines that are just a number."""
    found = re.findall(r"nhentai\.net/g/(\d+)", text) + (re.findall(r"^\s*(\d{1,7})\s*$", text, re.M) if bare else [])
    return list(dict.fromkeys(found))


def clipboard():
    return subprocess.run(["powershell", "-command", "Get-Clipboard -Raw"], capture_output=True, text=True).stdout


AUTH_MSG = "403 from nhentai: cf_clearance expired or your IP changed (VPN?). Redo the `nhentai --cookie ... --useragent ...` setup."
STOP = threading.Event()


def worker(q):
    while True:
        gid = q.get()
        try:
            download(gid)
        except AuthError:
            print(f"\n\n!! {AUTH_MSG}\n!! Stopped. {q.qsize() + 1} gallery(ies) stay saved in the queue; restart watch mode to resume.")
            STOP.set()
            return
        except Exception as e:  # keep the watcher alive whatever happens to one gallery
            fail(gid, repr(e))
        summary(q.qsize())


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--forget"]:
        with DB:
            for i in ids_in("\n".join(a[1:])):
                n = DB.execute("DELETE FROM done WHERE id=?", (int(i),)).rowcount
                print(f"[{i}] {'removed from database' if n else 'was not in database'}")
        sys.exit()
    if a:
        sync_existing()
    if a == ["--watch"]:
        q, seen = queue.Queue(), set()
        for i in pending():  # backlog left over from a previous run
            seen.add(i)
            q.put(i)
        if q.qsize():
            print(f"Resuming {q.qsize()} gallery(ies) saved from the last run.")
        threading.Thread(target=worker, args=(q,), daemon=True).start()
        print("Watching clipboard... copy nhentai links. Ctrl+C to stop. The queue is saved, so closing the window is safe.\n")
        while not STOP.is_set():
            for i in ids_in(clipboard(), bare=False):  # links only, so copying a random number never downloads anything
                if i not in seen:
                    seen.add(i)
                    enqueue(i)
                    q.put(i)
                    print(f"+ queued {i} ({q.qsize()} waiting)")
            time.sleep(0.7)
        input("\nPress Enter to close...")
    elif a:
        text = open(a[0], encoding="utf-8").read() if len(a) == 1 and os.path.isfile(a[0]) else "\n".join(a)
        ids = ids_in(text)
        for k, i in enumerate(ids, 1):
            print(f"--- {k}/{len(ids)} ---")
            try:
                download(i)
            except AuthError:
                sys.exit(f"\n!! {AUTH_MSG}\n!! Stopped at {k}/{len(ids)}; rerun the same command to continue (finished ones are skipped).")
            except Exception as e:
                fail(i, repr(e))
        summary()
    else:
        print(__doc__)
