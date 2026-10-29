"""
Lead Engine — web UI over the universal `leadgen` package.

Pick a vertical (what to prospect for) and a market (where), optionally paste
competitor pages to suppress or upload a CRM to de-dupe, then hit Run. Streams
live progress and serves the resulting CRM CSV + tiered XLSX for download, and
renders the top leads in-page.

Run:
    pip install -r gui/requirements.txt
    python gui/app.py            # then open the printed URL
    # or native window:  python gui/desktop_app.py
"""
import os
import sys
import time
import socket
import threading
import uuid
from datetime import datetime

from flask import Flask, request, jsonify, send_file, render_template_string

# Make the repo root importable so `import leadgen` works regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import leadgen
from leadgen import get_vertical, all_verticals, run_pipeline
from leadgen.geo import MARKETS
from leadgen.pipeline import load_crm_names
from leadgen.diagnostics import check_connectivity, friendly_error, explain_empty_result

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response

# In-memory job store: job_id -> {log, done, error, stats, leads, files}
JOBS: dict[str, dict] = {}
# Last few completed runs this session, for the "recent runs" panel (newest first).
RECENT: list[dict] = []
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_output")
os.makedirs(OUT_DIR, exist_ok=True)


# ───────────────────────────── job runner ────────────────────────────────────
def run_job(job_id: str, params: dict):
    job = JOBS[job_id]

    def log(msg):
        job["log"].append(f"[{datetime.now():%H:%M:%S}] {msg}")

    try:
        vertical = get_vertical(params["vertical"])
        demo = bool(params.get("demo"))
        market = (params.get("market") or "").strip() or ("(demo)" if demo else "")
        # Arvett MVP supports only the two relevant no-key map sources.
        requested_sources = params.get("sources") or ["osm"]
        sources = tuple(s for s in requested_sources if s in {"osm", "overture"}) or ("osm",)
        enrich = bool(params.get("enrich", True))
        try:
            limit = max(1, min(500, int(params.get("limit") or 100)))
        except (TypeError, ValueError):
            limit = 100
        try:
            enrich_cap = max(0, min(100, int(params.get("enrich_cap") or 20)))
        except (TypeError, ValueError):
            enrich_cap = 20
        category_filter = str(params.get("category_filter") or "all").lower()
        allowed_categories = {"all", "food", "retail", "healthcare", "professional",
                              "automotive", "hospitality", "fitness", "beauty"}
        selected_categories = [c.strip() for c in category_filter.split(",") if c.strip()]
        if not selected_categories or any(c not in allowed_categories for c in selected_categories):
            category_filter = "all"
        else:
            category_filter = ",".join(selected_categories)
        try:
            radius_km = float(params.get("radius_km") or 0)
            radius_km = max(0, min(100, radius_km))
        except (TypeError, ValueError):
            radius_km = 0

        override = {}
        comp = vertical.competitor_input
        urls = [u.strip() for u in (params.get("competitor_urls") or "").splitlines()
                if u.strip()]
        if comp and urls:
            override[comp["config_key"]] = urls
            log(f"Using {len(urls)} competitor page(s) for suppression.")

        # Existing-CRM dedupe: names pasted/uploaded so we never return a dupe.
        exclude = load_crm_names(params.get("crm_csv", ""), is_text=True) if params.get("crm_csv") else None
        if exclude:
            log(f"Loaded {len(exclude)} company names from your CRM — will skip those.")

        stem = os.path.join(
            OUT_DIR,
            f"{params['vertical']}_{_slug(market) or 'demo'}_"
            f"{datetime.now():%Y%m%d_%H%M%S}_{job_id[:8]}")

        log(f"Vertical: {vertical.label}" + ("  (DEMO MODE)" if demo else ""))
        run_metadata = {}
        leads = run_pipeline(
            vertical, market,
            sources=sources, limit=limit, enrich=enrich, enrich_cap=enrich_cap,
            category_filter=category_filter, radius_km=radius_km or None,
            out_stem=stem, config_override=override or None,
            exclude_names=exclude, demo=demo, log=log, run_metadata=run_metadata,
        )

        files = run_metadata.get("outputs")  # (csv_path, xlsx_path) or None
        job["files"] = {
            "csv": os.path.basename(files[0]) if files else None,
            "xlsx": os.path.basename(files[1]) if files else None,
        }
        job["columns"] = vertical.columns
        # Keep a preview (top 1000) for in-page search/browse; full data is in the files.
        job["leads"] = leads[:1000]
        job["stats"] = _tier_counts(leads, total=len(leads))
        if not leads:
            job["notice"] = explain_empty_result(market, sources, vertical.label)
            log(job["notice"])
        else:
            log(f"Done. {len(leads)} leads — download below.")
            # Remember this run for the session "recent runs" panel.
            RECENT.insert(0, {
                "jid": job_id, "label": vertical.label,
                "market": market or "(sample)", "stats": job["stats"],
                "files": job["files"], "when": datetime.now().strftime("%H:%M"),
            })
            del RECENT[8:]
    except Exception as e:
        raw = f"{type(e).__name__}: {e}"
        job["error"] = friendly_error(str(e)) or raw
        log(f"ERROR: {job['error']}")
    finally:
        job["done"] = True


def _tier_counts(leads, total):
    n = {"A": 0, "B": 0, "C": 0}
    for r in leads:
        n[r.get("tier", "C")] = n.get(r.get("tier", "C"), 0) + 1
    return {"total": total, **n}


def _slug(s):
    import re
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ───────────────────────────── routes ────────────────────────────────────────
@app.route("/")
def index():
    verts = [{"key": k, "label": v.label, "description": v.description,
              "competitor_input": v.competitor_input,
              "default_sources": ["osm"] if not v.overture_categories and v.osm_tags else ["overture"]}
             for k, v in sorted(all_verticals().items()) if k == "web_design"]
    markets = sorted(MARKETS.keys())
    return render_template_string(INDEX_HTML, verticals=verts, markets=markets)


@app.route("/run", methods=["POST"])
def start_run():
    if not request.is_json:
        return jsonify({"error": "Request body must be JSON."}), 415
    params = request.get_json(silent=True)
    if not isinstance(params, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    if not params.get("vertical"):
        return jsonify({"error": "vertical is required"}), 400
    if params["vertical"] not in all_verticals():
        return jsonify({"error": "Unsupported vertical."}), 400
    if not params.get("demo") and not params.get("market"):
        return jsonify({"error": "market is required (or use Demo mode)"}), 400
    if len(str(params.get("market") or "")) > 200:
        return jsonify({"error": "market is too long (maximum 200 characters)."}), 400
    if params.get("crm_csv") and len(str(params["crm_csv"])) > 200_000:
        return jsonify({"error": "CRM CSV is too large (maximum 200 KB)."}), 413
    requested_sources = params.get("sources", ["osm"])
    if not isinstance(requested_sources, list):
        return jsonify({"error": "sources must be a list."}), 400
    if any(source not in {"osm", "overture"} for source in requested_sources):
        return jsonify({"error": "sources contains an unsupported data source."}), 400
    allowed_categories = {
        "all", "food", "retail", "healthcare", "professional",
        "automotive", "hospitality", "fitness", "beauty",
    }
    categories = params.get("category_filter", "all")
    if not isinstance(categories, str) or any(
        item.strip().lower() not in allowed_categories
        for item in categories.split(",") if item.strip()
    ):
        return jsonify({"error": "category_filter contains an unsupported category."}), 400
    for field, minimum, maximum in (("limit", 1, 500), ("enrich_cap", 0, 100)):
        if field in params and params[field] not in (None, ""):
            try:
                value = int(params[field])
            except (TypeError, ValueError):
                return jsonify({"error": f"{field} must be a whole number."}), 400
            if not minimum <= value <= maximum:
                return jsonify({"error": f"{field} must be between {minimum} and {maximum}."}), 400
    if "radius_km" in params and params["radius_km"] not in (None, ""):
        try:
            radius = float(params["radius_km"])
        except (TypeError, ValueError):
            return jsonify({"error": "radius_km must be a number."}), 400
        if not 0 <= radius <= 100:
            return jsonify({"error": "radius_km must be between 0 and 100."}), 400
    jid = uuid.uuid4().hex
    JOBS[jid] = {"log": [], "done": False, "error": None, "notice": None,
                 "stats": None, "leads": None, "files": None, "columns": None}
    threading.Thread(target=run_job, args=(jid, params), daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({"error": "Request is too large."}), 413


@app.route("/check")
def check():
    """Connectivity self-check — probe every data source, plain-English status."""
    return jsonify(check_connectivity())


@app.route("/recent")
def recent():
    """The last few completed runs this session (for the recent-runs panel)."""
    return jsonify(RECENT)


@app.route("/progress/<jid>")
def progress(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify({
        "log": job["log"], "done": job["done"], "error": job["error"],
        "notice": job.get("notice"),
        "stats": job["stats"], "files": job["files"],
        "leads": job["leads"] if job["done"] and not job["error"] else None,
        "columns": job["columns"],
    })


@app.route("/download/<kind>/<jid>")
def download(kind, jid):
    job = JOBS.get(jid)
    if not job or not job.get("files"):
        return "Not ready", 404
    if kind not in {"csv", "xlsx"}:
        return "No such file", 404
    fname = job["files"].get(kind)
    if not fname:
        return "No such file", 404
    path = os.path.abspath(os.path.join(OUT_DIR, fname))
    if os.path.dirname(path) != os.path.abspath(OUT_DIR):
        return "No such file", 404
    if not os.path.exists(path):
        return "File missing", 404
    return send_file(path, as_attachment=True, download_name=fname)


# ───────────────────────────── server bootstrap ──────────────────────────────
def free_port(default=5000):
    for p in (default, 5001, 5050, 8000, 8080, 8765):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return default


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Arvett Prospect · Arvett</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--blue:#1F4E78;--green:#28a745;
  --bg:#fafbfc;--fg:#1d2733;--panel:#fff;--border:#dde2e8;--border2:#e3e8ee;
  --muted:#667;--muted2:#8a93a0;--field-border:#c4ccd6;--th:#eef2f7;--accent:#1F4E78;
  --tierA:#e8f8ec;--tierB:#fff8e1;--tierC:#fdecee;--ghosthover:#eef2f7;--code-bg:#0d1b2a;--code-fg:#c8e1ff}
html[data-theme=dark]{--blue:#5a9bd8;--green:#3fbf5f;--accent:#7fb6e6;
  --bg:#11161d;--fg:#dfe6ee;--panel:#1a212b;--border:#2c3742;--border2:#2c3742;
  --muted:#9aa7b4;--muted2:#7d8a97;--field-border:#3a4754;--th:#222c38;
  --tierA:#173324;--tierB:#332c14;--tierC:#3a1f23;--ghosthover:#222c38;--code-bg:#070d14;--code-fg:#a9cdf2}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;
  max-width:1080px;margin:0 auto;padding:24px 18px;color:var(--fg);background:var(--bg)}
h1{margin:0 0 2px;letter-spacing:-.02em;color:var(--fg)}.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
body:before{content:"LOCAL APPLICATION";display:inline-block;color:var(--accent);font-size:10px;
  letter-spacing:.12em;font-weight:700;margin-bottom:7px}
*:focus-visible{outline:3px solid rgba(90,155,216,.55);outline-offset:2px}
fieldset{border:1px solid var(--border);border-radius:10px;margin:0 0 16px;padding:14px 16px;background:var(--panel)}
legend{padding:0 8px;font-weight:650;color:var(--accent)}
.opt{font-weight:400;color:var(--muted2);font-size:12px}
.firsttime{background:#eaf4ff;border:1px solid #bcdcff;border-radius:9px;padding:11px 14px;margin-bottom:16px;font-size:13.5px;line-height:1.5;color:#1d2733}
html[data-theme=dark] .firsttime{background:#16314a;border-color:#27496b;color:#cfe2f5}
details.adv{border:1px solid var(--border2);border-radius:12px;background:var(--panel);padding:0 16px;margin:0 0 16px;box-shadow:0 2px 8px rgba(20,40,70,.04)}
details.adv>summary{cursor:pointer;list-style:none;font-weight:700;color:var(--fg);font-size:13.5px;padding:14px 0;display:flex;align-items:center;gap:9px}
details.adv>summary::-webkit-details-marker{display:none}
details.adv>summary:before{content:"＋";width:22px;height:22px;display:grid;place-items:center;border:1px solid var(--field-border);border-radius:6px;color:var(--accent);font-size:16px}
details.adv[open]>summary:before{content:"−"}
.legend{display:none;margin-top:10px;font-size:12.5px;color:var(--muted);background:var(--panel);border:1px solid var(--border2);border-radius:9px;padding:10px 13px;line-height:1.6}
label.fld{display:block;font-size:13px;font-weight:600;margin:10px 0 4px;color:var(--muted)}
input[type=text],input[type=number],select,textarea{
  width:100%;padding:10px 11px;font-size:14px;border:1px solid var(--field-border);border-radius:8px;font-family:inherit;background:var(--panel);color:var(--fg);color-scheme:light}
html[data-theme=dark] input,html[data-theme=dark] select,html[data-theme=dark] textarea{color-scheme:dark}
input::placeholder,textarea::placeholder{color:var(--muted2);opacity:1}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}
textarea{resize:vertical;min-height:74px}
.row{display:flex;gap:14px;flex-wrap:wrap}.row>div{flex:1;min-width:220px}
.hint{color:var(--muted2);font-size:12px;margin-top:4px}
.checks label{display:inline-flex;align-items:center;gap:5px;margin-right:16px;font-size:14px;cursor:pointer}
button{background:var(--blue);color:#fff;border:none;padding:11px 22px;border-radius:8px;
  font-size:15px;font-weight:600;cursor:pointer}button:hover{filter:brightness(.9)}
button:disabled{background:#a9b2bd;cursor:not-allowed;filter:none}
button.ghost{background:var(--panel);color:var(--accent);border:1.5px solid var(--field-border)}
button.ghost:hover{background:var(--ghosthover);filter:none}
.btnrow{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:4px}
input[type=file]{font-size:13px}
#checkout{margin-top:12px}
.probe{display:flex;align-items:center;gap:8px;font-size:13px;padding:4px 0}
.probe .dot{width:10px;height:10px;border-radius:50%;flex:none}
.dot.ok{background:#28a745}.dot.bad{background:#d6453f}
.banner{border-radius:9px;padding:11px 14px;margin-top:10px;font-size:13.5px}
.banner.good{background:#e8f8ec;border:1px solid #b6e3c2;color:#1d2733}
.banner.warn{background:#fff4e0;border:1px solid #f0d49a;color:#1d2733}
.banner.bad{background:#fdecee;border:1px solid #f0bcc0;color:#1d2733}
.vdesc{font-size:12.5px;color:var(--muted);margin-top:6px;line-height:1.45}
.featurelist{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.featurelist span{border:1px solid var(--border);border-radius:999px;padding:4px 8px;
  background:var(--th);color:var(--muted);font-size:11.5px;font-weight:600}
#status{display:none;margin-top:8px;background:var(--code-bg);color:var(--code-fg);border-radius:9px;
  padding:12px 14px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
  max-height:260px;overflow:auto;white-space:pre-wrap;line-height:1.5}
#summary{display:none;margin-top:14px;gap:8px}
.stat{flex:1;text-align:center;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px;box-shadow:0 2px 8px rgba(20,40,70,.05)}
.stat b{display:block;font-size:22px}.stat small{display:block;color:var(--muted);font-size:11px;margin-top:3px}.stat.A b{color:var(--green)}.stat.B b{color:#c79100}.stat.C b{color:#b04a52}
@media(max-width:700px){ #summary{display:grid!important;grid-template-columns:repeat(2,1fr)}}
#dls{display:none;margin-top:12px;gap:10px}
a.dl{display:inline-block;padding:10px 16px;background:var(--green);color:#fff;text-decoration:none;border-radius:8px;font-weight:600}
a.dl.alt{background:#34507a}
table{width:100%;border-collapse:collapse;margin-top:14px;font-size:12.5px;display:none;background:var(--panel)}
th,td{border:1px solid var(--border2);padding:5px 7px;text-align:left;vertical-align:top}
th{background:var(--th);position:sticky;top:0;cursor:pointer;user-select:none;white-space:nowrap}
th .arrow{color:var(--muted2);font-size:10px;margin-left:3px}
.tierA{background:var(--tierA)}.tierB{background:var(--tierB)}.tierC{background:var(--tierC)}
tr.tagged-contacted td{box-shadow:inset 4px 0 0 0 var(--green)}
tr.tagged-not_interested td{box-shadow:inset 4px 0 0 0 #b04a52;opacity:.6}
.tablewrap{max-height:440px;overflow:auto;border-radius:9px;border:1px solid var(--border2);margin-top:14px;display:none}
.recent{display:flex;flex-wrap:wrap;gap:8px}
.rcard{border:1px solid var(--border);border-radius:8px;padding:8px 11px;background:var(--panel);font-size:12.5px;min-width:210px}
.rcard b{color:var(--accent)}.rcard a{color:var(--green);text-decoration:none;font-weight:600}
.rcard .meta{color:var(--muted2);font-size:11.5px;margin:2px 0 5px}
#resultsearch{display:none;margin-top:14px}
#themetoggle{position:fixed;top:14px;right:14px;background:var(--panel);color:var(--accent);
  border:1.5px solid var(--field-border);padding:7px 12px;border-radius:8px;font-size:13px;z-index:50}
#themetoggle:hover{background:var(--ghosthover);filter:none}
.resulttools{display:none;flex-wrap:wrap;gap:8px;align-items:center;margin-top:14px}
.resulttools button{padding:7px 13px;font-size:13px}
.resultfilters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
.resultfilters input,.resultfilters select{width:auto;min-width:150px;padding:8px 10px}
.cellbtn{background:none;border:1px solid var(--field-border);color:var(--accent);
  padding:1px 5px;font-size:11px;border-radius:5px;cursor:pointer;font-weight:600;margin-left:4px;vertical-align:middle}
.cellbtn:hover{background:var(--ghosthover);filter:none}
td a{color:var(--accent);text-decoration:none}td a:hover{text-decoration:underline}
.statusbadge{display:inline-block;padding:2px 7px;border-radius:999px;font-size:11px;font-weight:650;
  border:1px solid var(--border);background:var(--th)}
.statusbadge.functional-website{background:var(--tierA)}.statusbadge.outdated-website,
.statusbadge.website-broken{background:var(--tierB)}.statusbadge.no-website-found,
.statusbadge.social-only{background:var(--tierC)}
.tagbtns{display:inline-flex;gap:3px}
.tagbtns button{background:none;border:1px solid var(--field-border);color:var(--muted);
  padding:1px 5px;font-size:11px;border-radius:5px;cursor:pointer}
.tagbtns button.on-c{background:var(--green);color:#fff;border-color:var(--green)}
.tagbtns button.on-n{background:#b04a52;color:#fff;border-color:#b04a52}
#colmenu{position:relative;display:inline-block}
#colpanel{display:none;position:absolute;top:100%;left:0;z-index:40;background:var(--panel);
  border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-top:4px;
  box-shadow:0 4px 14px rgba(0,0,0,.15);min-width:200px;max-height:300px;overflow:auto}
#colpanel label{display:block;font-size:13px;padding:3px 0;cursor:pointer;white-space:nowrap;font-weight:400;color:var(--fg)}
#colpanel input{margin-right:6px}
/* Settings panel */
details.settings>summary{cursor:pointer;font-weight:600;color:var(--accent);font-size:13.5px;padding:6px 0}
.setgrid{display:flex;flex-wrap:wrap;gap:14px;margin-top:8px}.setgrid>div{flex:1;min-width:200px}
.setchecks label{display:block;font-size:13px;padding:2px 0;cursor:pointer;font-weight:400;color:var(--fg)}
.setchecks input{margin-right:6px;width:auto}
.setactions{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.setactions .saved{color:var(--green);font-size:12.5px;font-weight:600}
/* Run queue */
.queuegrid{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.queuegrid>div{flex:1;min-width:160px}
.qlist{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.qitem{display:flex;align-items:center;gap:8px;border:1px solid var(--border);border-radius:8px;
  padding:7px 11px;background:var(--panel);font-size:13px}
.qitem .qmeta{flex:1}.qitem .qmeta b{color:var(--accent)}
.qitem .qstate{font-size:11.5px;color:var(--muted2);font-weight:600}
.qitem.active{border-color:var(--blue);box-shadow:inset 3px 0 0 0 var(--blue)}
.qitem.done{opacity:.65}.qitem.done .qstate{color:var(--green)}
.qitem.failed .qstate{color:#b04a52}
.qitem button.qrm{background:none;border:1px solid var(--field-border);color:var(--muted);
  padding:1px 7px;font-size:12px;border-radius:5px;cursor:pointer;font-weight:600}
/* Per-source tally + timer */
#runmeter{display:none;margin-top:10px;flex-wrap:wrap;gap:8px;align-items:center}
#runmeter .chip{background:var(--panel);border:1px solid var(--border);border-radius:20px;
  padding:4px 11px;font-size:12.5px;color:var(--fg)}
#runmeter .chip b{color:var(--accent)}
#progressbox{display:none;margin-top:12px;padding:12px 14px;border:1px solid var(--border);border-radius:10px;background:var(--panel)}
#progressbox .phase{font-size:13px;font-weight:700;color:var(--fg)}
#progressbar{height:7px;background:var(--th);border-radius:99px;overflow:hidden;margin-top:9px}
#progressbar span{display:block;width:35%;height:100%;background:var(--accent);border-radius:99px;animation:progress-sweep 1.4s ease-in-out infinite}
@keyframes progress-sweep{0%{transform:translateX(-130%)}100%{transform:translateX(310%)}}
@media (prefers-reduced-motion:reduce){ #progressbar span{animation:none;width:100%}}
#category_picker{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;padding:4px 0}
#category_picker label{display:flex;align-items:center;gap:8px;padding:9px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel);color:var(--fg);font-size:12px;cursor:pointer}
#category_picker label:has(input:checked){border-color:var(--accent);background:var(--th);color:var(--accent);font-weight:700}
#category_picker input{accent-color:var(--accent)}
@media(max-width:620px){body{padding:18px 12px}.row>div{min-width:100%}#category_picker{grid-template-columns:1fr}#themetoggle{position:absolute;top:10px;right:10px}.btnrow button{flex:1;min-width:0}}
.drawerbackdrop{display:none;position:fixed;inset:0;background:rgba(3,8,15,.55);z-index:80}
.drawer{position:absolute;right:0;top:0;height:100%;width:min(460px,100%);overflow:auto;background:var(--panel);border-left:1px solid var(--border);padding:24px;box-shadow:-12px 0 35px rgba(0,0,0,.2)}
.drawer h2{margin:0 36px 4px 0;font-size:21px}.drawer h3{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:22px 0 7px}.drawer p{margin:5px 0;color:var(--fg);font-size:13px;line-height:1.55}.drawerclose{position:absolute;right:18px;top:18px;padding:5px 9px}.drawergrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.drawergrid div{padding:9px;border:1px solid var(--border);border-radius:8px}.drawergrid b{display:block;font-size:11px;color:var(--muted);margin-bottom:3px}.drawergrid span{font-size:13px}.draweropen{cursor:pointer}
#runtimer{font-size:12.5px;color:var(--muted2);font-weight:600}
/* Arvett MVP: keep the interface focused; unused legacy controls remain hidden for compatibility. */
#settingswrap,#recentwrap,#queuewrap,#competitor_box,#crm_box,#check,#enqueue{display:none!important}
#src_socrata,#src_npi,#src_foursquare{display:none!important}
label:has(#src_socrata),label:has(#src_npi),label:has(#src_foursquare){display:none!important}
/* First-run tour */
#tour{display:none;position:fixed;inset:0;background:rgba(10,16,24,.55);z-index:100}
#tourcard{position:absolute;max-width:300px;background:var(--panel);color:var(--fg);
  border:1px solid var(--border);border-radius:11px;padding:15px 17px;box-shadow:0 8px 30px rgba(0,0,0,.35);font-size:13.5px;line-height:1.5}
#tourcard h3{margin:0 0 6px;color:var(--accent);font-size:15px}
#tourcard .tnav{display:flex;justify-content:space-between;align-items:center;margin-top:12px}
#tourcard .tnav .tstep{color:var(--muted2);font-size:12px}
#tourcard button{padding:6px 14px;font-size:13px}
#tourcard button.tskip{background:none;color:var(--muted);border:none;padding:6px 4px;font-weight:600}
.tourhi{position:relative;z-index:101;box-shadow:0 0 0 3px var(--blue),0 0 0 9px rgba(31,78,120,.3);border-radius:8px}
/* Product system: quiet graphite, crisp type, one electric-blue action. */
:root{--bg:#f5f7fa;--fg:#17212d;--panel:#fff;--border:#dce3eb;--border2:#e5eaf0;--muted:#586779;--muted2:#748295;--field-border:#c8d1dc;--th:#f0f3f7;--accent:#285d91;--blue:#285d91;--green:#26734d;--ghosthover:#edf2f8;--code-bg:#111923;--code-fg:#d4e3f2;color-scheme:light}
html[data-theme=dark]{--bg:#0d1218;--fg:#e7edf4;--panel:#151c24;--border:#2a3541;--border2:#26313c;--muted:#a4b0bd;--muted2:#8391a0;--field-border:#394653;--th:#1d2731;--accent:#74b4f1;--blue:#347fc0;--green:#72c59a;--ghosthover:#202b36;--code-bg:#090e13;--code-fg:#c3d9ed;color-scheme:dark}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{max-width:1240px;padding:0 28px 64px;font-family:Inter,Geist,"Segoe UI",sans-serif;line-height:1.5;background:var(--bg)}body:before{display:none}body>button#themetoggle{display:none}
.appbar{height:62px;display:flex;align-items:center;border-bottom:1px solid var(--border);gap:22px;margin-bottom:34px}.brand{display:flex;align-items:center;gap:8px;color:var(--fg);text-decoration:none;font-size:17px;font-weight:750;letter-spacing:-.045em}.brandmark{display:grid;place-items:center;width:25px;height:25px;background:#285d91;color:#fff;border-radius:6px;font-size:14px;font-weight:800}.brand i{height:18px;border-left:1px solid var(--border);margin:0 3px}.brand b{font-size:13px;letter-spacing:0;color:var(--muted);font-weight:600}.context{font-size:12px;color:var(--muted2)}.context span{padding:0 6px;color:var(--border)}#themetoggle2{margin-left:auto;position:static;padding:7px 10px;border-radius:7px;font-size:12px;font-weight:600;background:var(--panel);color:var(--fg);border:1px solid var(--border)}
.workspace{max-width:1080px;margin:0 auto}.pageintro{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:21px}.eyebrow{font-size:10px;font-weight:750;letter-spacing:.12em;color:var(--muted2);margin:0 0 7px}.eyebrow span{color:var(--accent)}h1{font-size:27px;line-height:1.2;letter-spacing:-.04em;margin:0 0 7px;font-weight:670}.sub{font-size:13px;max-width:710px;line-height:1.55;margin:0;color:var(--muted)}.workflow{display:flex;align-items:center;gap:8px;white-space:nowrap;font-size:11px;color:var(--muted2);padding-bottom:5px}.workflow b{display:inline-grid;place-items:center;width:20px;height:20px;border:1px solid var(--border);border-radius:50%;margin-right:5px;font-size:10px}.workflow .current{color:var(--accent);font-weight:650}.workflow .current b{background:var(--accent);color:#fff;border-color:var(--accent)}.connector{width:30px;border-top:1px solid var(--border)}
#form{display:grid;grid-template-columns:minmax(0,.78fr) minmax(0,1.22fr);gap:12px}fieldset{min-width:0;border:1px solid var(--border);border-radius:9px;margin:0;padding:13px 15px 15px;box-shadow:0 2px 7px rgba(9,20,35,.025)}#form>fieldset:first-of-type{grid-column:1}#form>fieldset:nth-of-type(2){grid-column:2;grid-row:1 / span 2}#form>details.adv,#form>.btnrow,#form>#checkout,#form>#runmeter,#form>#progressbox{grid-column:1 / -1}legend{font-size:12px;letter-spacing:.01em;padding:0 7px;color:var(--fg)}.stepnum{font-size:10px;letter-spacing:.08em;color:var(--accent);margin-right:5px}.fld{font-size:11.5px;margin:6px 0 4px;color:var(--muted);font-weight:650}input[type=text],input[type=number],select,textarea{border:1px solid var(--field-border);border-radius:6px;padding:9px 10px;font-size:13px;background:var(--panel)}input:focus,select:focus,textarea:focus{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 20%,transparent)}.hint{font-size:11px;line-height:1.5;color:var(--muted2)}.vdesc{font-size:11.5px}.featurelist{gap:5px}.featurelist span{font-size:10.5px;padding:3px 7px;border-radius:5px}.intent-note{margin-top:7px}.row{gap:11px}.row>div{min-width:150px}#form>fieldset:nth-of-type(2) .row{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}#form>fieldset:nth-of-type(2) .row>div{min-width:0}#category_picker{grid-template-columns:1fr;gap:3px;max-height:220px;overflow:auto;border:1px solid var(--border2);border-radius:6px;padding:4px}#category_picker label{border:0;border-radius:4px;padding:5px 6px;font-size:11.5px;background:transparent;min-height:30px}#category_picker label:has(input:checked){background:color-mix(in srgb,var(--accent) 10%,var(--panel));color:var(--fg)}#category_picker input{width:14px;height:14px;accent-color:var(--accent)}.selectedcount{font-size:10px;color:var(--accent);font-weight:600;margin-left:5px}.textbutton{background:none;border:0;padding:0;color:var(--accent);font-size:10px;font-weight:600;margin-left:8px;text-decoration:underline;text-underline-offset:2px}.checks label{font-size:11.5px;display:flex;align-items:center;gap:6px;margin:5px 0}.checks input{accent-color:var(--accent)}
details.adv{border:1px solid var(--border);border-radius:8px;padding:0 13px;margin:0;background:var(--panel);box-shadow:none}details.adv>summary{font-size:12px;padding:10px 0}details.adv>summary:before{width:19px;height:19px;font-size:13px;border-radius:5px}.opt{margin-left:auto;font-size:10.5px;font-weight:450}.btnrow{margin:0;justify-content:space-between;padding:2px 1px}.btnrow button{font-size:12px;padding:9px 14px;border-radius:6px}.btnrow #go{font-size:13px;padding:11px 20px;min-width:190px;box-shadow:0 2px 4px rgba(20,67,110,.16)}.btnrow #go span{margin-left:12px;font-size:16px}.btnrow .ghost{border-width:1px;font-size:11px;padding:8px 11px}.btnrow #check,.btnrow #enqueue{display:none!important}.field-error{color:#d05255;font-size:11px;margin-top:4px}.field-invalid{border-color:#d05255!important}.banner{font-size:12px;padding:10px 12px;border-radius:7px}.banner.good{background:color-mix(in srgb,#26734d 12%,var(--panel));color:var(--fg)}.banner.warn{background:color-mix(in srgb,#bd8521 13%,var(--panel));color:var(--fg)}.banner.bad{background:color-mix(in srgb,#bf4545 12%,var(--panel));color:var(--fg)}
#progressbox{padding:13px 15px;border-radius:8px}.progresshead{display:flex;align-items:center;justify-content:space-between}.phase{margin-top:2px}.progressstate{font-size:11px;color:var(--accent);font-weight:650}.progressstate i{display:inline-block;width:7px;height:7px;border-radius:50%;background:currentColor;margin-right:5px}.progressstate.failed{color:#d05255}.stat{border-radius:7px;box-shadow:none;padding:11px}.stat b{font-size:20px}.stat small{font-size:10px;color:var(--muted)}#summary{gap:8px;margin-top:20px}.stat.A b,.stat.B b,.stat.C b{color:var(--accent)}.tablewrap{border-radius:7px;max-height:62vh}.tablewrap table{font-size:11.5px}.tablewrap th{top:0;z-index:2}.tablewrap th,.tablewrap td{padding:8px 9px;border-width:0 0 1px 0}.tablewrap tr:hover td{background:color-mix(in srgb,var(--accent) 5%,var(--panel))}.resultfilters{gap:6px}.resultfilters input,.resultfilters select{font-size:11px;min-width:130px;padding:7px 8px;border-radius:6px}.resulttools button{font-size:11px;padding:7px 9px;border-radius:6px}.statusbadge{font-size:10px;border-radius:4px;padding:2px 5px}.drawer{padding:26px;width:min(440px,100%)}.drawer h2{font-size:19px}.drawer h3{letter-spacing:.06em}.drawergrid div{border-radius:6px}
@media(max-width:820px){body{padding:0 18px 48px}.pageintro{align-items:flex-start;flex-direction:column;gap:12px}.workflow{padding-bottom:0}#form{grid-template-columns:1fr 1fr}#form>fieldset:first-of-type{grid-column:1}#form>fieldset:nth-of-type(2){grid-column:2;grid-row:1 / span 2}#form>fieldset:nth-of-type(2) .row{grid-template-columns:1fr}.featurelist{flex-wrap:wrap}}
@media(max-width:600px){body{padding:0 13px 36px}.appbar{height:54px;margin-bottom:22px;gap:10px}.context{display:none}.brand{font-size:15px}h1{font-size:23px}.sub{font-size:12px}.workflow{font-size:10px}#form{grid-template-columns:1fr;gap:9px}#form>fieldset:first-of-type,#form>fieldset:nth-of-type(2){grid-column:1;grid-row:auto}#form>fieldset:nth-of-type(2) .row{grid-template-columns:1fr 1fr}#form>fieldset:nth-of-type(2) .row>div:nth-child(2){grid-column:1/-1;grid-row:2}#form>fieldset:nth-of-type(2) .row>div:nth-child(3){grid-column:1;grid-row:3}#form>fieldset:nth-of-type(2) .row>div:nth-child(4){grid-column:2;grid-row:3}.btnrow{gap:8px}.btnrow #go{flex:1;min-width:100%}.btnrow .ghost{display:none!important}#summary{grid-template-columns:repeat(2,1fr)}.resultfilters input,.resultfilters select{width:100%;min-width:0}.tablewrap{overflow:auto}.tablewrap table{min-width:850px}.drawergrid{grid-template-columns:1fr 1fr}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*:before,*:after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
#activefilters{display:flex;gap:5px;flex-wrap:wrap;margin-top:7px}#activefilters:empty{display:none}.filterchip{background:var(--th);border:1px solid var(--border);color:var(--muted);padding:3px 7px;border-radius:5px;font-size:10px;cursor:pointer}.filterchip:hover{color:var(--fg);border-color:var(--accent)}.notfound{color:var(--muted2);font-size:10.5px}#resultsheading{display:none;align-items:end;margin:26px 0 11px;border-bottom:1px solid var(--border);padding-bottom:10px}#resultsheading h2{font-size:18px;letter-spacing:-.025em;margin:0 0 2px}#resultsheading .eyebrow{margin:0 0 3px;color:var(--accent)}#resultsheading .hint{margin:0}
</style></head><body>
<button id="themetoggle" type="button" title="Toggle dark mode">🌙 Dark</button>
<header class="appbar"><a class="brand" href="/" aria-label="Arvett Prospect home"><span class="brandmark">A</span><span>arvett</span><i></i><b>Prospect</b></a><span class="context">Local business intelligence <span>/</span> New search</span><button id="themetoggle2" type="button" title="Toggle color theme" aria-label="Toggle color theme">Light theme</button></header>
<main class="workspace"><div class="pageintro"><div><p class="eyebrow">PROSPECTING WORKSPACE <span>·</span> 01 / SEARCH</p><h1>Find your next web development client.</h1><p class="sub">Discover local businesses with potential website opportunities, verify their digital presence, and build an actionable prospect list.</p></div><div class="workflow" aria-label="Workflow"><span class="current"><b>1</b> Configure search</span><span class="connector"></span><span><b>2</b> Review prospects</span></div></div>

<details class="adv settings" id="settingswrap">
  <summary>⚙ Settings &amp; defaults</summary>
  <div class="setgrid">
    <div>
      <label class="fld">Default data sources</label>
      <div class="setchecks" id="set_sources">
        <label><input type="checkbox" data-src="overture"> Nationwide business directory</label>
        <label><input type="checkbox" data-src="osm"> Live map data</label>
      </div>
      <div class="hint">Applied to the run form when you open the page. Picking a vertical may still override these.</div>
    </div>
    <div>
      <label class="fld">Default deep-check cap</label>
      <input type="number" id="set_enrich_cap" min="0" placeholder="150">
      <div class="hint">Pre-fills “Website checks (maximum 100)” under Advanced options.</div>
      <label class="fld">Crawler contact string (User-Agent)</label>
      <input type="text" id="set_ua" placeholder="LeadEngine/1.0 (you@example.com)">
      <div class="hint">Informational — a polite contact so site owners know who is crawling. Stored locally only.</div>
    </div>
  </div>
  <div class="setactions">
    <button type="button" id="set_save">Save defaults</button>
    <button type="button" class="ghost" id="set_clear">Reset</button>
    <span class="saved" id="set_saved" style="display:none">Saved ✓</span>
  </div>
</details>

<div id="recentwrap" style="display:none">
  <fieldset><legend>Recent runs <span class="opt">(this session)</span></legend>
    <div class="recent" id="recentlist"></div>
  </fieldset>
</div>

<form id="form">
  <fieldset>
    <legend><span class="stepnum">01</span> Opportunity type</legend>
    <label class="fld">Type of business to find</label>
    <select id="vertical"></select>
    <div class="featurelist" aria-label="Search targets"><span>No website</span><span>Social-only presence</span><span>Weak or outdated website</span><span>Combined opportunity</span></div>
    <p class="hint intent-note">These are search targets. Website status is verified during research.</p>
    <div class="vdesc" id="vdesc"></div>
  </fieldset>

  <fieldset>
    <legend><span class="stepnum">02</span> Location, coverage &amp; category</legend>
    <div class="row">
      <div>
        <label class="fld" for="market">Location</label>
        <input type="text" id="market" list="markets" placeholder="Mehsana, Gujarat, India" autocomplete="off" aria-describedby="market_hint market_error" required>
        <datalist id="markets">{% for m in markets %}<option value="{{m}}">{% endfor %}</datalist>
        <div class="hint" id="market_hint">Use the most specific place available. We resolve it before collecting businesses.</div><div class="field-error" id="market_error" role="alert"></div>
      </div>
      <div>
        <label class="fld">Business category <span class="selectedcount" id="category_count">1 selected</span><button type="button" class="textbutton" id="clear_categories">Clear all</button></label>
        <div id="category_picker" role="group" aria-label="Business categories">
          <label><input type="checkbox" name="category" value="all" checked> All local businesses</label>
          <label><input type="checkbox" name="category" value="food"> Restaurants, cafes and food</label>
          <label><input type="checkbox" name="category" value="retail"> Retail and shops</label>
          <label><input type="checkbox" name="category" value="healthcare"> Clinics and healthcare</label>
          <label><input type="checkbox" name="category" value="professional"> IT, offices and professional services</label>
          <label><input type="checkbox" name="category" value="automotive"> Automotive services</label>
          <label><input type="checkbox" name="category" value="hospitality"> Hotels and accommodation</label>
          <label><input type="checkbox" name="category" value="fitness"> Fitness and sports</label>
          <label><input type="checkbox" name="category" value="beauty"> Beauty and personal care</label>
        </div>
        <div class="hint">Choose one or more groups. “All local businesses” clears other selections.</div>
      </div>
      <div>
        <label class="fld">Search radius</label>
        <select id="radius_km">
          <option value="0">Use selected area boundary</option>
          <option value="5">Within 5 km</option>
          <option value="10">Within 10 km</option>
          <option value="25">Within 25 km</option>
          <option value="50">Within 50 km</option>
        </select>
        <div class="hint">Radius searches exclude businesses without coordinates.</div>
      </div>
      <div>
        <label class="fld">Where the data comes from</label>
        <div class="checks" style="padding-top:8px">
          <label><input type="checkbox" id="src_overture"> Overture Maps (optional broader coverage)</label>
          <label><input type="checkbox" id="src_osm" checked> OpenStreetMap (recommended free source)</label>
        </div>
        <div class="hint">Start with OpenStreetMap. Overture is optional and can be slower. Source coverage varies by area.</div>
      </div>
    </div>
  </fieldset>

  <fieldset id="competitor_box" style="display:none">
    <legend>3 · Skip anyone already using a competitor <span class="opt">(optional)</span></legend>
    <label class="fld" id="competitor_label"></label>
    <textarea id="competitor_urls" placeholder="https://a-competitor.com/testimonials&#10;https://another-competitor.com/reviews"></textarea>
    <div class="hint" id="competitor_help"></div>
    <div class="hint">One web address per line.</div>
  </fieldset>

  <fieldset id="crm_box">
    <legend>4 · Skip people already in your CRM <span class="opt">(optional)</span></legend>
    <label class="fld">Upload your current contact list (.csv)</label>
    <input type="file" id="crm_file" accept=".csv,text/csv">
    <div class="hint" id="crm_status">We match on company name and remove anyone you already have — so you never get a duplicate.</div>
  </fieldset>

  <details class="adv">
    <summary>Advanced options</summary>
    <div class="row" style="margin-top:10px">
      <div>
        <label class="fld"><input type="checkbox" id="enrich" checked style="width:auto"> Visit each website for deeper detail</label>
        <div class="hint">Audits each site (HTTPS, mobile, speed, DIY builder). Richer results, a bit slower. Recommended on.</div>
      </div>
      <div>
        <label class="fld">Website checks (maximum 100)</label>
        <input type="number" id="enrich_cap" value="20" min="0" max="100">
      </div>
      <div>
        <label class="fld">Maximum businesses to collect (1–500)</label>
        <input type="number" id="limit" value="100" min="1" max="500">
      </div>
    </div>
  </details>

  <div class="btnrow">
    <button type="submit" id="go">🔍 Find leads</button>
    <button type="button" id="check" class="ghost">📡 Check my connection</button>
    <button type="button" id="enqueue" class="ghost">➕ Add to queue</button>
  </div>
  <div id="checkout"></div>
  <div id="runmeter">
    <span id="runtimer">⏱ 0:00</span>
    <span id="runchips"></span>
  </div>
  <div id="progressbox" role="status" aria-live="polite">
    <div class="phase" id="runphase">Preparing your search…</div>
    <div id="progressbar" aria-hidden="true"><span></span></div>
    <div class="hint">Progress updates reflect completed pipeline stages; website checks may take longer.</div>
  </div>
</form>

<details class="adv settings" id="queuewrap" style="display:none">
  <summary>📋 Run queue <span class="opt" id="queuecount"></span></summary>
  <div class="hint" style="margin:4px 0 0">Queued runs use the current sources / advanced options and run one after another.</div>
  <ul class="qlist" id="qlist"></ul>
  <div class="setactions">
    <button type="button" id="queuerun">▶ Run queue</button>
    <button type="button" class="ghost" id="queueclear">Clear queue</button>
  </div>
</details>

<div id="resultsheading"><div><p class="eyebrow">02 / REVIEW</p><h2>Prospect workspace</h2><p class="hint">Review the evidence, refine your list, then export the prospects that fit.</p></div></div>
<div id="summary" class="row">
  <div class="stat" title="Records returned after collection and deduplication"><b id="s_total">0</b><small>Businesses collected</small></div>
  <div class="stat A" title="Records with a high research-priority grade"><b id="s_A">0</b><small>High opportunity</small></div>
  <div class="stat B" title="Records with a website opportunity status"><b id="s_opportunity">0</b><small>Website opportunities</small></div>
  <div class="stat" title="Records with a public phone or email"><b id="s_contact">0</b><small>Contactable leads</small></div>
  <div class="stat C" title="Records needing manual review or with inconclusive evidence"><b id="s_review">0</b><small>Inconclusive / review</small></div>
</div>
<div id="dls" class="row"></div>
<div id="legend" class="legend"></div>
<details id="logwrap" class="adv" style="display:none"><summary>Show activity log</summary>
  <div id="status"></div>
</details>
<div id="resultsearch">
  <div class="resultfilters">
    <input type="search" id="r_filter" aria-label="Search prospects" placeholder="Search business, city, reason…">
    <select id="r_category" aria-label="Filter by category"><option value="">All categories</option></select>
    <select id="r_location" aria-label="Filter by location"><option value="">All locations</option></select>
    <select id="r_status" aria-label="Filter by website status"><option value="">All website statuses</option></select>
    <select id="r_grade" aria-label="Filter by opportunity grade"><option value="">All grades</option><option value="A">Grade A</option><option value="B">Grade B</option><option value="C">Grade C</option></select>
    <select id="r_contact" aria-label="Filter by contact availability"><option value="">Any contact</option><option value="yes">Phone or email found</option><option value="no">No phone or email</option></select>
    <button type="button" class="ghost" id="r_clear">Clear filters</button>
  </div>
  <div id="activefilters" aria-live="polite"></div>
</div>
<div class="resulttools" id="resulttools">
  <span id="colmenu">
    <button type="button" class="ghost" id="colbtn">☰ Columns</button>
    <div id="colpanel"></div>
  </span>
  <button type="button" class="ghost" id="exportbtn">⬇ Export filtered view (.csv)</button>
  <span class="hint" id="exportcount"></span>
</div>
<div class="tablewrap" id="tablewrap"><table id="preview"></table></div>
<div class="drawerbackdrop" id="drawerbackdrop" aria-hidden="true">
  <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawertitle">
    <button type="button" class="ghost drawerclose" id="drawerclose" aria-label="Close prospect details">×</button>
    <h2 id="drawertitle">Prospect details</h2>
    <p id="drawersubtitle" class="hint"></p>
    <div class="drawergrid" id="drawergrid"></div>
    <h3>Qualification reason</h3><p id="drawerwhy">Not verified</p>
    <h3>Website evidence</h3><p id="drawerevidence">Not checked</p>
    <h3>Source attribution</h3><p id="drawersource">Not available</p>
    <h3>Suggested next action</h3><p id="draweraction">Review the available evidence before contacting.</p>
  </aside>
</div>

<div id="tour">
  <div id="tourcard">
    <h3 id="tourtitle"></h3>
    <div id="tourbody"></div>
    <div class="tnav">
      <button type="button" class="tskip" id="tourskip">Skip tour</button>
      <span class="tstep" id="tourstep"></span>
      <button type="button" id="tournext">Next →</button>
    </div>
  </div>
</div>

 </main>
<script>
// ── Dark mode: respect prefers-color-scheme on first load, persist choice. ──
(function(){
  const tog=document.getElementById("themetoggle2");
  function apply(t){
    document.documentElement.setAttribute("data-theme", t);
    tog.textContent = t==="dark" ? "☀️ Light" : "🌙 Dark";
  }
  let saved=null; try{ saved=localStorage.getItem("leadgen_theme"); }catch(e){}
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  apply(saved || "dark");
  tog.addEventListener("click", ()=>{
    const next = document.documentElement.getAttribute("data-theme")==="dark" ? "light" : "dark";
    apply(next); try{ localStorage.setItem("leadgen_theme", next); }catch(e){}
  });
})();

const VERTS = {{ verticals|tojson }};
const vsel = document.getElementById("vertical");
VERTS.forEach(v => { const o=document.createElement("option"); o.value=v.key; o.textContent=v.label; vsel.appendChild(o); });

function syncVertical(){
  const v = VERTS.find(x=>x.key===vsel.value);
  document.getElementById("vdesc").textContent = v.description || "";
  const cb = document.getElementById("competitor_box");
  if (v.competitor_input){
    cb.style.display="block";
    document.getElementById("competitor_label").textContent = v.competitor_input.label;
    document.getElementById("competitor_help").textContent = v.competitor_input.help;
  } else cb.style.display="none";
  document.getElementById("src_overture").checked = v.default_sources.includes("overture");
  document.getElementById("src_osm").checked = v.default_sources.includes("osm");
}
vsel.addEventListener("change", syncVertical); syncVertical();

const form=document.getElementById("form"), go=document.getElementById("go");
const checkBtn=document.getElementById("check");
const checkout=document.getElementById("checkout");
const statusEl=document.getElementById("status"), summary=document.getElementById("summary");
const logwrap=document.getElementById("logwrap"), legend=document.getElementById("legend");
const dls=document.getElementById("dls"), tablewrap=document.getElementById("tablewrap"), table=document.getElementById("preview");
const progressbox=document.getElementById("progressbox"), runphase=document.getElementById("runphase");
const GO_LABEL="Find prospects →";
go.textContent=GO_LABEL;
let logPinned=true;
statusEl.addEventListener("scroll", ()=>{
  logPinned=statusEl.scrollHeight-statusEl.scrollTop-statusEl.clientHeight<24;
});

function esc(s){ return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c])); }

// Read an uploaded CRM .csv into memory (text) so the server can dedupe against it.
let crmText="";
document.getElementById("crm_file").addEventListener("change", e=>{
  const f=e.target.files[0]; const st=document.getElementById("crm_status");
  if(!f){ crmText=""; return; }
  const reader=new FileReader();
  reader.onload=()=>{ crmText=reader.result||"";
    const lines=crmText.split(/\\r?\\n/).filter(x=>x.trim()).length-1;
    st.textContent=`Loaded ${f.name} — about ${Math.max(0,lines)} companies will be skipped if found.`; };
  reader.readAsText(f);
});

function baseBody(){
  const sources=[]; if(document.getElementById("src_osm").checked) sources.push("osm");
  if(document.getElementById("src_overture").checked) sources.push("overture");
  const categories=[...document.querySelectorAll("#category_picker input[name=category]:checked")].map(o=>o.value);
  return {
    vertical:vsel.value, market:document.getElementById("market").value,
    category_filter:categories.includes("all")||!categories.length?"all":categories.join(","),
    radius_km:document.getElementById("radius_km").value,
    sources, enrich:document.getElementById("enrich").checked,
    enrich_cap:+document.getElementById("enrich_cap").value||0,
    limit:+document.getElementById("limit").value||0,
    competitor_urls:document.getElementById("competitor_urls").value,
    crm_csv:crmText,
  };
}

let lastBody=null;  // last run's request body, for the Retry button

async function startRun(body){
  lastBody=body;
  go.disabled=true; go.textContent="Working…";
  logwrap.style.display="block"; statusEl.style.display="block"; statusEl.textContent="Starting…"; checkout.innerHTML="";
  progressbox.style.display="block"; runphase.textContent="Preparing your search…";
  summary.style.display="none"; dls.style.display="none"; legend.style.display="none";
  tablewrap.style.display="none"; table.style.display="none";
  startMeter();
  let j;
  try{
    const r=await fetch("/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    j=await r.json();
    if(!r.ok) throw new Error(j.error||"Could not start the search.");
  }catch(error){
    checkout.innerHTML=`<div class="banner bad">${esc(error.message||"Could not start the search.")}</div>`;
    go.disabled=false; go.textContent=GO_LABEL; stopMeter(); progressbox.style.display="none";
    return {error:error.message};
  }
  if(j.error){ checkout.innerHTML=`<div class="banner bad">${esc(j.error)}</div>`;
    go.disabled=false; go.textContent=GO_LABEL; stopMeter();
    return Promise.resolve({error:j.error}); }
  return poll(j.job_id);
}

form.addEventListener("submit", e=>{
  e.preventDefault();
  const body=baseBody();
  for(const [id,min,max] of [["enrich_cap",0,100],["limit",1,500]]){
    const field=document.getElementById(id), value=Number(field.value);
    if(!Number.isInteger(value)||value<min||value>max){
      field.classList.add("field-invalid");field.setAttribute("aria-invalid","true");
      let error=field.nextElementSibling;
      if(!error||!error.classList.contains("field-error")){error=document.createElement("div");error.className="field-error";field.after(error);}
      error.textContent=`Enter a whole number from ${min} to ${max}.`;field.focus();return;
    }
    field.classList.remove("field-invalid");field.removeAttribute("aria-invalid");
    if(field.nextElementSibling&&field.nextElementSibling.classList.contains("field-error"))field.nextElementSibling.textContent="";
  }
  if(!body.market.trim()){ showLocationError("Enter a city, region, or other search area."); document.getElementById("market").focus(); return; }
  clearLocationError();
  if(!body.sources.length){ checkout.innerHTML='<div class="banner warn" role="alert">Select at least one data source to continue.</div>'; return; }
  saveMarket(body.market); startRun(body);
});

document.querySelectorAll("#category_picker input[name=category]").forEach(box=>{
  box.addEventListener("change", ()=>{
    const all=document.querySelector("#category_picker input[value=all]");
    if(box.value==="all" && box.checked){
      document.querySelectorAll("#category_picker input:not([value=all])").forEach(other=>other.checked=false);
    }else if(box.checked){ all.checked=false; }
    if(!document.querySelector("#category_picker input:checked")) all.checked=true;
    updateCategoryCount();
  });
});
function updateCategoryCount(){
  const chosen=[...document.querySelectorAll("#category_picker input:checked")];
  const all=chosen.some(x=>x.value==="all");
  document.getElementById("category_count").textContent=all?"All categories":`${chosen.length} selected`;
}
function showLocationError(message){const input=document.getElementById("market");input.classList.add("field-invalid");input.setAttribute("aria-invalid","true");document.getElementById("market_error").textContent=message;}
function clearLocationError(){const input=document.getElementById("market");input.classList.remove("field-invalid");input.removeAttribute("aria-invalid");document.getElementById("market_error").textContent="";}
document.getElementById("market").addEventListener("input",clearLocationError);
document.getElementById("clear_categories").addEventListener("click",()=>{document.querySelectorAll("#category_picker input").forEach(x=>x.checked=false);updateCategoryCount();});
updateCategoryCount();

checkBtn.addEventListener("click", async ()=>{
  checkBtn.disabled=true; checkout.innerHTML="<div class='hint'>Testing each data source…</div>";
  try{
    const j=await (await fetch("/check")).json();
    let h=j.results.map(p=>`<div class="probe"><span class="dot ${p.ok?'ok':'bad'}"></span>`
      +`<b>${esc(p.label)}</b> — ${esc(p.detail)}</div>`).join("");
    const cls=(j.can_overture||j.can_osm)?(j.can_overture&&j.can_osm?"good":"warn"):"bad";
    h+=`<div class="banner ${cls}">${esc(j.summary)}</div>`;
    checkout.innerHTML=h;
  }catch(e){ checkout.innerHTML="<div class='banner bad'>Could not run the check.</div>"; }
  checkBtn.disabled=false;
});

function poll(jid){
 return new Promise(resolve=>{
  const t=setInterval(async ()=>{
    let j;
    try{
      const r=await fetch("/progress/"+jid); j=await r.json();
      if(!r.ok) throw new Error(j.error||"Progress request failed");
    }catch(error){
      clearInterval(t); go.disabled=false; go.textContent=GO_LABEL; stopMeter();
      progressbox.style.display="none";
      checkout.innerHTML=`<div class="banner bad">${esc(error.message||"Could not read search progress.")}</div>`;
      resolve({error:error.message}); return;
    }
    statusEl.textContent=j.log.join("\\n"); if(logPinned) statusEl.scrollTop=statusEl.scrollHeight;
    updateMeter(j.log);
    updatePhase(j.log, j.done);
    if(j.done){
      clearInterval(t); go.disabled=false; go.textContent=GO_LABEL;
      stopMeter(); progressbox.style.display="none"; loadRecent();
      // Mid-run recovery: error OR a source reported "failed" in the log → retry banner.
      const logText=(j.log||[]).join("\\n");
      const sourceFailed=/failed/i.test(logText);
      if(j.error || sourceFailed){
        const msg=j.error || "Partial results: a selected data source failed, so coverage may be incomplete.";
        checkout.innerHTML=`<div class="banner ${j.error?"bad":"warn"}" id="retrybanner" role="status">${esc(msg)} `
          +`<button type="button" class="ghost" id="retrybtn" style="padding:5px 12px;margin-left:8px">↻ Retry</button></div>`;
        const rb=document.getElementById("retrybtn");
        if(rb) rb.addEventListener("click", ()=>{ if(lastBody) startRun(Object.assign({}, lastBody)); });
      }
      else if(j.notice){ checkout.innerHTML=`<div class="banner warn">${esc(j.notice)}</div>`; }
      if(j.stats){
        if(!j.error)document.getElementById("resultsheading").style.display="flex";
        summary.style.display="flex";
        s_total.textContent=j.stats.total; s_A.textContent=j.stats.A;
        const rows=j.leads||[];
        s_opportunity.textContent=rows.filter(r=>["NO_WEBSITE_FOUND","SOCIAL_ONLY","OUTDATED_WEBSITE","WEBSITE_BROKEN"].includes(r.website_status)).length;
        s_contact.textContent=rows.filter(r=>r.phone||r.business_email||r.email).length;
        s_review.textContent=rows.filter(r=>r.qualification==="Needs Review"||r.quality_warnings||r.website_status==="UNKNOWN").length;
      }
      if(j.files && (j.files.csv||j.files.xlsx)){
        dls.style.display="flex"; dls.innerHTML="";
        if(j.files.xlsx) dls.innerHTML+=`<a class="dl" href="/download/xlsx/${jid}">⬇ Download spreadsheet (Excel)</a>`;
        if(j.files.csv) dls.innerHTML+=`<a class="dl alt" href="/download/csv/${jid}">⬇ Download for CRM (.csv)</a>`;
      }
      if(j.leads && j.leads.length){
        legend.style.display="block";
        legend.innerHTML="<b>How to read this:</b> Opportunity and contact-confidence scores are separate research signals. "
          +"Review evidence and contact availability before outreach. "
          +"The full list (with phone, website, why it's a lead, and a suggested opener) is in the downloads above.";
        renderTable(j.leads, j.columns);
      }
      resolve({error:j.error, sourceFailed, stats:j.stats});
    }
  }, 700);
 });
}

let curLeads=[], curCols=[];
let visibleCols={};        // key -> bool (shown in preview)
let sortKey=null, sortDir=1; // 1 asc, -1 desc
let categoryOptions=new Set(), statusOptions=new Set();
const PHONE_KEYS=new Set(["phone","telephone","tel"]);
const EMAIL_KEYS=new Set(["email","e-mail","mail"]);

// ── Per-lead tagging (contacted / not-interested), persisted in localStorage. ──
function loadTags(){ try{ return JSON.parse(localStorage.getItem("leadgen_tags")||"{}"); }catch(e){ return {}; } }
function saveTags(t){ try{ localStorage.setItem("leadgen_tags", JSON.stringify(t)); }catch(e){} }
let tags=loadTags();
function leadKey(r){ return (String(r.name||"").trim()+"|"+String(r.phone||"").trim()).toLowerCase(); }

function renderTable(leads, columns){
  curLeads=leads||[]; curCols=columns||[];
  // Default visible columns: first 9 (matches prior "keep readable" behaviour), unless prefs saved.
  let savedVis=null; try{ savedVis=JSON.parse(localStorage.getItem("leadgen_cols")||"null"); }catch(e){}
  visibleCols={};
  curCols.forEach((c,i)=>{ const k=c[1];
    visibleCols[k] = savedVis && (k in savedVis) ? !!savedVis[k] : i<9; });
  sortKey=curCols.find(c=>c[1]==="score")?.[1]||null; sortDir=-1;
  categoryOptions=new Set(curLeads.map(r=>r.category).filter(Boolean));
  statusOptions=new Set(curLeads.map(r=>r.website_status).filter(Boolean));
  const locationOptions=new Set(curLeads.map(r=>r.city||r.locality||r.state).filter(Boolean));
  document.getElementById("r_category").innerHTML="<option value=''>All categories</option>"+
    [...categoryOptions].sort().map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join("");
  document.getElementById("r_status").innerHTML="<option value=''>All website statuses</option>"+
    [...statusOptions].sort().map(v=>`<option value="${esc(v)}">${esc(statusLabel(v))}</option>`).join("");
  document.getElementById("r_location").innerHTML="<option value=''>All locations</option>"+
    [...locationOptions].sort().map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join("");
  document.getElementById("resultsearch").style.display="block";
  document.getElementById("resulttools").style.display="flex";
  buildColPanel();
  drawRows();
}

function buildColPanel(){
  const p=document.getElementById("colpanel");
  p.innerHTML=curCols.map(c=>
    `<label><input type="checkbox" data-k="${esc(c[1])}" ${visibleCols[c[1]]?"checked":""}>${esc(c[0])}</label>`
  ).join("");
  p.querySelectorAll("input").forEach(cb=>cb.addEventListener("change", ()=>{
    visibleCols[cb.dataset.k]=cb.checked;
    try{ localStorage.setItem("leadgen_cols", JSON.stringify(visibleCols)); }catch(e){}
    drawRows();
  }));
}

function shownCols(){ return curCols.filter(c=>visibleCols[c[1]]); }

function filteredRows(){
  const q=(document.getElementById("r_filter").value||"").toLowerCase().trim();
  const category=document.getElementById("r_category").value;
  const location=document.getElementById("r_location").value;
  const websiteStatus=document.getElementById("r_status").value;
  const grade=document.getElementById("r_grade").value;
  const contact=document.getElementById("r_contact").value;
  const cols=shownCols();
  const match=r=>cols.some(c=>String(r[c[1]]==null?"":r[c[1]]).toLowerCase().includes(q));
  let rows=curLeads.filter(r=>(!q||match(r))&&(!category||r.category===category)&&
    (!location||(r.city||r.locality||r.state)===location)&&(!grade||String(r.tier||"")===grade)&&
    (!websiteStatus||r.website_status===websiteStatus)&&
    (!contact||(contact==="yes" ? !!(r.phone||r.business_email||r.email) : !(r.phone||r.business_email||r.email))));
  if(sortKey!=null){
    rows.sort((a,b)=>{
      let x=a[sortKey], y=b[sortKey];
      const nx=parseFloat(x), ny=parseFloat(y);
      let c;
      if(!isNaN(nx)&&!isNaN(ny)&&String(x).trim()!==""&&String(y).trim()!=="") c=nx-ny;
      else c=String(x==null?"":x).toLowerCase().localeCompare(String(y==null?"":y).toLowerCase());
      return c*sortDir;
    });
  }
  return rows;
}

function cellHTML(r, key){
  const raw=r[key]; const v=String(raw==null?"":raw);
  if(!v){
    if(PHONE_KEYS.has(key)||EMAIL_KEYS.has(key)) return '<span class="notfound">Not found</span>';
    if(["website","source_url","website_audit_url"].includes(key)) return '<span class="notfound">Not verified</span>';
    return '<span class="notfound">—</span>';
  }
  if(key==="website_status"||key==="qualification"){
    return `<span class="statusbadge">${esc(key==="website_status"?statusLabel(v):v)}</span>`;
  }
  if(["website","source_url","contact_source","website_audit_url"].includes(key)
      && /^https?:\/\//i.test(v)){
    return `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>`;
  }
  if(PHONE_KEYS.has(key)){
    const tel=v.replace(/[^0-9+]/g,"");
    return `<a href="tel:${esc(tel)}">${esc(v)}</a>`
      +`<button type="button" class="cellbtn" data-copy="${esc(v)}">copy</button>`;
  }
  if(EMAIL_KEYS.has(key)){
    return `<a href="mailto:${esc(v)}">${esc(v)}</a>`
      +`<button type="button" class="cellbtn" data-copy="${esc(v)}">copy</button>`;
  }
  return esc(v);
}
function statusLabel(value){
  const labels={NO_WEBSITE_FOUND:"No website found",VERIFIED_NO_WEBSITE:"No website found",SOCIAL_ONLY:"Social-only presence",OUTDATED_WEBSITE:"Outdated website",WEBSITE_BROKEN:"Broken website",FUNCTIONAL_WEBSITE:"Functional website",BASIC_WEBSITE:"Functional website",UNKNOWN:"Inconclusive audit",NOT_CHECKED:"Not checked"};
  return labels[value]||String(value||"Not checked").replace(/_/g," ").toLowerCase().replace(/(^|\s)\S/g,m=>m.toUpperCase());
}

function drawRows(){
  updateActiveFilters();
  const cols=shownCols();
  const all=filteredRows();
  const rows=all.slice(0,500);
  document.getElementById("exportcount").textContent=
    all.length+" row(s) match"+(curLeads.length!==all.length?" of "+curLeads.length:"");
  const arrow=k=> sortKey===k ? `<span class="arrow">${sortDir>0?"▲":"▼"}</span>` : `<span class="arrow">↕</span>`;
  let h="<thead><tr>"+cols.map(c=>`<th data-k="${esc(c[1])}" tabindex="0" role="button" aria-label="Sort by ${esc(c[0])}">${esc(c[0])}${arrow(c[1])}</th>`).join("")
    +`<th>Status</th></tr></thead><tbody>`;
  for(const r of rows){
    const tier=r.tier||"C";
    const tg=tags[leadKey(r)]||"";
    const tagClass=tg?` tagged-${esc(tg)}`:"";
    h+=`<tr class="tier${esc(tier)}${tagClass}">`
      +cols.map(c=>`<td>${cellHTML(r,c[1])}</td>`).join("")
      +`<td><button type="button" class="cellbtn draweropen" data-detail="${esc(leadKey(r))}">Details</button><span class="tagbtns" data-key="${esc(leadKey(r))}">`
      +`<button type="button" data-tag="contacted" class="${tg==='contacted'?'on-c':''}" title="Mark contacted">✓</button>`
      +`<button type="button" data-tag="not_interested" class="${tg==='not_interested'?'on-n':''}" title="Not interested">✕</button>`
      +`</span></td></tr>`;
  }
  table.innerHTML=h+"</tbody>"; table.style.display="table"; tablewrap.style.display="block";
  // header sort handlers
  table.querySelectorAll("th[data-k]").forEach(th=>th.addEventListener("click", ()=>{
    const k=th.dataset.k;
    if(sortKey===k) sortDir=-sortDir; else { sortKey=k; sortDir=1; }
    drawRows();
  }));
  table.querySelectorAll("th[data-k]").forEach(th=>th.addEventListener("keydown",ev=>{if(ev.key==="Enter"||ev.key===" "){ev.preventDefault();th.click();}}));
  // copy buttons
  table.querySelectorAll("button[data-copy]").forEach(b=>b.addEventListener("click", ev=>{
    ev.stopPropagation();
    const txt=b.dataset.copy;
    const done=()=>{ const o=b.textContent; b.textContent="✓"; setTimeout(()=>b.textContent=o,900); };
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done,done);
    else { const ta=document.createElement("textarea"); ta.value=txt; document.body.appendChild(ta);
           ta.select(); try{document.execCommand("copy");}catch(e){} ta.remove(); done(); }
  }));
  // tag buttons (mark contacted / not-interested; click again to clear)
  table.querySelectorAll(".tagbtns button").forEach(b=>b.addEventListener("click", ()=>{
    const key=b.parentNode.dataset.key, val=b.dataset.tag;
    if(tags[key]===val) delete tags[key]; else tags[key]=val;
    saveTags(tags); drawRows();
  }));
  table.querySelectorAll("[data-detail]").forEach(b=>b.addEventListener("click", ()=>{
    const record=curLeads.find(item=>leadKey(item)===b.dataset.detail); if(record) openDrawer(record);
  }));
}
function updateActiveFilters(){
  const entries=[["r_filter","Search"],["r_category","Category"],["r_location","Location"],["r_status","Website status"],["r_grade","Grade"],["r_contact","Contact"]];
  const chips=[];
  for(const [id,label] of entries){
    const el=document.getElementById(id);if(!el.value)continue;
    const value=el.tagName==="SELECT"?el.options[el.selectedIndex]?.text:el.value;
    chips.push(`<button type="button" class="filterchip" data-clear-filter="${id}" aria-label="Remove ${esc(label)} filter">${esc(label)}: ${esc(value)} ×</button>`);
  }
  const box=document.getElementById("activefilters");box.innerHTML=chips.join("");
  box.querySelectorAll("[data-clear-filter]").forEach(b=>b.addEventListener("click",()=>{document.getElementById(b.dataset.clearFilter).value="";drawRows();}));
}
document.getElementById("r_filter").addEventListener("input", drawRows);
["r_category","r_location","r_status","r_grade","r_contact"].forEach(id=>document.getElementById(id).addEventListener("change",drawRows));
document.getElementById("r_clear").addEventListener("click", ()=>{
  document.getElementById("r_filter").value="";
  ["r_category","r_location","r_status","r_grade","r_contact"].forEach(id=>document.getElementById(id).value="");
  drawRows();
});

let drawerReturnFocus=null;
function openDrawer(r){
  drawerReturnFocus=document.activeElement;
  const value=(key, fallback="Not verified")=>r[key] ? String(r[key]) : fallback;
  document.getElementById("drawertitle").textContent=value("name","Prospect");
  document.getElementById("drawersubtitle").textContent=[value("category","Category not found"),value("city","Location not verified")].join(" · ");
  document.getElementById("drawergrid").innerHTML=[
    ["Website status",statusLabel(value("website_status","Not checked"))],["Opportunity score",value("score","Not scored")],
    ["Phone",value("phone")],["Email",value("business_email",value("email"))],
    ["Website",value("website","Not found")],["Last checked",value("checked_at")]
  ].map(([label,data])=>`<div><b>${esc(label)}</b>${label==="Website"&&/^https?:\/\//i.test(data)?`<a href="${esc(data)}" target="_blank" rel="noopener">${esc(data)}</a>`:`<span>${esc(data)}</span>`}</div>`).join("");
  document.getElementById("drawerwhy").textContent=value("why");
  document.getElementById("drawerevidence").textContent=[value("website_status_evidence"),value("website_status_confidence"),value("audit_notes","")].filter(Boolean).join(" · ");
  document.getElementById("drawersource").textContent=value("source_url","Source not available");
  const status=r.website_status||"";
  document.getElementById("draweraction").textContent=status==="UNKNOWN"||status==="WEBSITE_BROKEN" ?
    "Re-check the website and verify contact details before outreach." :
    status==="FUNCTIONAL_WEBSITE" ? "Review the site manually for a specific improvement angle." :
    "Verify the business and contact details before contacting.";
  document.getElementById("drawerbackdrop").style.display="block";
  document.getElementById("drawerbackdrop").setAttribute("aria-hidden","false");
  document.getElementById("drawerclose").focus();
}
function closeDrawer(){ document.getElementById("drawerbackdrop").style.display="none"; document.getElementById("drawerbackdrop").setAttribute("aria-hidden","true"); if(drawerReturnFocus&&drawerReturnFocus.focus)drawerReturnFocus.focus(); }
document.getElementById("drawerclose").addEventListener("click",closeDrawer);
document.getElementById("drawerbackdrop").addEventListener("click",e=>{if(e.target.id==="drawerbackdrop")closeDrawer();});
document.addEventListener("keydown",e=>{
  const backdrop=document.getElementById("drawerbackdrop");if(backdrop.style.display!=="block")return;
  if(e.key==="Escape"){closeDrawer();return;}
  if(e.key==="Tab"){
    const focusable=[...backdrop.querySelectorAll("a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])")];
    const first=focusable[0],last=focusable[focusable.length-1];
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  }
});

// Column chooser open/close
document.getElementById("colbtn").addEventListener("click", e=>{
  e.stopPropagation();
  const p=document.getElementById("colpanel");
  p.style.display = p.style.display==="block" ? "none" : "block";
});
document.addEventListener("click", e=>{
  const p=document.getElementById("colpanel");
  if(p && !document.getElementById("colmenu").contains(e.target)) p.style.display="none";
});

// ── Export exactly the filtered + sorted rows, with visible columns, as CSV. ──
function csvCell(v){ v=String(v==null?"":v);
  return /[",\\n\\r]/.test(v) ? '"'+v.replace(/"/g,'""')+'"' : v; }
document.getElementById("exportbtn").addEventListener("click", ()=>{
  const cols=shownCols();
  const rows=filteredRows();
  const header=cols.map(c=>csvCell(c[0])).concat(["status"]);
  const lines=[header.join(",")];
  for(const r of rows){
    const row=cols.map(c=>csvCell(r[c[1]])).concat([csvCell(tags[leadKey(r)]||"")]);
    lines.push(row.join(","));
  }
  const blob=new Blob(["\\ufeff"+lines.join("\\r\\n")],{type:"text/csv;charset=utf-8"});
  const url=URL.createObjectURL(blob);
  const a=document.createElement("a");
  a.href=url; a.download="leads_filtered.csv"; document.body.appendChild(a); a.click();
  a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
});

// Recent runs (this session) — re-download without re-running.
async function loadRecent(){
  try{
    const list=await (await fetch("/recent")).json();
    const wrap=document.getElementById("recentwrap"), el=document.getElementById("recentlist");
    if(!list.length){ wrap.style.display="none"; return; }
    el.innerHTML=list.map(r=>{
      const f=r.files||{};
      const links=[f.xlsx?`<a href="/download/xlsx/${r.jid}">⬇ Excel</a>`:"",
                   f.csv?`<a href="/download/csv/${r.jid}">⬇ CSV</a>`:""].filter(Boolean).join(" · ");
      return `<div class="rcard"><b>${esc(r.label)}</b>`
        +`<div class="meta">${esc(r.market)} · ${esc(r.when)}</div>`
        +`${r.stats.total} leads · 🟢${r.stats.A} 🟡${r.stats.B} 🔴${r.stats.C}`
        +`<div style="margin-top:5px">${links}</div></div>`;
    }).join("");
    wrap.style.display="block";
  }catch(e){}
}

// Remember markets the user actually searched, across sessions.
function loadSavedMarkets(){
  try{
    const saved=JSON.parse(localStorage.getItem("leadgen_markets")||"[]");
    const dl=document.getElementById("markets");
    saved.forEach(m=>{ if(![...dl.options].some(o=>o.value===m)){
      const o=document.createElement("option"); o.value=m; dl.appendChild(o); } });
  }catch(e){}
}
function saveMarket(m){
  if(!m||!m.trim()) return;
  try{
    let saved=JSON.parse(localStorage.getItem("leadgen_markets")||"[]");
    saved=[m,...saved.filter(x=>x!==m)].slice(0,8);
    localStorage.setItem("leadgen_markets",JSON.stringify(saved));
  }catch(e){}
}

// ─────────────────────────── Settings panel ────────────────────────────────
// Default sources, default enrich cap, crawler UA — persisted in localStorage.
const SET_KEY="leadgen_settings";
function loadSettings(){ try{ return JSON.parse(localStorage.getItem(SET_KEY)||"null"); }catch(e){ return null; } }
function applySettings(s){
  if(!s) return;
  if(Array.isArray(s.sources)){
    document.querySelectorAll("#set_sources input").forEach(cb=>cb.checked=s.sources.includes(cb.dataset.src));
    // Mirror onto the run form's source checkboxes.
    const map={overture:"src_overture",osm:"src_osm",socrata:"src_socrata",npi:"src_npi",foursquare:"src_foursquare"};
    Object.entries(map).forEach(([src,id])=>{ const el=document.getElementById(id); if(el) el.checked=s.sources.includes(src); });
  }
  if(s.enrich_cap!=null && s.enrich_cap!==""){
    document.getElementById("set_enrich_cap").value=s.enrich_cap;
    document.getElementById("enrich_cap").value=s.enrich_cap;
  }
  if(s.ua!=null) document.getElementById("set_ua").value=s.ua;
}
function readSettingsForm(){
  const sources=[...document.querySelectorAll("#set_sources input")].filter(cb=>cb.checked).map(cb=>cb.dataset.src);
  return { sources, enrich_cap:document.getElementById("set_enrich_cap").value,
           ua:document.getElementById("set_ua").value };
}
document.getElementById("set_save").addEventListener("click", ()=>{
  const s=readSettingsForm();
  try{ localStorage.setItem(SET_KEY, JSON.stringify(s)); }catch(e){}
  applySettings(s);
  const saved=document.getElementById("set_saved");
  saved.style.display="inline"; setTimeout(()=>saved.style.display="none",1500);
});
document.getElementById("set_clear").addEventListener("click", ()=>{
  try{ localStorage.removeItem(SET_KEY); }catch(e){}
  document.querySelectorAll("#set_sources input").forEach(cb=>cb.checked=false);
  document.getElementById("set_enrich_cap").value="";
  document.getElementById("set_ua").value="";
});
// Apply saved settings on load — but a vertical change can still re-set sources via syncVertical.
applySettings(loadSettings());
// Force safe Arvett defaults; settings panel is intentionally hidden in this MVP.
document.getElementById("src_osm").checked = true;
document.getElementById("src_overture").checked = false;

// ─────────────────────────── Run queue ─────────────────────────────────────
// Queue (vertical, market) combos and run them sequentially through /run + /progress.
let queue=[];          // {vertical, vlabel, market}
let queueRunning=false;
const qlist=document.getElementById("qlist"), queuewrap=document.getElementById("queuewrap");
function renderQueue(){
  document.getElementById("queuecount").textContent = queue.length ? `(${queue.length})` : "";
  queuewrap.style.display = queue.length ? "block" : "none";
  if(queue.length && !queueRunning) queuewrap.open=true;
  qlist.innerHTML=queue.map((q,i)=>{
    const st=q.state||"queued";
    const cls=st==="active"?"active":st==="done"?"done":st==="failed"?"failed":"";
    const label={queued:"queued",active:"running…",done:"done ✓",failed:"failed"}[st]||st;
    return `<li class="qitem ${cls}"><span class="qmeta"><b>${esc(q.vlabel)}</b> · ${esc(q.market)}</span>`
      +`<span class="qstate">${esc(label)}</span>`
      +(queueRunning?"":`<button type="button" class="qrm" data-i="${i}">✕</button>`)+`</li>`;
  }).join("");
  qlist.querySelectorAll("button.qrm").forEach(b=>b.addEventListener("click", ()=>{
    queue.splice(+b.dataset.i,1); renderQueue();
  }));
}
document.getElementById("enqueue").addEventListener("click", ()=>{
  const market=document.getElementById("market").value.trim();
  if(!market){ alert("Type an area before adding to the queue."); return; }
  const v=VERTS.find(x=>x.key===vsel.value);
  queue.push({vertical:vsel.value, vlabel:(v&&v.label)||vsel.value, market, state:"queued"});
  saveMarket(market); renderQueue();
});
document.getElementById("queueclear").addEventListener("click", ()=>{
  if(queueRunning) return; queue=[]; renderQueue();
});
document.getElementById("queuerun").addEventListener("click", runQueue);
async function runQueue(){
  if(queueRunning || !queue.length) return;
  queueRunning=true; renderQueue();
  // Sources / advanced options taken once from the form (baseBody) and reused per combo.
  const tmpl=baseBody();
  for(const q of queue){
    if(q.state==="done") continue;
    q.state="active"; renderQueue();
    const body=Object.assign({}, tmpl, {vertical:q.vertical, market:q.market});
    let res;
    try{ res=await startRun(body); }catch(e){ res={error:String(e)}; }
    q.state=(res&&(res.error||res.sourceFailed))?"failed":"done";
    renderQueue();
  }
  queueRunning=false; renderQueue();
}

// ────────────────── Per-source progress / ETA + elapsed timer ───────────────
// Parse streaming log lines like "  Overture: 412 businesses" / "  NPI: 128 providers…"
let meterTimer=null, meterStart=0;
const SOURCE_LINE=/^\\s*([A-Za-z][A-Za-z0-9 ]*?):\\s*([\\d,]+)\\b/;
function startMeter(){
  meterStart=Date.now();
  document.getElementById("runmeter").style.display="flex";
  document.getElementById("runchips").innerHTML="";
  document.getElementById("runtimer").textContent="⏱ 0:00";
  clearInterval(meterTimer);
  meterTimer=setInterval(()=>{
    const s=Math.floor((Date.now()-meterStart)/1000);
    document.getElementById("runtimer").textContent=`⏱ ${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
  }, 500);
}
function updateMeter(log){
  if(!log) return;
  const tally={};
  for(let line of log){
    line=line.replace(/^\\[\\d{2}:\\d{2}:\\d{2}\\]\\s*/,"");  // drop timestamp prefix
    const m=line.match(SOURCE_LINE);
    if(m){
      const name=m[1].trim(), n=parseInt(m[2].replace(/,/g,""),10);
      if(name && !isNaN(n)) tally[name]=n;   // last value for a source wins
    }
  }
  const chips=Object.entries(tally).map(([k,v])=>`<span class="chip"><b>${esc(k)}</b> ${v}</span>`).join("");
  document.getElementById("runchips").innerHTML=chips;
}
function updatePhase(log, done){
  if(done){ runphase.textContent="Search complete — preparing your results"; return; }
  const text=(log||[]).join(" ");
  const phases=[
    [/Market:|bbox=/,"Resolving the search area…"],
    [/Overture:|OSM:|Socrata:|NPI:|Foursquare:/,"Collecting businesses from selected sources…"],
    [/deduping|after dedupe/i,"Removing duplicate listings…"],
    [/radius filter|category filter/i,"Checking location and category fit…"],
    [/Enriching|enriched/i,"Auditing selected websites…"],
    [/Scored/i,"Scoring website opportunities…"],
    [/ERROR/i,"Recovering from a source issue…"]
  ];
  const match=phases.find(([pattern])=>pattern.test(text));
  runphase.textContent=match ? match[1] : "Working through your search…";
}
function stopMeter(){ clearInterval(meterTimer); meterTimer=null; }

// ─────────────────────────── First-run tour ────────────────────────────────
const TOUR_KEY="leadgen_tour_done";
const TOUR_STEPS=[
  {sel:"#vertical", title:"1 · Pick what to find",
   body:"Start here — choose the type of business you want to prospect for."},
  {sel:"#market", title:"2 · Set the area",
   body:"Type any city, county, or metro, like “Austin, Texas”."},
];
let tourIdx=0;
function tourSeen(){ try{ return localStorage.getItem(TOUR_KEY)==="1"; }catch(e){ return true; } }
function endTour(){
  try{ localStorage.setItem(TOUR_KEY,"1"); }catch(e){}
  document.getElementById("tour").style.display="none";
  document.querySelectorAll(".tourhi").forEach(el=>el.classList.remove("tourhi"));
}
function showTourStep(){
  document.querySelectorAll(".tourhi").forEach(el=>el.classList.remove("tourhi"));
  const step=TOUR_STEPS[tourIdx];
  const target=document.querySelector(step.sel);
  document.getElementById("tourtitle").textContent=step.title;
  document.getElementById("tourbody").textContent=step.body;
  document.getElementById("tourstep").textContent=`${tourIdx+1} / ${TOUR_STEPS.length}`;
  document.getElementById("tournext").textContent=(tourIdx===TOUR_STEPS.length-1)?"Got it":"Next →";
  const card=document.getElementById("tourcard");
  if(target){
    card.style.transform="none";
    target.classList.add("tourhi");
    if(target.scrollIntoView) target.scrollIntoView({block:"center"});
    const rb=target.getBoundingClientRect();
    let top=rb.bottom+10, left=rb.left;
    // Keep the card on-screen.
    requestAnimationFrame(()=>{
      const cw=card.offsetWidth, ch=card.offsetHeight;
      if(left+cw>window.innerWidth-12) left=Math.max(12, window.innerWidth-cw-12);
      if(top+ch>window.innerHeight-12) top=Math.max(12, rb.top-ch-10);
      card.style.left=left+"px"; card.style.top=top+"px";
    });
  } else {
    card.style.left="50%"; card.style.top="40%"; card.style.transform="translate(-50%,-50%)";
  }
}
document.getElementById("tournext").addEventListener("click", ()=>{
  tourIdx++;
  if(tourIdx>=TOUR_STEPS.length) endTour(); else showTourStep();
});
document.getElementById("tourskip").addEventListener("click", endTour);
function maybeStartTour(){
  if(tourSeen()) return;
  tourIdx=0;
  document.getElementById("tour").style.display="block";
  showTourStep();
}

loadSavedMarkets();
</script>
</body></html>"""


def main():
    port = int(os.environ.get("PORT", "5000"))
    host = "0.0.0.0"
    print("\n" + "=" * 46)
    print(f"  Arvett LeadFinder  →  http://{host}:{port}")
    print("=" * 46 + "\n")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
