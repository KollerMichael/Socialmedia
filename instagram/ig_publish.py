#!/usr/bin/env python3
"""Instagram Publisher für @medical.athletic.coach – postet direkt über die Meta Graph API.

Liest instagram/posts.yml, veröffentlicht fällige und freigegebene Beiträge (Karussell, Bild,
Story, Reel) und merkt sich Erledigtes in instagram/posted.json.

Modi (Umgebungsvariable MODE):
  publish  – fällige Beiträge wirklich posten (Standard beim Zeitplan-Lauf)
  dry-run  – nur anzeigen, was gepostet würde (nichts wird veröffentlicht)
  check    – Zugang prüfen: Account-Name und aktuelles Posting-Limit anzeigen

Benötigte Secrets: IG_USER_ID, IG_ACCESS_TOKEN
"""
import os, sys, json, time, pathlib, subprocess, datetime as dt
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests, yaml
from PIL import Image

ROOT = pathlib.Path(os.environ.get("GITHUB_WORKSPACE", pathlib.Path(__file__).resolve().parent.parent)).resolve()
DIR = ROOT / "instagram"
SCHEDULE = DIR / "posts.yml"
STATE = DIR / "posted.json"
JPG_DIR = DIR / "_jpg"
TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Vienna"))
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")
MODE = os.environ.get("MODE", "publish").strip().lower()
MAX_LATE_H = float(os.environ.get("MAX_LATE_HOURS", "12"))   # ältere, verpasste Posts nicht mehr nachholen
REPO = os.environ.get("GITHUB_REPOSITORY", "KollerMichael/Socialmedia")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
USER = os.environ.get("IG_USER_ID", "").strip()
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()
# Schlüssel aus "API-Einrichtung mit Instagram-Login" beginnen mit "IG" und laufen über graph.instagram.com,
# Schlüssel aus "API-Einrichtung mit Facebook-Login" (meist "EAA…") über graph.facebook.com.
HOST = "graph.instagram.com" if TOKEN.startswith("IG") else "graph.facebook.com"
API = f"https://{HOST}/{GRAPH_VERSION}"

IMG = {".jpg", ".jpeg", ".png", ".webp"}
VID = {".mp4", ".mov"}


def log(*a):
    print(*a, flush=True)


# ---------- Graph API ----------
def api(method, path, **params):
    params["access_token"] = TOKEN
    r = requests.request(method, f"{API}/{path}", params=params if method == "GET" else None,
                         data=None if method == "GET" else params, timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text}
    if r.status_code >= 400 or "error" in body:
        err = body.get("error", body)
        raise RuntimeError(f"Graph API {method} {path}: {err.get('message', err)} "
                           f"(code {err.get('code')}, subcode {err.get('error_subcode')})")
    return body


def wait_ready(container_id, video=False):
    limit = time.time() + (900 if video else 300)
    while time.time() < limit:
        st = api("GET", container_id, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Container {container_id} meldet {st}")
        time.sleep(10 if video else 4)
    raise RuntimeError(f"Container {container_id} wurde nicht rechtzeitig fertig")


def container(url, kind, caption=None, carousel_item=False, extra=None):
    video = pathlib.Path(url.split("?")[0]).suffix.lower() in VID
    p = {}
    if kind == "story":
        p["media_type"] = "STORIES"
    elif kind == "reel":
        p["media_type"] = "REELS"
    elif video:
        p["media_type"] = "VIDEO" if carousel_item else "REELS"
    p["video_url" if video else "image_url"] = url
    if carousel_item:
        p["is_carousel_item"] = "true"
    if caption and not carousel_item and kind != "story":
        p["caption"] = caption
    p.update(extra or {})
    cid = api("POST", f"{USER}/media", **p)["id"]
    wait_ready(cid, video)
    return cid


def publish(post, urls):
    kind, cap = post["type"], post.get("caption", "")
    if kind == "carousel":
        kids = [container(u, kind, carousel_item=True) for u in urls]
        cid = api("POST", f"{USER}/media", media_type="CAROUSEL", children=",".join(kids), caption=cap)["id"]
        wait_ready(cid)
    elif kind == "reel":
        extra = {"share_to_feed": "true"}
        if post.get("cover"):
            extra["cover_url"] = raw_url(prepare_media([post["cover"]])[0])
        cid = container(urls[0], kind, cap, extra=extra)
    else:  # image | story
        cid = container(urls[0], kind, cap)
    mid = api("POST", f"{USER}/media_publish", creation_id=cid)["id"]
    link = ""
    if kind != "story":
        try:
            link = api("GET", mid, fields="permalink").get("permalink", "")
        except RuntimeError:
            pass
    return mid, link


# ---------- Medien ----------
def raw_url(rel):
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{quote(rel)}"


def prepare_media(files):
    """Instagram akzeptiert für Bilder nur JPEG: PNG/WebP werden automatisch umgewandelt."""
    out = []
    for f in files:
        if f.startswith("http"):
            out.append(f)
            continue
        src = (ROOT / f).resolve()
        if not src.exists():
            raise FileNotFoundError(f"Datei nicht im Repo gefunden: {f}")
        if src.suffix.lower() in {".png", ".webp"}:
            JPG_DIR.mkdir(exist_ok=True)
            dst = JPG_DIR / (src.stem + ".jpg")
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                im = Image.open(src)
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (7, 21, 43))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                im.convert("RGB").save(dst, "JPEG", quality=93, optimize=True)
                log(f"  umgewandelt: {f} -> {dst.relative_to(ROOT)}")
            out.append(str(dst.relative_to(ROOT)))
        else:
            out.append(str(src.relative_to(ROOT)))
    return out


def git_push(msg):
    if MODE != "publish" or not os.environ.get("GITHUB_ACTIONS"):
        return
    subprocess.run(["git", "config", "user.name", "instagram-publisher"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "instagram"], cwd=ROOT, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, check=True)
    subprocess.run(["git", "pull", "--rebase", "origin", BRANCH], cwd=ROOT, check=True)
    subprocess.run(["git", "push", "origin", f"HEAD:{BRANCH}"], cwd=ROOT, check=True)


def wait_public(urls):
    for u in urls:
        for _ in range(24):
            if requests.head(u, timeout=20, allow_redirects=True).status_code == 200:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"Datei nicht öffentlich erreichbar: {u}")


# ---------- Zeitplan ----------
def validate(p):
    errs, warn = [], []
    for k in ("id", "type", "time", "media"):
        if not p.get(k):
            errs.append(f"Feld '{k}' fehlt")
    t, n, cap = p.get("type"), len(p.get("media") or []), p.get("caption", "") or ""
    if t not in ("carousel", "image", "story", "reel"):
        errs.append("type muss carousel, image, story oder reel sein")
    if t == "carousel" and not 2 <= n <= 10:
        errs.append("Karussell braucht 2–10 Dateien")
    if t in ("image", "story", "reel") and n != 1:
        errs.append(f"{t} braucht genau 1 Datei")
    if len(cap) > 2200:
        errs.append("Caption länger als 2200 Zeichen")
    if cap.count("#") > 30:
        errs.append("mehr als 30 Hashtags")
    if t in ("carousel", "image", "reel"):
        for must in ("#Sportordination", "#medicalathleticcoach", "sportordination.com"):
            if must.lower() not in cap.lower():
                errs.append(f"Caption ohne Pflichtangabe '{must}'")
    return errs, warn


def due(p, now):
    t = dt.datetime.strptime(str(p["time"]), "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    return t, now >= t


def diagnose():
    """Prüft Schlüssel und Konto-ID und zeigt, welche ID die richtige wäre."""
    log(f"Schlüsseltyp: {'Instagram-Login (graph.instagram.com)' if HOST.startswith('graph.instagram') else 'Facebook-Login (graph.facebook.com)'}")
    candidates = []
    try:
        if HOST.startswith("graph.instagram"):
            me = api("GET", "me", fields="user_id,username")
            log(f"Schlüssel gehört zu @{me.get('username')}")
            candidates.append((str(me.get("user_id")), me.get("username")))
        else:
            me = api("GET", "me", fields="id,name")
            log(f"Schlüssel gehört zu: {me.get('name')} (Typ Benutzer oder Seite)")
            try:
                pages = api("GET", "me/accounts", fields="id,name,instagram_business_account{id,username}").get("data", [])
                for pg in pages:
                    iba = pg.get("instagram_business_account")
                    log(f"  Seite '{pg.get('name')}' → Instagram: {('@' + iba['username'] + ' ID ' + iba['id']) if iba else 'nicht verknüpft'}")
                    if iba:
                        candidates.append((iba["id"], iba.get("username")))
            except RuntimeError:
                pass
            try:  # Seiten-Schlüssel: die Seite selbst abfragen
                own = api("GET", "me", fields="instagram_business_account{id,username}").get("instagram_business_account")
                if own:
                    log(f"  Diese Seite ist verknüpft mit @{own.get('username')} ID {own['id']}")
                    candidates.append((own["id"], own.get("username")))
            except RuntimeError:
                pass
    except RuntimeError as e:
        sys.exit(f"❌ Schlüssel ungültig oder ohne Berechtigung: {e}")

    try:
        acc = api("GET", USER, fields="username")
        lim = api("GET", f"{USER}/content_publishing_limit", fields="config,quota_usage")
        log(f"✅ Zugang ok: @{acc.get('username')} – IG_USER_ID passt.")
        log(f"   Posting-Limit: {json.dumps(lim.get('data', lim), ensure_ascii=False)}")
    except RuntimeError as e:
        log(f"❌ IG_USER_ID passt nicht zu diesem Schlüssel: {e}")
        if candidates:
            for cid, name in candidates:
                log(f"👉 Richtige IG_USER_ID wäre vermutlich: {cid} (@{name}) – bitte das Secret damit ersetzen.")
        else:
            log("👉 Kein Instagram-Business-Konto über diesen Schlüssel gefunden. Beim Erzeugen des Schlüssels "
                "Facebook-Seite UND Instagram-Konto freigeben und die Berechtigungen instagram_basic, "
                "instagram_content_publish, pages_show_list, pages_read_engagement, business_management wählen.")
        sys.exit(1)


def main():
    if not USER or not TOKEN:
        msg = "Secrets IG_USER_ID und IG_ACCESS_TOKEN fehlen (GitHub → Settings → Secrets and variables → Actions)."
        if MODE == "check":
            sys.exit(msg)
        log(f"⏸  {msg} Es wird nichts gepostet.")
        return

    if MODE == "check":
        diagnose()
        return

    posts = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or []
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    now = dt.datetime.now(TZ)
    log(f"Lauf {now:%Y-%m-%d %H:%M} ({TZ}) · Modus {MODE} · {len(posts)} Einträge im Plan")

    failed = False
    for p in posts:
        pid = str(p.get("id"))
        if state.get(pid, {}).get("status") in ("posted", "skipped_late"):
            continue
        errs, warns = validate(p)
        if errs:
            log(f"❌ {pid}: {'; '.join(errs)}")
            failed = True
            continue
        for w in warns:
            log(f"⚠️  {pid}: {w}")
        if p.get("approved") is not True:
            log(f"⏸  {pid}: nicht freigegeben (approved: true fehlt) – wird nicht gepostet")
            continue
        t, is_due = due(p, now)
        if not is_due:
            log(f"🕒 {pid}: geplant für {t:%d.%m. %H:%M}")
            continue
        if now - t > dt.timedelta(hours=MAX_LATE_H):
            log(f"⏭  {pid}: mehr als {MAX_LATE_H:g} h überfällig – wird nicht mehr gepostet")
            state[pid] = {"status": "skipped_late", "at": now.isoformat()}
            continue

        log(f"▶ {pid}: {p['type']} mit {len(p['media'])} Datei(en) ist fällig")
        try:
            rel = prepare_media(p["media"])
            if MODE != "publish":
                for r in rel:
                    log(f"   (dry-run) {raw_url(r) if not r.startswith('http') else r}")
                continue
            git_push(f"Instagram: JPEG für {pid}")
            urls = [r if r.startswith("http") else raw_url(r) for r in rel]
            wait_public(urls)
            mid, link = publish(p, urls)
            state[pid] = {"status": "posted", "media_id": mid, "permalink": link, "at": now.isoformat()}
            log(f"✅ {pid} veröffentlicht {link}")
        except Exception as e:  # weiter mit dem nächsten Post, Lauf aber als Fehler markieren
            failed = True
            state[pid] = {"status": "error", "error": str(e)[:500], "at": now.isoformat()}
            log(f"❌ {pid}: {e}")

    if MODE == "publish":
        STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        git_push("Instagram: Status aktualisiert")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
