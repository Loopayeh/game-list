#!/usr/bin/env python3
"""Game List — scan a folder of PS4/PS5 games, show a table, export customer PDF.

Parse engine is imported from pkg-viewer (no copy): D:/Hermes/pkg-viewer/pkgviewer.py
"""
import concurrent.futures as _fut
import hashlib as _hl
import io
import json as _json
import os
import sys
import tempfile
import threading as _th
import time as _time

sys.path.insert(0, os.path.join("D:", os.sep, "Hermes", "pkg-viewer"))
from pkgviewer import parse_pkg, read_entry_bytes, fmt_size  # noqa: E402

SUPPORTED_EXTS = (".pkg", ".exfat", ".ffpfsc", ".ffpkg")

SORTS = {
    "size": ("Size ↓", lambda s: (-(s.get("size", 0) or 0),
                                  (s.get("title") or "").lower())),
    "title": ("Title A–Z", lambda s: ((s.get("title") or "").lower(),)),
    "added": ("Newest added", lambda s: (-(s.get("ctime", 0) or 0),
                                         (s.get("title") or "").lower())),
}

FMTS = ("pkg", "exfat", "ffpfsc", "ffpkg", "folder")

FA_WORD = "farsi"

SHOP_NAME = "Loopayeh"

APP_VERSION = "v1.0.0"  # bump on every release — the updater compares this
UPDATE_REPO = "Loopayeh/game-list"
UPDATE_EXE = "GameList.exe"


def is_farsi(path):
    """True if the file/folder name contains 'Farsi' (any case)."""
    return FA_WORD in os.path.basename(path.rstrip("/\\")).lower()


def fmt_of_path(path):
    """Short format tag: pkg / exfat / ffpfsc / ffpkg / folder."""
    if os.path.isdir(path):
        return "folder"
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return ext if ext in FMTS else ext or "?"


# Same colors as the pkg-viewer format badge.
FMT_COLORS = {
    "exfat": "#e2f985",
    "ffpfsc": "#b693f1",
    "ffpkg": "#e17b7b",
    "folder": "#91c8f6",
    "pkg-ps5": "#91c8f6",
    "pkg-ps4": "#9efd88",
}


def console_of(it):
    """ps4 / ps5 from platform text or title id (CUSA = PS4, PPSA = PS5)."""
    pl = (it.get("platform") or "").lower()
    if "ps4" in pl or "cnt" in pl:
        return "ps4"
    tid = (it.get("title_id") or "").upper()
    if tid.startswith("CUSA"):
        return "ps4"
    return "ps5"


def norm_ptype(t):
    """Normalize a package Type string -> game / dlc / patch."""
    tl = (t or "").strip().lower()
    if ("dlc" in tl or "addcont" in tl or "additional" in tl
            or "add-on" in tl or "addon" in tl or "(ac)" in tl):
        return "dlc"
    if "patch" in tl or "update" in tl or "(gp)" in tl:
        return "patch"
    return "game"


def is_extra(it):
    """True for DLC / update packages (hidden by the Hide DLC tick)."""
    return (it.get("ptype") or "") in ("dlc", "patch")


def fmt_tag(it):
    """Color tag for an item: pkg splits into ps4/ps5 via platform."""
    f = (it.get("fmt") or "?").lower()
    if f == "pkg":
        pl = (it.get("platform") or "").lower()
        return "pkg-ps4" if ("ps4" in pl or "cnt" in pl) else "pkg-ps5"
    return f if f in FMT_COLORS else "?"


def fmt_color(it):
    return FMT_COLORS.get(fmt_tag(it), "#6b7280")


def cover_bytes_of(r, limit=8_000_000):
    """Return PNG bytes of icon0.png (or first PNG), or b''."""
    ents = r.get("entries", []) if isinstance(r, dict) else []
    ordered = sorted(ents, key=lambda e: (e.get("name", "").lower() != "icon0.png",
                                          e.get("name", "")))
    for e in ordered:
        nm = (e.get("name") or "")
        if not nm.lower().endswith(".png"):
            continue
        try:
            if e.get("local_path") and os.path.isfile(e["local_path"]):
                with open(e["local_path"], "rb") as fh:
                    data = fh.read(limit)
            elif e.get("cached") is not None:
                data = bytes(e["cached"][:limit])
            else:
                data = read_entry_bytes(r.get("path", ""), e.get("abs_off"),
                                        e.get("size", 0), limit=limit)
        except Exception:
            continue
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return data
    return b""


# Cache: single JSON file next to the game-list repo.
# Key = abspath + size + mtime. Everything scan needs lives here:
# parsed title/id/region/version + cover path on disk. Cover PNGs are
# written once to the cache dir, so a re-scan never re-reads the image.
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".gamelist_cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "cache.json")
_CACHE_LOCK = _th.Lock()


def _cache_load():
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as fh:
            return _json.load(fh)
    except Exception:
        return {}


def _cache_save(cache):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        _tmp = _CACHE_FILE + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as fh:
            _json.dump(cache, fh)
        os.replace(_tmp, _CACHE_FILE)
    except Exception:
        pass


def _cache_key(path):
    try:
        st = os.stat(path) if not os.path.isdir(path) else None
        if st is not None:
            return f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
        # app folder: key on param.json mtime + total file count
        pj = os.path.join(path, "sce_sys", "param.json")
        pst = os.stat(pj)
        n = sum(len(fns) for _, _, fns in os.walk(path))
        return f"{os.path.abspath(path)}|dir:{n}|{int(pst.st_mtime)}"
    except OSError:
        return f"{os.path.abspath(path)}|unknown"


def summarize(path, cache=None, save_cover=True):
    """Parse one game path -> dict for the list. Never raises.

    cache: dict from _cache_load() (shared, lock-protected on write).
    On a cache hit this returns in milliseconds without touching the image.
    """
    key = _cache_key(path)
    hit = cache.get(key) if cache is not None else None
    if hit and "ptype" in hit and (not hit.get("cover_file")
                or os.path.isfile(hit["cover_file"])):
        d = dict(hit)
        d["path"] = path
        # backfill fields added after this entry was cached
        d["fmt"] = fmt_of_path(path)
        if not d.get("ctime"):
            try:
                _st = os.stat(path)
                d["ctime"], d["mtime"] = _st.st_ctime, _st.st_mtime
                import datetime as _dt2
                d["added"] = _dt2.datetime.fromtimestamp(
                    _st.st_ctime).strftime("%Y-%m-%d")
                with _CACHE_LOCK:
                    _u = dict(cache.get(key, {}))
                    _u.update({"fmt": d["fmt"], "ctime": d["ctime"],
                               "mtime": d["mtime"], "added": d["added"]})
                    cache[key] = _u
            except OSError:
                pass
        d["cover"] = b""
        d["fa"] = is_farsi(path)
        if d.get("cover_file"):
            try:
                with open(d["cover_file"], "rb") as fh:
                    d["cover"] = fh.read()
            except OSError:
                pass
        d.pop("cover_file", None)
        return d
    try:
        r = parse_pkg(path)
    except Exception as e:
        return {"path": path, "error": str(e)}
    if not isinstance(r, dict) or not r.get("ok"):
        err = r.get("error", "?") if isinstance(r, dict) else "parse failed"
        return {"path": path, "error": err}
    rd = dict(r.get("rows", []))
    version = (rd.get("Content Ver") or rd.get("Version")
               or rd.get("Master Ver") or "-")
    cover = cover_bytes_of(r)
    try:
        _st = os.stat(path)
        _ctime, _mtime = _st.st_ctime, _st.st_mtime
    except OSError:
        _ctime = _mtime = 0.0
    import datetime as _dt
    _added = _dt.datetime.fromtimestamp(_ctime).strftime("%Y-%m-%d") if _ctime else "-"
    out = {"path": path,
           "title": r.get("title") or os.path.basename(path.rstrip("/\\")),
           "title_id": rd.get("Title ID", "-") or "-",
           "region": rd.get("Region", "-") or "-",
           "version": version,
           "size": r.get("size", 0),
           "size_str": fmt_size(r.get("size", 0)),
           "platform": rd.get("Platform", "-"),
           "ctime": _ctime, "mtime": _mtime, "added": _added,
           "fmt": fmt_of_path(path), "fa": is_farsi(path),
           "ptype": norm_ptype(rd.get("Type", "")),
           "cover": cover}
    if cache is not None and save_cover:
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            cf = os.path.join(
                _CACHE_DIR,
                _hl.md5(key.encode("utf-8", "replace")).hexdigest() + ".png")
            if cover:
                with open(cf, "wb") as fh:
                    fh.write(cover)
            stored = {k: v for k, v in out.items() if k != "cover"}
            stored["cover_file"] = cf if cover else ""
            with _CACHE_LOCK:
                cache[key] = stored
        except Exception:
            pass
    return out


def is_game_dir(full):
    return (os.path.isdir(full)
            and os.path.isfile(os.path.join(full, "sce_sys", "param.json")))


def list_drives():
    """All mounted Windows drives, e.g. ['C:/', 'D:/', 'H:/']."""
    out = []
    for _L in "DEFGHIJKLMNOPQRSTUVWXYZAB":  # C: is the Windows drive, never games
        _p = _L + ":/"
        if os.path.isdir(_p):
            out.append(_p)
    return out


_SKIP_DIRS = {"system volume information", "$recycle.bin", "windows",
              "program files", "program files (x86)", "programdata",
              "perflogs", "recovery", "esd", "config.msi", "msocache",
              "$windows.~bt", "$windows.~ws", "$winreagent"}


def _collect_targets(root, depth, want):
    """Find games under root, searching *depth* folder levels.

    depth=1: top level only. depth=2: + one level of subfolders, etc.
    System folders (Windows, Program Files, ...) are never descended into.
    """
    targets, seen = [], set()

    def _add(nm, full):
        ap = os.path.abspath(full).lower()
        if ap not in seen:
            seen.add(ap)
            targets.append((nm, full))

    stack = [(root, max(1, depth))]
    while stack:
        cur, d = stack.pop()
        try:
            names = os.listdir(cur)
        except OSError:
            continue
        for nm in names:
            full = os.path.join(cur, nm)
            try:
                if os.path.isfile(full):
                    if not nm.lower().endswith(SUPPORTED_EXTS):
                        continue
                    if (want and os.path.splitext(nm)[1].lower()
                            .lstrip(".") not in want):
                        continue
                    _add(nm, full)
                elif os.path.isdir(full):
                    if nm.lower() in _SKIP_DIRS:
                        continue
                    if is_game_dir(full):
                        if want and "folder" not in want:
                            continue
                        _add(nm, full)
                    elif d > 1:
                        stack.append((full, d - 1))
            except OSError:
                continue
    return targets


def scan_folder(root, workers=8, progress=None, cache=None, save=True,
                sort="size", fmts=None, farsi_only=False, depth=2,
                fresh=False):
    """Scan for games: supported files + app-folder dirs. Parallel + cached.

    root: one folder path, or a list of them (e.g. all drives).
    workers: parallel parses (I/O-bound, threads are fine).
    progress(done, total, name): called from worker threads after each item.
    cache: shared dict (loads its own if None). save: persist cache at end.
    sort: key into SORTS (size / title / added).
    fmts: iterable of format tags to keep (pkg/exfat/ffpfsc/ffpkg/folder),
          or None for all.
    farsi_only: keep only items whose file/folder name contains 'Farsi'.
    depth: folder levels to search (1 = top level, 2 = + subfolders, ...).
    fresh: ignore the disk cache and re-parse everything (dead entries
      for deleted games are pruned from the cache file).
    Returns (items, errors).
    """
    items, errors = [], []
    want = set(f.lower() for f in fmts) if fmts else None
    roots = [root] if isinstance(root, str) else list(root)
    targets = []
    for _rt in roots:
        if not _rt or not os.path.isdir(_rt):
            errors.append(f"{_rt}: folder not found")
            continue
        targets.extend(_collect_targets(_rt, max(1, depth), want))
    total = len(targets)
    if cache is None:
        cache = {} if fresh else _cache_load()
    done = [0]
    lock = _th.Lock()

    def one(pair):
        nm, full = pair
        s = summarize(full, cache=cache)
        with lock:
            done[0] += 1
            n = done[0]
        if progress:
            try:
                progress(n, total, nm)
            except Exception:
                pass
        return nm, s

    with _fut.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for nm, s in ex.map(one, targets):
            if "error" in s and "title" not in s:
                errors.append(f"{nm}: {s['error']}")
            elif farsi_only and not s.get("fa"):
                continue
            else:
                items.append(s)
    key_fn = SORTS.get(sort, SORTS["size"])[1]
    items.sort(key=key_fn)
    if save:
        _cache_save(cache)
    return items, errors


LIB_EXT = ".gamelist"


def save_library(items, dest):
    import base64
    data = {"app": "game-list", "v": 1, "items": []}
    for it in items:
        d = {k: v for k, v in it.items() if k != "cover"}
        cv = it.get("cover") or b""
        if cv:
            try:
                d["cover_b64"] = base64.b64encode(cv).decode("ascii")
            except Exception:
                pass
        data["items"].append(d)
    with open(dest, "w", encoding="utf-8") as fh:
        _json.dump(data, fh)
    return dest


def load_library(path):
    import base64
    with open(path, "r", encoding="utf-8") as fh:
        data = _json.load(fh)
    raw = data.get("items", []) if isinstance(data, dict) else []
    items = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        it = dict(d)
        b64 = it.pop("cover_b64", "")
        try:
            it["cover"] = base64.b64decode(b64) if b64 else b""
        except Exception:
            it["cover"] = b""
        it.setdefault("fmt", "?")
        it.setdefault("fa", False)
        it.setdefault("ptype", "")
        it.setdefault("size", 0)
        it.setdefault("size_str", fmt_size(it.get("size", 0)))
        it.setdefault("title", os.path.basename(
            (it.get("path") or "").rstrip("/\\")) or "-")
        it.setdefault("title_id", "-")
        it.setdefault("version", "-")
        items.append(it)
    return items


def export_pdf(items, dest, shop_name=None, theme="light"):
    """Customer PDF (English): cover + title/ID/version/format/size."""
    shop_name = SHOP_NAME  # fixed shop branding, not user-changeable
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Image as RLImage, Paragraph, Spacer)
    from reportlab.lib.utils import ImageReader  # noqa: F401 (validates PNGs)
    from datetime import date
    from PIL import Image as PILImage

    dark = (theme or "light").lower() == "dark"
    page_bg = colors.HexColor("#171717") if dark else colors.white
    body_fg = colors.white if dark else colors.black
    title_fg = colors.white if dark else colors.black
    mono_fg = colors.HexColor("#aab2c5") if dark else colors.HexColor("#333a45")
    sub_fg = colors.HexColor("#8b93a5")
    head_bg = colors.HexColor("#404040") if dark else colors.HexColor("#1a1e26")
    line_col = colors.HexColor("#404040") if dark else colors.HexColor("#d4d9e0")

    styles = getSampleStyleSheet()
    cell = styles["Normal"]
    cell.fontSize = 9
    cell.leading = 11
    cell.textColor = body_fg
    title_cell = styles["Normal"]
    title_cell.fontSize = 10
    title_cell.leading = 12
    title_cell.fontName = "Helvetica-Bold"
    title_cell.textColor = title_fg
    mono = styles["Normal"]
    mono.fontSize = 8.5
    mono.leading = 10.5
    mono.fontName = "Helvetica"
    mono.textColor = mono_fg
    hdr = styles["Heading1"]
    hdr.fontSize = 20
    hdr.spaceAfter = 2
    hdr.textColor = title_fg
    sub = styles["Normal"]
    sub.fontSize = 10
    sub.leading = 13
    sub.textColor = sub_fg
    total_style = styles["Normal"]
    total_style.fontSize = 11
    total_style.leading = 14
    total_style.fontName = "Helvetica-Bold"
    total_style.textColor = title_fg
    total_style.alignment = 2

    tmp = tempfile.mkdtemp(prefix="gamelist_")
    tmp_files = []

    def thumb_png(png_bytes, box=(96, 96)):
        try:
            im = PILImage.open(io.BytesIO(png_bytes)).convert("RGB")
            im.thumbnail(box)
            p = os.path.join(tmp, f"cv{len(tmp_files)}.png")
            im.save(p, "PNG")
            tmp_files.append(p)
            return p
        except Exception:
            return None

    doc = SimpleDocTemplate(dest, pagesize=A4,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title=shop_name)
    def _bg(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(page_bg)
        canvas.rect(0, 0, A4[0], A4[1], stroke=0, fill=1)
        canvas.restoreState()
    _logo_file = None
    try:
        import sys as _sys2
        _here2 = os.path.dirname(os.path.abspath(__file__))
        _cands = [os.path.join(os.getcwd(), "assets", "logo.png"),
                  os.path.join(_here2, "assets", "logo.png")]
        if getattr(_sys2, "frozen", False):
            _cands.insert(0, os.path.join(os.path.dirname(_sys2.executable),
                                          "assets", "logo.png"))
            _cands.insert(0, os.path.join(_sys2._MEIPASS, "assets",
                                          "logo.png"))
        for _lp in _cands:
            if os.path.isfile(_lp):
                _lim = PILImage.open(_lp).convert("RGB")
                _lim.thumbnail((140, 140))
                _logo_file = os.path.join(tmp, "shoplogo.png")
                _lim.save(_logo_file, "PNG")
                tmp_files.append(_logo_file)
                break
    except Exception:
        _logo_file = None
    _head_left = (RLImage(_logo_file, width=22 * mm, height=22 * mm)
                  if _logo_file else "")
    _head_right = [Paragraph(shop_name, hdr),
                   Paragraph(f"{date.today().isoformat()} · "
                             f"{len(items)} games", sub)]
    _head = Table([[_head_left, _head_right]],
                  colWidths=[28 * mm, 150 * mm])
    _head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("BACKGROUND", (0, 0), (-1, -1), page_bg),
    ]))
    story = [_head, Spacer(1, 8)]

    header = ["Cover", "Game", "Format", "Size"]
    data = [header]
    for it in items:
        cv = it.get("cover") or b""
        tp = thumb_png(cv, box=(128, 128)) if cv else None
        cell_img = RLImage(tp, width=17 * mm, height=17 * mm) if tp else ""
        game = [Paragraph(it.get("title", "-"), title_cell),
                Paragraph(f"{it.get('title_id', '-')} · v{it.get('version', '-')}",
                          mono)]
        if it.get("fa"):
            game.append(Paragraph('<font color="#9efd88"><b>FA subtitle</b></font>',
                                  mono))
        _fc = fmt_color(it)
        data.append([cell_img, game,
                     Paragraph(f'<font color="{_fc}"><b>{it.get("fmt", "?")}</b></font>',
                               cell),
                     Paragraph(f"<b>{it.get('size_str', '-')}</b>", cell)])
    widths = [24 * mm, 100 * mm, 24 * mm, 28 * mm]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (3, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, head_bg),
        ("LINEBELOW", (0, 1), (-1, -2), 0.5, line_col),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (1, 1), (1, -1), 8),
        ("BACKGROUND", (0, 1), (-1, -1), page_bg),
        ("TEXTCOLOR", (0, 1), (-1, -1), body_fg),
    ]))
    story.append(t)
    _n = len(items)
    _tot = sum((it.get("size", 0) or 0) for it in items)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Total: %d game%s · %s" % (_n, "" if _n == 1 else "s", fmt_size(_tot)),
        total_style))
    foot = styles["Normal"]
    foot.fontSize = 8
    foot.leading = 10
    foot.textColor = sub_fg
    foot.alignment = 1
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        'Made with Game List · <a '
        'href="https://github.com/Loopayeh/game-list" color="#91c8f6">'
        'github.com/Loopayeh/game-list</a>', foot))
    doc.build(story, onFirstPage=_bg, onLaterPages=_bg)
    for p in tmp_files:
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return dest


# ---------------- GUI ----------------
def _local_logo(size=(40, 40)):
    """Load local assets/logo.png if present (gitignored, never committed)."""
    try:
        from PIL import Image as _I
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [os.path.join(os.getcwd(), "assets", "logo.png"),
                 os.path.join(here, "assets", "logo.png")]
        import sys as _sys
        if getattr(_sys, "frozen", False):
            cands.insert(0, os.path.join(os.path.dirname(_sys.executable),
                                         "assets", "logo.png"))
            cands.insert(0, os.path.join(_sys._MEIPASS, "assets",
                                         "logo.png"))
        for p in cands:
            if os.path.isfile(p):
                im = _I.open(p).convert("RGB")
                im.thumbnail(size)
                return im
        return None
    except Exception:
        return None
BG, CARD, CARD2, ACCENT = "#171717", "#202020", "#2a2a2a", "#91c8f6"
TEXT, MUTED = "#f1f3f8", "#8b93a5"
FONT = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)


def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk
    try:
        from PIL import Image as PILImage, ImageTk
        has_pil = True
    except ImportError:
        has_pil = False

    state = {"items": [], "all_items": [], "photos": [], "folder": "",
             "sort": "size", "fmts": list(FMTS), "pdf_theme": "dark"}

    root = tk.Tk()
    root.title("Game List %s  •  PS4 / PS5" % APP_VERSION)
    try:
        import sys as _sys
        _ic = os.path.join(getattr(_sys, "_MEIPASS",
                           os.path.dirname(_sys.executable)
                           if getattr(_sys, "frozen", False) else "."),
                           "assets", "logo.ico")
        if os.path.isfile(_ic):
            root.iconbitmap(_ic)
    except Exception:
        pass
    root.geometry("1060x700")
    root.configure(bg=BG)
    root.minsize(900, 600)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT, font=FONT)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=FONT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
    style.configure("Accent.TButton", background=ACCENT, foreground="#171717",
                    font=FONT, borderwidth=0, padding=(16, 9))
    style.map("Accent.TButton", background=[("active", "#7ab5e8")])
    style.configure("Ghost.TButton", background="#404040", foreground=TEXT,
                    font=FONT, borderwidth=0, padding=(12, 7))
    style.map("Ghost.TButton", background=[("active", "#4d4d4d")])
    style.configure("Drive.TButton", background="#b693f1", foreground="#171717",
                    font=FONT, borderwidth=0, padding=(12, 7))
    style.map("Drive.TButton", background=[("active", "#a17fe8")])
    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, font=FONT, rowheight=30, borderwidth=0)
    style.configure("Treeview.Heading", background=CARD2, foreground=MUTED,
                    font=FONT_SMALL)
    style.map("Treeview", background=[("selected", ACCENT)],
              foreground=[("selected", "#171717")])

    toolbar = ttk.Frame(root, padding=(16, 12, 16, 4))
    toolbar.pack(fill="x")
    header = toolbar
    try:
        _lg = _local_logo((36, 36))
        if _lg is not None:
            from PIL import ImageTk as _ITk
            _lph = _ITk.PhotoImage(_lg)
            state["logo_photo"] = _lph
            tk.Label(header, image=_lph, bg=BG).pack(side="left",
                                                     padx=(0, 12))
    except Exception:
        pass
    ttk.Label(header, text="Scan:", style="Muted.TLabel").pack(side="left")
    ttk.Button(header, text="Browse...", style="Ghost.TButton",
               command=lambda: browse()).pack(side="left", padx=(6, 0))
    ttk.Button(header, text="Scan", style="Accent.TButton",
               command=lambda: scan_now()).pack(side="left", padx=(8, 0))
    ttk.Button(header, text="Drives...", style="Drive.TButton",
               command=lambda: scan_drives()).pack(side="left", padx=(8, 0))
    ttk.Separator(header, orient="vertical").pack(side="left", fill="y",
                                                  padx=(12, 6), pady=2)
    ttk.Label(header, text="PDF:", style="Muted.TLabel").pack(side="left")
    ttk.Button(header, text="Export PDF", style="Ghost.TButton",
               command=lambda: export()).pack(side="left", padx=(6, 0))
    themevar = tk.StringVar(value="dark")
    themebox = ttk.Combobox(header, textvariable=themevar, state="readonly",
                            width=7, values=["dark", "light"])
    themebox.pack(side="left", padx=(8, 0))
    themebox.set("dark")
    def _on_theme(_e):
        state["pdf_theme"] = themevar.get()
    themebox.bind("<<ComboboxSelected>>", _on_theme)
    ttk.Separator(header, orient="vertical").pack(side="left", fill="y",
                                                  padx=(12, 6), pady=2)
    ttk.Label(header, text="Library:", style="Muted.TLabel").pack(
        side="left")
    ttk.Button(header, text="Save lib", style="Ghost.TButton",
               command=lambda: save_lib()).pack(side="left", padx=(6, 0))
    ttk.Button(header, text="Open lib", style="Ghost.TButton",
               command=lambda: open_lib()).pack(side="left", padx=(8, 0))
    filterbar = ttk.Frame(root, padding=(16, 2, 16, 4))
    filterbar.pack(fill="x")
    header = filterbar
    ttk.Label(header, text="Sort:", style="Muted.TLabel").pack(side="left")
    sortvar = tk.StringVar(value="size")
    sortbox = ttk.Combobox(header, textvariable=sortvar, state="readonly",
                           width=18, values=[f"{k} — {lbl}" for k, (lbl, _)
                                             in SORTS.items()])
    sortbox.set(f"size — {SORTS['size'][0]}")
    sortbox.pack(side="left", padx=(6, 0))
    def _on_sort(_e):
        state["sort"] = sortvar.get().split(" — ")[0].split(" ")[0]
        refresh()
    sortbox.bind("<<ComboboxSelected>>", _on_sort)
    ttk.Separator(header, orient="vertical").pack(side="left", fill="y",
                                                  padx=(10, 6), pady=2)
    ttk.Label(header, text="Format:", style="Muted.TLabel").pack(
        side="left")
    fmtbtns = {}
    for _f in FMTS:
        _v = tk.BooleanVar(value=True)
        _cb = tk.Checkbutton(header, text=_f, variable=_v, bg=BG, fg=TEXT,
                             selectcolor=CARD2, activebackground=BG,
                             activeforeground=TEXT, font=FONT_SMALL,
                             command=lambda: refresh())
        _cb.pack(side="left", padx=(4, 0))
        fmtbtns[_f] = _v
    state["fmtbtns"] = fmtbtns
    ttk.Separator(header, orient="vertical").pack(side="left", fill="y",
                                                  padx=(10, 6), pady=2)
    _fav = tk.BooleanVar(value=False)
    tk.Checkbutton(header, text="Farsi only", variable=_fav, bg=BG, fg="#9efd88",
                   selectcolor=CARD2, activebackground=BG,
                   activeforeground="#9efd88", font=FONT_SMALL,
                   command=lambda: refresh()).pack(side="left", padx=(8, 0))
    _ps4v = tk.BooleanVar(value=True)
    tk.Checkbutton(header, text="PS4", variable=_ps4v, bg=BG, fg="#9efd88",
                   selectcolor=CARD2, activebackground=BG,
                   activeforeground="#9efd88", font=FONT_SMALL,
                   command=lambda: refresh()).pack(side="left", padx=(8, 0))
    _ps5v = tk.BooleanVar(value=True)
    tk.Checkbutton(header, text="PS5", variable=_ps5v, bg=BG, fg="#91c8f6",
                   selectcolor=CARD2, activebackground=BG,
                   activeforeground="#91c8f6", font=FONT_SMALL,
                   command=lambda: refresh()).pack(side="left", padx=(4, 0))
    state["ps4var"] = _ps4v
    state["ps5var"] = _ps5v
    state["farsivar"] = _fav
    _dlcv = tk.BooleanVar(value=False)
    tk.Checkbutton(header, text="Hide DLC", variable=_dlcv, bg=BG,
                   fg="#e17b7b", selectcolor=CARD2, activebackground=BG,
                   activeforeground="#e17b7b", font=FONT_SMALL,
                   command=lambda: refresh()).pack(side="left", padx=(8, 0))
    state["dlcvar"] = _dlcv
    pathrow = ttk.Frame(root, padding=(16, 0, 16, 2))
    pathrow.pack(fill="x")
    pathvar = tk.StringVar(value="Browse a folder (or Drives...), then press Scan")
    ttk.Label(pathrow, textvariable=pathvar,
              style="Muted.TLabel").pack(side="left")

    searchframe = ttk.Frame(pathrow)
    searchframe.pack(side="right")
    ttk.Label(searchframe, text="Search:", style="Muted.TLabel").pack(
        side="left")
    searchvar = tk.StringVar(value="")
    searchbox = ttk.Entry(searchframe, textvariable=searchvar, width=24)
    searchbox.pack(side="left", padx=(6, 0))
    def _clear_search():
        searchvar.set("")
        refresh()
        try:
            searchbox.focus_set()
        except Exception:
            pass
    ttk.Button(searchframe, text="\u00d7", width=2, style="Ghost.TButton",
               command=_clear_search).pack(side="left", padx=(4, 0))
    searchbox.bind("<KeyRelease>", lambda _e: refresh())
    state["searchvar"] = searchvar

    body = ttk.Frame(root, padding=(16, 4))
    body.pack(fill="both", expand=True)
    body.columnconfigure(1, weight=1)
    body.rowconfigure(0, weight=1)

    left = ttk.Frame(body, style="Card.TFrame", padding=18)
    left.grid(row=0, column=0, sticky="ns", padx=(0, 14))
    covervar = tk.Label(left, bg=CARD, fg=MUTED, text="No selection",
                        font=FONT, justify="center")
    covervar.pack()
    titlevar = tk.StringVar(value="—")
    tk.Label(left, textvariable=titlevar, bg=CARD, fg=TEXT,
             font=("Segoe UI", 12, "bold"), wraplength=260,
             justify="left").pack(pady=(8, 0), anchor="w")
    countvar = tk.StringVar(value="")
    tk.Label(left, textvariable=countvar, bg=CARD, fg=MUTED,
             font=FONT_SMALL).pack(anchor="w")
    selvar = tk.StringVar(value="")
    tk.Label(left, textvariable=selvar, bg=CARD, fg=ACCENT,
             font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 0))
    pickvar = tk.StringVar(value="")
    tk.Label(left, textvariable=pickvar, bg=CARD, fg="#9efd88",
             font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Button(left, text="Open folder", style="Ghost.TButton",
               command=lambda: open_folder()).pack(anchor="w",
                                                   pady=(10, 0))
    ttk.Button(left, text="Select all", style="Ghost.TButton",
               command=lambda: select_all()).pack(anchor="w",
                                                  pady=(6, 0))
    state["checked"] = set()
    state["pick_order"] = []
    state["pending"] = ""

    right = ttk.Frame(body)
    right.grid(row=0, column=1, sticky="nsew")
    right.rowconfigure(0, weight=1)
    right.columnconfigure(0, weight=1)
    tree = ttk.Treeview(right, columns=("pick", "titleid", "fmt", "version",
                                    "size"),
                        show="tree headings", selectmode="extended")
    tree.heading("#0", text="Title", anchor="w")
    tree.heading("pick", text="\u2713", anchor="center",
                 command=lambda: toggle_all())
    tree.heading("titleid", text="Title ID", anchor="w")
    tree.heading("fmt", text="Format", anchor="center")
    tree.heading("version", text="Version", anchor="center")
    tree.heading("size", text="Size", anchor="center")
    tree.column("#0", width=340, minwidth=220, stretch=True)
    tree.column("pick", width=34, minwidth=34, anchor="center",
                stretch=False)
    tree.column("titleid", width=110, minwidth=100, anchor="w",
                stretch=False)
    tree.column("fmt", width=80, minwidth=70, anchor="center",
                stretch=False)
    tree.column("version", width=90, minwidth=80, anchor="center",
                stretch=False)
    tree.column("size", width=120, minwidth=110, anchor="center",
                stretch=False)
    for _tag, _col in FMT_COLORS.items():
        try:
            tree.tag_configure(_tag, foreground=_col)
        except Exception:
            pass
    try:
        tree.tag_configure("sep", foreground="#6b7280")
    except Exception:
        pass
    sb = ttk.Scrollbar(right, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    bottombar = ttk.Frame(root)
    bottombar.pack(fill="x", side="bottom")
    statusvar = tk.StringVar(value="Ready")
    tk.Label(bottombar, textvariable=statusvar, bg=BG, fg=MUTED,
             font=FONT_SMALL, anchor="w", padx=12, pady=6).pack(
                 side="left", fill="x", expand=True)
    ttk.Button(bottombar, text="Check updates", style="Ghost.TButton",
               command=lambda: check_updates(manual=True)).pack(
                   side="right", padx=(0, 12), pady=4)

    def check_updates(manual=False):
        """Check GitHub releases for a newer build (stdlib only)."""
        try:
            import updater as _up
        except Exception as e:
            if manual:
                statusvar.set("Update check failed: %s" % e)
            return
        if manual:
            statusvar.set("Checking for updates...")

        def _done(info):
            def _ui():
                if not info:
                    if manual:
                        statusvar.set("No releases found (or offline)")
                    return
                try:
                    newer = _up.is_newer(info.get("tag", ""), APP_VERSION)
                except Exception:
                    newer = False
                if newer:
                    _show_update_dialog(info)
                elif manual:
                    statusvar.set("Up to date (%s)" % APP_VERSION)
            try:
                root.after(0, _ui)
            except Exception:
                pass
        _up.check_in_background(UPDATE_REPO, _done)

    def _show_update_dialog(info):
        try:
            import updater as _up
        except Exception:
            return
        tag = info.get("tag", "")
        dlg = tk.Toplevel(root)
        dlg.title("Update available")
        dlg.configure(bg=BG)
        try:
            dlg.transient(root)
            dlg.grab_set()
        except Exception:
            pass
        tk.Label(dlg, text="A new version is available:", bg=BG, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(dlg, text="%s  (you have %s)" % (info.get("name", tag),
                                                  APP_VERSION),
                 bg=BG, fg=TEXT, font=FONT).pack(anchor="w", padx=16)
        _body = (info.get("body", "") or "").strip().split("\n")
        _notes = "\n".join(_body[:12])
        if _notes:
            _tx = tk.Text(dlg, bg=CARD, fg=TEXT, font=FONT_SMALL,
                          wrap="word", borderwidth=0, padx=10, pady=10,
                          height=8, width=60)
            _tx.pack(fill="both", expand=True, padx=16, pady=(10, 0))
            _tx.insert("end", _notes)
            _tx.config(state="disabled")
        _prog = tk.StringVar(value="")
        tk.Label(dlg, textvariable=_prog, bg=BG, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", padx=16, pady=(6, 0))
        _btns = tk.Frame(dlg, bg=BG)
        _btns.pack(fill="x", padx=16, pady=14)

        def _dl():
            _asset = _up.pick_exe_asset(info, (UPDATE_EXE,))
            if not _asset:
                _prog.set("No .exe found in this release")
                return
            _prog.set("Downloading %s..." % _asset["name"])
            for _b in _btns.winfo_children():
                try:
                    _b.config(state="disabled")
                except Exception:
                    pass

            def _work():
                try:
                    import tempfile as _tf
                    _tmp = _tf.mkdtemp(prefix="update_")
                    _dest = os.path.join(_tmp, _asset["name"])

                    def _pg(got, total):
                        if total:
                            root.after(0, _prog.set,
                                       "Downloading... %d%%"
                                       % (got * 100 // total))
                    _up.download(_asset["url"], _dest, progress=_pg)
                except Exception as e:
                    root.after(0, _prog.set, "Download failed: %s" % e)
                    return

                def _fin():
                    try:
                        if _up.stage_and_restart(_dest):
                            try:
                                dlg.destroy()
                            except Exception:
                                pass
                            root.after(300, root.destroy)
                        else:
                            _prog.set("Saved to %s (dev mode)" % _dest)
                    except Exception as e:
                        _prog.set("Update failed: %s" % e)
                root.after(0, _fin)
            import threading as _th
            _th.Thread(target=_work, daemon=True).start()
        ttk.Button(_btns, text="Download + Restart",
                   style="Accent.TButton", command=_dl).pack(side="left")
        ttk.Button(_btns, text="Later", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="left", padx=(8, 0))

    def browse():
        d = filedialog.askdirectory(title="Select folder of games")
        if d:
            state["pending"] = d
            pathvar.set(d + "  -  press Scan")
            statusvar.set("Folder selected - press Scan to start")

    def _depth():
        return 2

    def scan_drives():
        import shutil
        drives = list_drives()
        if not drives:
            statusvar.set("No drives found")
            return
        dlg = tk.Toplevel(root)
        dlg.title("Choose drives to scan")
        dlg.configure(bg=BG)
        dlg.transient(root)
        dlg.grab_set()
        ttk.Label(dlg, text="Tick the drives to scan:",
                  style="Muted.TLabel").pack(anchor="w", padx=16,
                                             pady=(14, 6))
        _vars = []
        for _d in drives:
            try:
                _u = shutil.disk_usage(_d)
                _info = "%s  (%s free of %s)" % (
                    _d, fmt_size(_u.free), fmt_size(_u.total))
            except Exception:
                _info = _d
            _v = tk.BooleanVar(value=True)
            tk.Checkbutton(dlg, text=_info, variable=_v, bg=BG, fg=TEXT,
                           selectcolor=CARD2, activebackground=BG,
                           activeforeground=TEXT, font=FONT).pack(
                               anchor="w", padx=16)
            _vars.append((_d, _v))
        _btns = ttk.Frame(dlg)
        _btns.pack(fill="x", padx=16, pady=14)
        def _ok():
            sel = [_d for _d, _v in _vars if _v.get()]
            dlg.destroy()
            if not sel:
                statusvar.set("No drives ticked")
                return
            load(sel, _depth())
        ttk.Button(_btns, text="Scan", style="Accent.TButton",
                   command=_ok).pack(side="left")
        ttk.Button(_btns, text="Cancel", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="left", padx=(8, 0))

    def scan_now():
        d = state.get("pending") or state.get("folder")
        if not d:
            browse()
            return
        load(d, _depth())

    def refresh(focus="__keep__"):
        """Re-sort + re-filter the loaded items without re-scanning.

        focus: path to focus after rebuild, or "__keep__" (default) to
        keep the currently focused row so keyboard control never drops.
        """
        all_items = state.get("all_items", [])
        _chk = state.get("checked", set())
        # picked games are exempt from filters - they always stay visible
        picked = [it for it in all_items if it.get("path") in _chk]
        items = [it for it in all_items if it.get("path") not in _chk]
        want = {f for f, v in state.get("fmtbtns", {}).items()
                if v.get()} if state.get("fmtbtns") else set(FMTS)
        items = [it for it in items if it.get("fmt", "?") in want]
        _fav = state.get("farsivar")
        if _fav is not None and _fav.get():
            items = [it for it in items if it.get("fa")]
        _p4 = state.get("ps4var")
        _p5 = state.get("ps5var")
        if _p4 is not None and _p5 is not None and not (
                _p4.get() and _p5.get()):
            items = [it for it in items
                     if (_p4.get() and console_of(it) == "ps4")
                     or (_p5.get() and console_of(it) == "ps5")]
        _sv = state.get("searchvar")
        if _sv is not None:
            _q = _sv.get().strip().lower()
            if _q:
                items = [it for it in items
                         if _q in (it.get("title") or "").lower()
                         or _q in (it.get("title_id") or "").lower()]
        _dlc_hidden = 0
        _dlcv = state.get("dlcvar")
        if _dlcv is not None and _dlcv.get():
            _before = len(items)
            items = [it for it in items if not is_extra(it)]
            _dlc_hidden = _before - len(items)
        key_fn = SORTS.get(state.get("sort", "size"), SORTS["size"])[1]
        _pos = {p: i for i, p in enumerate(state.get("pick_order", []))}
        picked.sort(key=lambda it: _pos.get(it.get("path"), 10 ** 9))
        items.sort(key=key_fn)
        items = picked + items
        if focus == "__keep__":
            try:
                _fs = tree.selection()
                focus = (_row_path(_fs[0]) if _fs else None)
            except Exception:
                focus = None
        state["items"] = items
        state["_sep"] = None
        tree.delete(*tree.get_children())
        _rowmap, _rowrev = {}, {}
        for _i, it in enumerate(items):
            if picked and _i == len(picked) and len(items) > len(picked):
                _pst = sum((p.get("size", 0) or 0) for p in picked)
                _sepr = tree.insert("", "end",
                            text="-- %d picked \u00b7 %s --"
                            % (len(picked), fmt_size(_pst)),
                            values=("", "all games below", "", "", ""),
                            tags=("sep",))
                state["_sep"] = _sepr
            _is_pick = it.get("path") in _chk
            _mark = "\u2611" if _is_pick else "\u2610"
            _rid = tree.insert("", "end", text=it["title"][:80],
                        values=(_mark, it["title_id"], it.get("fmt", ""),
                                it["version"], it["size_str"]),
                        tags=(fmt_tag(it),))
            _rowmap[_rid] = _i
            _rowrev[_i] = _rid
        state["_rowmap"] = _rowmap
        state["_rowrev"] = _rowrev

        if focus:
            for _i, _it in enumerate(items):
                if _it.get("path") == focus:
                    _rid = state.get("_rowrev", {}).get(_i)
                    if _rid:
                        tree.focus(_rid)
                        tree.selection_set(_rid)
                        tree.see(_rid)
                    break
        update_pick()
        _tot = sum((it.get("size", 0) or 0) for it in items)
        _n = len(items)
        _cnt = "%d game%s · %s" % (_n, "" if _n == 1 else "s",
                                   fmt_size(_tot))
        if _dlc_hidden:
            _cnt += " (%d DLC hidden)" % _dlc_hidden
        countvar.set(_cnt)
        selvar.set("")
        if items:
            show_cover(0)

    def load(folder, depth=2, fresh=False):
        state["folder"] = folder
        state["pending"] = folder if isinstance(folder, str) else ""
        if isinstance(folder, str):
            pathvar.set((os.path.basename(folder) or folder)
                        + f"  (depth {depth})")
        else:
            pathvar.set("All drives (%d)  (depth %d)"
                        % (len(folder), depth))
        statusvar.set(("Refreshing (depth %d, fresh)..." if fresh
                       else "Scanning (depth %d)...") % depth)
        # progress bar row (created once)
        try:
            bar = state.get("bar")
            if bar is None:
                bar = ttk.Progressbar(right, mode="determinate", maximum=100)
                bar.pack(side="bottom", fill="x", pady=(4, 0))
                state["bar"] = bar
            bar["value"] = 0
        except Exception:
            bar = None
        t0 = _time.time()

        def prog(n, total, _nm):
            def _ui():
                try:
                    if bar is not None:
                        bar["value"] = 100.0 * n / max(1, total)
                    statusvar.set(f"Scanning {n}/{total}...")
                except Exception:
                    pass
            try:
                root.after(0, _ui)
            except Exception:
                pass

        def work():
            items, errors = scan_folder(folder, progress=prog, depth=depth,
                                          fresh=fresh)
            def _done():
                state["all_items"] = items
                state["checked"] = set()
                state["pick_order"] = []
                state["hist"] = []
                refresh()
                dt = _time.time() - t0
                base = f"OK - {len(items)} games in {dt:.0f}s"
                statusvar.set(base + (f", {len(errors)} skipped" if errors else ""))
                try:
                    if bar is not None:
                        bar["value"] = 100
                except Exception:
                    pass
            try:
                root.after(0, _done)
            except Exception:
                pass
        _th.Thread(target=work, daemon=True).start()

    def checked_items():
        _chk = state.get("checked", set())
        _ord = state.get("pick_order", [])
        _pos = {p: i for i, p in enumerate(_ord)}
        got = [it for it in state["items"] if it.get("path") in _chk]
        got.sort(key=lambda it: _pos.get(it.get("path"), 10 ** 9))
        return got

    def update_pick():
        pick = checked_items()
        if pick:
            _st = sum((it.get("size", 0) or 0) for it in pick)
            pickvar.set("\u2611 %d picked \u00b7 %s - Export gives these"
                        % (len(pick), fmt_size(_st)))
        else:
            pickvar.set("")

    def _flip(idx):
        items = state["items"]
        if idx < 0 or idx >= len(items):
            return None
        key = items[idx].get("path")
        _chk = state.setdefault("checked", set())
        _ord = state.setdefault("pick_order", [])
        if key in _chk:
            _chk.discard(key)
            try:
                _ord.remove(key)
            except ValueError:
                pass
        else:
            _chk.add(key)
            if key not in _ord:
                _ord.append(key)
        return key

    def _focus_path(path, fallback=0):
        items = state["items"]
        idx = fallback
        for i, it in enumerate(items):
            if it.get("path") == path:
                idx = i
                break
        rowid = state.get("_rowrev", {}).get(idx)
        if rowid:
            tree.focus(rowid)
            tree.selection_set(rowid)
            tree.see(rowid)

    def _push_hist():
        _h = state.setdefault("hist", [])
        _h.append((set(state.get("checked", set())),
                   list(state.get("pick_order", []))))
        if len(_h) > 50:
            del _h[0]

    def on_undo(_e=None):
        _h = state.get("hist", [])
        if not _h:
            statusvar.set("Nothing to undo")
            return "break"
        _c, _o = _h.pop()
        if isinstance(_c, set):  # old entries stored set only
            _o = [p for p in state.get("pick_order", []) if p in _c]
            _o += [p for p in _c if p not in _o]
        state["checked"] = set(_c)
        state["pick_order"] = list(_o)
        _first = _o[0] if _o else None
        refresh(_first)
        try:
            tree.focus_set()
        except Exception:
            pass
        _n = len(state.get("checked", set()))
        statusvar.set("Undone - %d picked" % _n)
        return "break"

    def toggle_row(rowid):
        idx = _item_index(rowid)
        if idx is None:
            return
        _push_hist()
        key = _flip(idx)
        if key is None:
            return
        refresh(key)

    def toggle_all():
        items = state["items"]
        if not items:
            return
        _push_hist()
        _chk = state.setdefault("checked", set())
        _ord = state.setdefault("pick_order", [])
        vis = [it.get("path") for it in items]
        if all(k in _chk for k in vis):
            for k in vis:
                _chk.discard(k)
                try:
                    _ord.remove(k)
                except ValueError:
                    pass
        else:
            for k in vis:
                if k not in _chk:
                    _chk.add(k)
                    _ord.append(k)
        refresh()

    def select_all():
        items = state.get("items", [])
        if not items:
            return
        _push_hist()
        _chk = state.setdefault("checked", set())
        _ord = state.setdefault("pick_order", [])
        for it in items:
            k = it.get("path")
            if k and k not in _chk:
                _chk.add(k)
                _ord.append(k)
        refresh()
        statusvar.set("%d picked" % len(_chk))

    def _item_index(rowid):
        if not rowid:
            return None
        try:
            return state.get("_rowmap", {}).get(rowid)
        except Exception:
            return None

    def _row_path(rowid):
        try:
            _ix = _item_index(rowid)
            if _ix is None:
                return None
            return state["items"][_ix].get("path")
        except Exception:
            return None

    def _drop_move(src, target_rowid):
        _chk = state.get("checked", set())
        _ord = state.get("pick_order", [])
        if not src or src not in _chk or src not in _ord:
            return
        tgt = _row_path(target_rowid)
        if target_rowid and target_rowid == state.get("_sep"):
            _push_hist()
            _ord.remove(src)
            _ord.append(src)
            refresh(src)
            statusvar.set("Moved to #%d of picked" % (_ord.index(src) + 1))
            return
        _push_hist()
        _i = _ord.index(src)
        _ord.remove(src)
        if tgt and tgt in _ord:
            _ord.insert(_ord.index(tgt), src)
        elif tgt == src or tgt is None:
            _ord.insert(min(_i, len(_ord)), src)
        else:
            _ord.append(src)
        refresh(src)
        statusvar.set("Moved to #%d of picked" % (_ord.index(src) + 1))

    def on_press(e):
        state["_drag"] = {"x": e.x, "y": e.y, "moved": False,
                          "src": _row_path(tree.identify_row(e.y))}

    def on_motion(e):
        _d = state.get("_drag")
        if not _d or not _d.get("src"):
            return
        if (not _d["moved"]
                and abs(e.x - _d["x"]) + abs(e.y - _d["y"]) < 6):
            return
        _d["moved"] = True
        _chk = state.get("checked", set())
        if _d["src"] not in _chk:
            return
        _tgt = _row_path(tree.identify_row(e.y))
        _ord = state.get("pick_order", [])
        if _tgt and _tgt in _ord:
            statusvar.set("Drop to move to #%d of picked"
                          % (_ord.index(_tgt) + 1))
        else:
            statusvar.set("Drop to move to end of picked")

    def on_release(e):
        _d = state.get("_drag") or {}
        state["_drag"] = None
        rowid = tree.identify_row(e.y)
        if _d.get("moved") and _d.get("src"):
            _drop_move(_d["src"], rowid)
            return
        if tree.identify_column(e.x) == "#1" and rowid:
            toggle_row(rowid)

    def on_space(_e):
        sel = tree.selection()
        if not sel:
            return
        idxs = []
        for rowid in sel:
            _ix = _item_index(rowid)
            if _ix is not None:
                idxs.append(_ix)
        if not idxs:
            return "break"
        _push_hist()
        before = state["items"]
        _last = max(idxs)
        nxt_path = (before[_last + 1].get("path")
                    if _last + 1 < len(before) else None)
        for _ix in idxs:
            try:
                _flip(_ix)
            except Exception:
                pass
        if nxt_path is not None:
            refresh(nxt_path)
        else:
            refresh()
            items = state["items"]
            if items:
                _focus_path(items[-1].get("path"), len(items) - 1)
        return "break"

    def on_arrow(_e, step):
        cur = tree.focus() or (tree.selection() or [""])[0]
        nxt = tree.next(cur) if step > 0 else tree.prev(cur)
        while nxt and _item_index(nxt) is None:
            nxt = tree.next(nxt) if step > 0 else tree.prev(nxt)
        if nxt:
            tree.focus(nxt)
            tree.selection_set(nxt)
            tree.see(nxt)
        return "break"

    def on_move(_e, step):
        sel = tree.selection()
        if not sel:
            return "break"
        try:
            _mix = _item_index(sel[0])
            if _mix is None:
                return "break"
            it = state["items"][_mix]
        except Exception:
            return "break"
        key = it.get("path")
        _ord = state.get("pick_order", [])
        if key not in state.get("checked", set()) or key not in _ord:
            return "break"
        i = _ord.index(key)
        j = i + step
        if 0 <= j < len(_ord):
            _push_hist()
            _ord[i], _ord[j] = _ord[j], _ord[i]
            refresh(key)
            statusvar.set("Moved to #%d of picked" % (j + 1))
        return "break"

    def on_double(e):
        if tree.identify_column(e.x) == "#1":
            return
        rowid = tree.identify_row(e.y)
        if rowid:
            tree.focus(rowid)
            tree.selection_set(rowid)
            open_folder()

    tree.bind("<ButtonPress-1>", on_press)
    tree.bind("<B1-Motion>", on_motion)
    tree.bind("<ButtonRelease-1>", on_release)
    tree.bind("<Double-1>", on_double)
    # Ctrl+Z is handled ONLY by _undo_press below (single handler, no
    # <Control-z> bindings anywhere) so one keypress = one undo step.
    # Ctrl+Z must win even when focus is in the search box (Entry eats
    # <Control-z> for its own text-undo) and on Persian layout (keysym
    # differs, physical keycode is the same). Widget-level KeyPress runs
    # before the Entry class binding, so returning "break" blocks it.
    def _undo_press(_e):
        try:
            if (_e.state & 0x4) and (_e.keycode == 90 or (_e.keysym or "").lower() == "z"):
                return on_undo(_e)
        except Exception:
            pass
        return None
    for _w in (searchbox, themebox, sortbox, tree):
        try:
            _w.bind("<KeyPress>", _undo_press, add="+")
        except Exception:
            pass
    try:
        root.bind_all("<KeyPress>", _undo_press, add="+")
    except Exception:
        pass
    tree.bind("<space>", on_space)
    tree.bind("<Down>", lambda e: on_arrow(e, 1))
    tree.bind("<Up>", lambda e: on_arrow(e, -1))
    tree.bind("<Alt-Up>", lambda e: on_move(e, -1))
    tree.bind("<Alt-Down>", lambda e: on_move(e, 1))

    def open_folder():
        sel = tree.selection()
        items = state["items"]
        if not sel or not items:
            statusvar.set("Select a game first")
            return
        try:
            _oix = _item_index(sel[0])
            if _oix is None:
                return
            it = items[_oix]
        except Exception:
            return
        p = it.get("path", "")
        if not p:
            return
        target = p if os.path.isdir(p) else (os.path.dirname(p) or p)
        try:
            os.startfile(target)
            statusvar.set("Opened " + target)
        except Exception as e:
            statusvar.set(f"Open failed: {e}")

    def show_cover(idx):
        items = state["items"]
        if not items or idx < 0 or idx >= len(items):
            return
        it = items[idx]
        titlevar.set(it["title"])
        if not has_pil:
            covervar.config(text="(Pillow not installed)", image="")
            return
        cv = it.get("cover") or b""
        if not cv:
            covervar.config(text="(no cover)", image="")
            return
        try:
            im = PILImage.open(io.BytesIO(cv)).convert("RGB")
            im.thumbnail((260, 260))
            ph = ImageTk.PhotoImage(im)
            state["photos"] = [ph]
            covervar.config(image=ph, text="")
            covervar.image = ph
        except Exception:
            covervar.config(text="(bad cover)", image="")

    def on_select(_e):
        sel = tree.selection()
        if state.get("_sep") and state["_sep"] in sel:
            tree.selection_remove(state["_sep"])
            sel = tree.selection()
        if sel:
            _six = _item_index(sel[0])
            if _six is not None:
                show_cover(_six)
            idxs = sorted(_ix for _ix in
                          (_item_index(i) for i in sel)
                          if _ix is not None)
            pick = [state["items"][i] for i in idxs
                    if 0 <= i < len(state["items"])]
            _st = sum((it.get("size", 0) or 0) for it in pick)
            selvar.set("%d selected · %s" % (len(pick), fmt_size(_st)))
        else:
            selvar.set("")

    tree.bind("<<TreeviewSelect>>", on_select)

    def sel_or_all():
        pick = checked_items()
        if pick:
            return pick, "%d picked" % len(pick)
        sel = tree.selection()
        if sel:
            idxs = sorted(_ix for _ix in
                          (_item_index(i) for i in sel)
                          if _ix is not None)
            got = [state["items"][i] for i in idxs
                   if 0 <= i < len(state["items"])]
            return got, "%d selected" % len(got)
        return state["items"], ""

    def export():
        items, tag = sel_or_all()
        if not items:
            statusvar.set("Nothing to export - open a folder first")
            return
        dest = filedialog.asksaveasfilename(
            title="Export customer PDF", defaultextension=".pdf",
            initialfile="game-list.pdf",
            filetypes=[("PDF", "*.pdf"), ("all", "*.*")])
        if not dest:
            return
        statusvar.set("Writing PDF...")
        root.update_idletasks()
        try:
            theme = state.get("pdf_theme", "dark")
            export_pdf(items, dest, theme=theme)
            _st = sum((it.get("size", 0) or 0) for it in items)
            extra = " (%s)" % tag if tag else ""
            statusvar.set("Saved %s - %d games · %s%s"
                          % (os.path.basename(dest), len(items),
                             fmt_size(_st), extra))
        except Exception as e:
            statusvar.set(f"PDF failed: {e}")

    def save_lib():
        items = state["all_items"]
        if not items:
            statusvar.set("Nothing to save - open a folder first")
            return
        dest = filedialog.asksaveasfilename(
            title="Save library snapshot", defaultextension=LIB_EXT,
            initialfile="my-games" + LIB_EXT,
            filetypes=[("Game list", "*" + LIB_EXT), ("all", "*.*")])
        if not dest:
            return
        try:
            save_library(items, dest)
            statusvar.set("Library saved - %d games (%s)"
                          % (len(items), os.path.basename(dest)))
        except Exception as e:
            statusvar.set(f"Save failed: {e}")

    def open_lib():
        src = filedialog.askopenfilename(
            title="Open library snapshot",
            filetypes=[("Game list", "*" + LIB_EXT),
                       ("JSON", "*.json"), ("all", "*.*")])
        if not src:
            return
        try:
            items = load_library(src)
        except Exception as e:
            statusvar.set(f"Open failed: {e}")
            return
        key_fn = SORTS.get(state.get("sort", "size"), SORTS["size"])[1]
        items.sort(key=key_fn)
        state["all_items"] = items
        state["checked"] = set()
        state["pick_order"] = []
        state["hist"] = []
        state["folder"] = ""
        pathvar.set(os.path.basename(src) + " (library)")
        refresh()
        _st = sum((it.get("size", 0) or 0) for it in items)
        statusvar.set("Library - %d games · %s (no HDD needed)"
                      % (len(items), fmt_size(_st)))

    root.mainloop()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(sys.argv) >= 3 and sys.argv[1] == "--scan":
        folder = sys.argv[2]
        sort = "size"
        fmts = None
        depth = 2
        fresh = False
        consoles = None
        search_q = ""
        hide_dlc = False
        theme = "light"
        farsi_only = False
        lib = None
        save_lib_path = None
        out = None
        for a in sys.argv[3:]:
            if a.startswith("--sort="):
                sort = a.split("=", 1)[1]
            elif a.startswith("--fmt="):
                fmts = [f.strip() for f in a.split("=", 1)[1].split(",") if f.strip()]
            elif a.startswith("--theme="):
                theme = a.split("=", 1)[1]
            elif a.startswith("--lib="):
                lib = a.split("=", 1)[1]
            elif a.startswith("--save-lib="):
                save_lib_path = a.split("=", 1)[1]
            elif a == "--farsi":
                farsi_only = True
            elif a == "--hide-dlc":
                hide_dlc = True
            elif a == "--fresh":
                fresh = True
            elif a.startswith("--console="):
                consoles = set(
                    c.strip() for c in a.split("=", 1)[1].split(",")
                    if c.strip())
            elif a.startswith("--search="):
                search_q = a.split("=", 1)[1].strip().lower()
            elif a.startswith("--depth="):
                try:
                    depth = max(1, min(4, int(a.split("=", 1)[1])))
                except Exception:
                    pass
            elif a.endswith(".pdf"):
                out = a
        def _console_filter(lst):
            if consoles:
                lst = [it for it in lst if console_of(it) in consoles]
            if hide_dlc:
                lst = [it for it in lst if not is_extra(it)]
            if search_q:
                lst = [it for it in lst
                       if search_q in (it.get("title") or "").lower()
                       or search_q in (it.get("title_id") or "").lower()]
            return lst

        if lib:
            items = load_library(lib)
            if fmts:
                want = set(f.lower() for f in fmts)
                items = [it for it in items
                         if (it.get("fmt", "?") or "?").lower() in want]
            if farsi_only:
                items = [it for it in items if it.get("fa")]
            items = _console_filter(items)
            items.sort(key=SORTS.get(sort, SORTS["size"])[1])
            errors = []
        else:
            roots = (list_drives()
                     if folder.upper() in ("ALL", "ALL-DRIVES", "DRIVES")
                     else folder)
            items, errors = scan_folder(roots, sort=sort, fmts=fmts,
                                        farsi_only=farsi_only, depth=depth,
                                        fresh=fresh)
            items = _console_filter(items)
            if save_lib_path and items:
                save_library(items, save_lib_path)
                print("LIB:", save_lib_path,
                      os.path.getsize(save_lib_path), "bytes")
        _allt = sum((it.get("size", 0) or 0) for it in items)
        print("games: %d  skipped: %d  total: %s"
              % (len(items), len(errors), fmt_size(_allt)))
        for it in items:
            print(f"  {it['title_id']} | {it['title'][:60]} | {it['region']} | "
                  f"{it['version']} | {it['size_str']} | {it.get('fmt','?')}")
        for e in errors[:10]:
            print("  SKIP:", e[:160])
        if out and items:
            export_pdf(items, out, theme=theme)
            print("PDF:", out, os.path.getsize(out), "bytes", f"[{theme}]")
    else:
        run_gui()
