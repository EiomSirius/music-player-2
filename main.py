import os, re, uuid, json, shutil, subprocess, time, hashlib, mimetypes
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ============================================================================
#  RESONANCE — reproductor personal elevado
#  Backend FastAPI. Misma esencia que el original (biblioteca, subidas,
#  playlists, YouTube con título automático, reproducción con seek) pero con
#  estructura más clara, mejor código y más comodidades de API.
# ============================================================================

BASE = Path(os.environ.get("MUSIC_DIR", "/app/music"))
TRACKS = BASE / "tracks"
THUMBS = BASE / "thumbs"
LIB = BASE / "library.json"
for d in (TRACKS, THUMBS):
    d.mkdir(parents=True, exist_ok=True)

YT_PROXY = os.environ.get("YT_PROXY", "socks5://85.208.48.210:1080")
YT_EXTRACTOR_ARGS = "youtube:player_client=tv,web,mweb"
CACHE_DIR = Path(os.environ.get("YT_CACHE", "/app/music/.ytcache"))
CACHE_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Resonance Music Player", version="2.0")


# -------------------------------------------------------------------------
#  Persistencia
# -------------------------------------------------------------------------
def default_lib():
    return {"tracks": [], "playlists": []}


def load_lib():
    lib = default_lib()
    if LIB.exists():
        try:
            lib = json.loads(LIB.read_text(encoding="utf-8"))
        except Exception:
            lib = default_lib()
    lib.setdefault("tracks", [])
    lib.setdefault("playlists", [])
    _import_existing(lib)
    return lib


def save_lib(lib):
    tmp = LIB.with_suffix(".tmp")
    tmp.write_text(json.dumps(lib, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(LIB)


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return round(float(r.stdout.strip().split("=")[-1]))
    except Exception:
        return 0


def _import_existing(lib):
    """Reclama mp3 que estén sueltos en MUSIC_DIR (no dentro de tracks/)."""
    known = {t.get("file") for t in lib["tracks"]}
    changed = False
    for p in sorted(BASE.glob("*.mp3")):
        if p.name in known:
            continue
        tid = hashlib.md5(p.name.encode()).hexdigest()[:10]
        dest = TRACKS / p.name
        if not dest.exists():
            shutil.move(str(p), str(dest))
        tr = {
            "id": tid, "title": Path(p.name).stem, "artist": "",
            "file": p.name, "dur": probe_duration(dest),
            "thumb": "", "source": "import",
        }
        lib["tracks"].append(tr)
        known.add(p.name)
        changed = True
    if changed:
        save_lib(lib)


# -------------------------------------------------------------------------
#  Utilidades de medios
# -------------------------------------------------------------------------
def _norm_name(s):
    s = re.sub(r"[^\w\-. ]+", "", s or "").strip()
    return s or "track"


def _extract_audio(src: Path, out_mp3: Path, timeout=600):
    """Extrae la pista de audio como mp3. Devuelve True si OK."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(out_mp3)],
        capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0 and out_mp3.exists()


AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma")
VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v")


def _write_track(lib, *, title, artist, file_name, dur, thumb, source, extra):
    """Crea el registro de una pista ya escrita en disco y lo añade a la biblioteca."""
    tid = extra.pop("_tid", uuid.uuid4().hex[:10])
    tr = {
        "id": tid, "title": title, "artist": artist, "file": file_name,
        "dur": dur, "thumb": thumb, "source": source,
    }
    tr.update(extra)
    lib["tracks"].append(tr)
    save_lib(lib)
    return tr


# -------------------------------------------------------------------------
#  API: biblioteca
# -------------------------------------------------------------------------
@app.get("/api/library")
def get_library():
    return load_lib()


@app.get("/api/library/meta")
def library_meta():
    """Resumen numérico para la cabecera."""
    lib = load_lib()
    return {"count": len(lib["tracks"]), "playlists": len(lib["playlists"])}


@app.get("/api/tracks/{tid}/file")
def get_track(tid: str, range: Optional[str] = None):
    lib = load_lib()
    t = next((x for x in lib["tracks"] if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "no track")
    p = TRACKS / t["file"]
    if not p.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(p, media_type="audio/mpeg", filename=Path(t["file"]).name)


@app.get("/api/thumbs/{tid}")
def get_thumb(tid: str):
    p = THUMBS / (tid + ".jpg")
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


@app.post("/api/tracks/{tid}/edit")
def edit_track(tid: str, payload: dict = Body(...)):
    lib = load_lib()
    t = next((x for x in lib["tracks"] if x["id"] == tid), None)
    if not t:
        raise HTTPException(404)
    if isinstance(payload.get("title"), str) and payload["title"].strip():
        t["title"] = payload["title"].strip()
    if isinstance(payload.get("artist"), str):
        t["artist"] = payload["artist"].strip()
    save_lib(lib)
    return {"ok": True, "track": t}


@app.delete("/api/tracks/{tid}")
def del_track(tid: str):
    lib = load_lib()
    t = next((x for x in lib["tracks"] if x["id"] == tid), None)
    if not t:
        raise HTTPException(404)
    lib["tracks"] = [x for x in lib["tracks"] if x["id"] != tid]
    for pl in lib["playlists"]:
        pl["tracks"] = [x for x in pl["tracks"] if x != tid]
    (TRACKS / t["file"]).unlink(missing_ok=True)
    (THUMBS / (tid + ".jpg")).unlink(missing_ok=True)
    save_lib(lib)
    return {"ok": True}


# -------------------------------------------------------------------------
#  API: subida (audio o vídeo → audio)
# -------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...)):
    out = []
    for f in files:
        name = Path(f.filename or "audio.mp3").name
        raw = TRACKS / ("raw_" + uuid.uuid4().hex[:10] + Path(name).suffix.lower())
        raw.write_bytes(await f.read())
        ext = Path(name).suffix.lower()
        try:
            if ext in AUDIO_EXTS:
                tid = uuid.uuid4().hex[:10]
                fname = f"{tid}.mp3"
                if ext != ".mp3":
                    if not _extract_audio(raw, TRACKS / fname):
                        out.append({"error": f"{name}: no se pudo convertir"})
                        continue
                else:
                    shutil.copy(raw, TRACKS / fname)
                dur = probe_duration(TRACKS / fname)
                tr = _write_track(load_lib(), title=Path(name).stem, artist="",
                                  file_name=fname, dur=dur, thumb="", source="upload",
                                  extra={"_tid": tid})
                out.append(tr)
            elif ext in VIDEO_EXTS:
                tid = uuid.uuid4().hex[:10]
                fname = f"{tid}.mp3"
                if not _extract_audio(raw, TRACKS / fname):
                    out.append({"error": f"{name}: no se pudo extraer audio"})
                    continue
                dur = probe_duration(TRACKS / fname)
                tr = _write_track(load_lib(), title=Path(name).stem, artist="",
                                  file_name=fname, dur=dur, thumb="", source="upload",
                                  extra={"_tid": tid})
                out.append(tr)
            else:
                out.append({"error": f"{name}: formato no soportado"})
        finally:
            raw.unlink(missing_ok=True)
    added = [t for t in out if "error" not in t]
    errors = [t for t in out if "error" in t]
    return {"added": added, "errors": errors, "count": len(added)}


# -------------------------------------------------------------------------
#  API: YouTube
# -------------------------------------------------------------------------
COOKIES = Path(os.environ.get("YT_COOKIES", "/app/music/cookies.txt"))

def _yt_cmd(extra_list):
    cmd = ["yt-dlp", "--proxy", YT_PROXY, "--js-runtimes", "node",
           "--remote-components", "ejs:github",
           "--extractor-args", YT_EXTRACTOR_ARGS]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    return cmd + extra_list


def _yt_title(url: str):
    cmd = _yt_cmd(["--skip-download", "--no-playlist", "--no-warnings",
                   "--print", "%(title)s", url])
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
        if "429" in r.stderr:
            time.sleep(15 * (attempt + 1))
            continue
        break
    return ""


def _yt_download(url: str, tid: str):
    out_template = str(TRACKS / tid) + ".%(ext)s"
    cmd = _yt_cmd(["--no-part", "-f", "18/bestaudio/best", "-o", out_template,
                   "--write-thumbnail", "--convert-thumbnails", "jpg",
                   "--no-playlist", "--no-warnings", url])
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        video = next((f for f in TRACKS.glob(tid + ".*")
                      if f.suffix in (".mp4", ".webm", ".mkv")), None)
        if video:
            break
        if "429" in r.stderr or "Too Many Requests" in r.stderr:
            time.sleep(15 * (attempt + 1))
            continue
        return {"error": (r.stderr[-500:] if r.stderr else "descarga fallida")}
    if not video:
        return {"error": (r.stderr[-500:] if r.stderr else "descarga fallida")}

    mp3 = TRACKS / (tid + ".mp3")
    rr = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                         "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(mp3)],
                        capture_output=True, text=True, timeout=600)
    video.unlink(missing_ok=True)
    if rr.returncode != 0:
        return {"error": "conversión fallida"}
    for f in TRACKS.glob(tid + ".*"):
        if f.suffix in (".webp", ".png", ".jpeg"):
            f.unlink(missing_ok=True)
    return {"ok": True, "mp3": mp3.name}


class YTReq(BaseModel):
    url: str
    title: Optional[str] = None
    artist: Optional[str] = None


@app.post("/api/youtube")
def youtube(req: YTReq):
    if not req.url:
        raise HTTPException(400, "falta url")
    lib = load_lib()
    tid = uuid.uuid4().hex[:10]
    res = _yt_download(req.url, tid)
    if "error" in res:
        return JSONResponse({"error": res["error"]}, status_code=502)
    mp3 = TRACKS / res["mp3"]
    dur = probe_duration(mp3)
    thumb = tid if (THUMBS / (tid + ".jpg")).exists() else ""
    title = _yt_title(req.url) or req.title or "Video de YouTube"
    artist = req.artist or "YouTube"
    tr = {"id": tid, "title": title, "artist": artist, "file": res["mp3"],
          "dur": dur, "thumb": thumb, "source": "youtube", "url": req.url}
    lib["tracks"].append(tr)
    save_lib(lib)
    return {"track": tr}


# --- Reproducción con caché + seek (descarga parcial a /tmp) ---
def _yt_cache_file(url: str):
    key = hashlib.md5(url.encode()).hexdigest()[:12]
    m4a = CACHE_DIR / f"{key}.m4a"
    if m4a.exists():
        return m4a
    mp4 = CACHE_DIR / f"{key}.mp4"
    if not mp4.exists():
        r = subprocess.run(_yt_cmd(["--no-part", "-f", "18/bestaudio",
                                    "-o", str(mp4), url]),
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not mp4.exists():
            raise HTTPException(502, "no se pudo descargar: " +
                                (r.stderr[-200:] if r.stderr else "desconocido"))
    if not m4a.exists():
        rr = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                             "-vn", "-c:a", "copy", str(m4a)],
                            capture_output=True, text=True, timeout=600)
        if rr.returncode != 0:
            rr = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                                 "-vn", "-acodec", "aac", "-b:a", "192k", str(m4a)],
                                capture_output=True, text=True, timeout=600)
        mp4.unlink(missing_ok=True)
        if rr.returncode != 0 or not m4a.exists():
            raise HTTPException(502, "conversión fallida")
    return m4a


@app.get("/api/youtube/play")
def yt_play(url: str = "", title: str = ""):
    if not url:
        raise HTTPException(400, "falta url")
    out = _yt_cache_file(url)
    return FileResponse(out, media_type="audio/mp4",
                        headers={"Accept-Ranges": "bytes"})


@app.get("/api/youtube/prepare")
def yt_prepare(url: str = ""):
    if not url:
        raise HTTPException(400, "falta url")
    out = _yt_cache_file(url)
    return {"ok": True, "size": out.stat().st_size, "dur": probe_duration(out)}


# --- Streaming en vivo (sin tocar disco) ---
@app.get("/api/youtube/stream")
def yt_stream(url: str = "", title: str = ""):
    def gen():
        yt = subprocess.Popen(
            _yt_cmd(["--no-part", "-f", "18/bestaudio/best", "-o", "-", url]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ff = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-vn",
             "-acodec", "libmp3lame", "-q:a", "2", "-f", "mp3", "pipe:1"],
            stdin=yt.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if yt.stdout:
            yt.stdout.close()
        try:
            while True:
                chunk = ff.stdout.read(65536) if ff.stdout else b""
                if not chunk:
                    break
                yield chunk
        finally:
            for p in (ff, yt):
                try:
                    p.terminate()
                    p.wait(timeout=3)
                except Exception:
                    pass
    return StreamingResponse(gen(), media_type="audio/mpeg",
                             headers={"Content-Disposition": 'inline; filename="stream.mp3"'})


# -------------------------------------------------------------------------
#  API: playlists
# -------------------------------------------------------------------------
class PLReq(BaseModel):
    name: str


class PLAdd(BaseModel):
    track_ids: List[str]


class PLReorder(BaseModel):
    track_ids: List[str]


@app.get("/api/playlists")
def list_playlists():
    lib = load_lib()
    return lib["playlists"]


@app.post("/api/playlists")
def create_pl(req: PLReq):
    lib = load_lib()
    name = req.name.strip() or "Nueva lista"
    pid = uuid.uuid4().hex[:10]
    lib["playlists"].append({"id": pid, "name": name, "tracks": []})
    save_lib(lib)
    return {"id": pid, "name": name}


@app.post("/api/playlists/{pid}/tracks")
def add_to_pl(pid: str, req: PLAdd):
    lib = load_lib()
    pl = next((p for p in lib["playlists"] if p["id"] == pid), None)
    if not pl:
        raise HTTPException(404, "playlist no existe")
    for t in req.track_ids:
        if t not in pl["tracks"]:
            pl["tracks"].append(t)
    save_lib(lib)
    return {"ok": True, "count": len(pl["tracks"])}


@app.post("/api/playlists/{pid}/reorder")
def reorder_pl(pid: str, req: PLReorder):
    lib = load_lib()
    pl = next((p for p in lib["playlists"] if p["id"] == pid), None)
    if not pl:
        raise HTTPException(404)
    valid = set(pl["tracks"])
    new_order = [t for t in req.track_ids if t in valid]
    for t in pl["tracks"]:
        if t not in new_order:
            new_order.append(t)
    pl["tracks"] = new_order
    save_lib(lib)
    return {"ok": True}


@app.delete("/api/playlists/{pid}/tracks")
def remove_from_pl(pid: str, track_id: str = ""):
    lib = load_lib()
    pl = next((p for p in lib["playlists"] if p["id"] == pid), None)
    if not pl:
        raise HTTPException(404)
    if track_id:
        pl["tracks"] = [t for t in pl["tracks"] if t != track_id]
        save_lib(lib)
    return {"ok": True}


@app.delete("/api/playlists/{pid}")
def del_pl(pid: str):
    lib = load_lib()
    lib["playlists"] = [p for p in lib["playlists"] if p["id"] != pid]
    save_lib(lib)
    return {"ok": True}


# -------------------------------------------------------------------------
#  Estáticos
# -------------------------------------------------------------------------
STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
