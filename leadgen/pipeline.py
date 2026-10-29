"""
The pipeline: collect → dedupe → enrich → suppress → score → export.
Vertical-agnostic; all use-case specifics come from the Vertical object.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from .geo import resolve_market, resolve_center
from .sources import (overture_collect, osm_collect, socrata_collect,
                      npi_collect, foursquare_collect, arcgis_collect)
from .suppression import norm, build_suppression_set
from .export import write_outputs
from .vertical import Vertical


def load_crm_names(path_or_text: str, *, is_text: bool = False) -> set[str]:
    """Normalized company names from an existing CRM export (CSV) so the pipeline
    never hands back a business the user already has.

    Reads the first column that looks like a company/business/name field; falls
    back to the first column. Accepts a file path or raw CSV text (is_text=True).
    """
    import csv
    import io
    if not path_or_text:
        return set()
    if is_text:
        f = io.StringIO(path_or_text)
    else:
        f = open(path_or_text, newline="", encoding="utf-8-sig", errors="replace")
    try:
        reader = csv.reader(f)
        rows = list(reader)
    finally:
        if not is_text:
            f.close()
    if not rows:
        return set()
    header = [h.strip().lower() for h in rows[0]]
    name_keys = ("company name", "company", "business", "business name", "brokerage",
                 "name", "account name", "organization")
    col = next((i for i, h in enumerate(header) if h in name_keys), 0)
    out: set[str] = set()
    for row in rows[1:]:
        if col < len(row):
            k = norm(row[col])
            if k:
                out.add(k)
    return out


def _dedupe(leads: list[dict]) -> list[dict]:
    """Collapse same-name duplicates (across sources) within one market."""
    seen: dict[str, dict] = {}
    for r in leads:
        key = norm(r.get("name", ""))
        if not key:
            continue
        if key in seen:
            # prefer the record that already has a website / more fields
            cur = seen[key]
            if not cur.get("website") and r.get("website"):
                cur["website"] = r["website"]
            if not cur.get("phone") and r.get("phone"):
                cur["phone"] = r["phone"]
        else:
            seen[key] = r
    return list(seen.values())


def _matches_business_category(rec: dict, selected: str) -> bool:
    """Map raw map-provider categories into prospecting-friendly groups."""
    category = str(rec.get("category") or "").strip().lower().replace("-", "_")
    name = str(rec.get("name") or "").lower()
    groups = {
        "food": {"restaurant", "cafe", "bar", "pub", "fast_food", "food_court", "ice_cream", "bakery", "food"},
        "retail": {"clothes", "furniture", "supermarket", "convenience", "department_store", "electronics", "computer", "mobile_phone", "shoes", "books", "gift", "hardware", "jewelry", "chemist", "mall", "retail", "shop"},
        "healthcare": {"clinic", "dentist", "doctors", "pharmacy", "hospital", "veterinary", "healthcare", "physiotherapist", "optician", "medical"},
        "professional": {"office", "it", "company", "lawyer", "accountant", "architect", "insurance", "estate_agent", "consulting", "coworking", "craft", "electrician", "plumber", "carpenter", "construction", "engineer"},
        "automotive": {"fuel", "car_wash", "car_rental", "car_repair", "tyres", "vehicle", "motorcycle", "garage"},
        "hospitality": {"hotel", "guest_house", "motel", "hostel", "bed_and_breakfast"},
        "fitness": {"fitness_centre", "sports_centre", "gym", "sports"},
        "beauty": {"beauty", "hairdresser", "barber", "spa", "nail_salon", "massage"},
    }
    if "," in selected:
        return any(_matches_business_category(rec, item.strip()) for item in selected.split(",") if item.strip())
    if selected not in groups:
        return True
    tokens = groups[selected]
    if category in tokens:
        return True
    return not category and any(token in name for token in tokens)


def _category_exclusion(rec: dict) -> str:
    category = str(rec.get("category") or "").lower()
    exclusions = {
        "government": "Government facility",
        "atm": "ATM or generic financial infrastructure",
        "place_of_worship": "Religious place or landmark",
        "religious": "Religious place or landmark",
        "bank": "Financial institution",
        "bureau_de_change": "Financial institution",
    }
    for token, reason in exclusions.items():
        if token in category:
            return reason
    return ""


def run_pipeline(vertical: Vertical, market: str, *,
                 sources=("osm",), limit: int | None = 100,
                 enrich: bool = True, enrich_cap: int | None = 20,
                 category_filter: str | None = None, radius_km: float | None = None,
                 out_stem: str | None = None, config_override: dict | None = None,
                 exclude_names: set | None = None, demo: bool = False,
                 log=print) -> list[dict]:
    """Run the full pipeline for one vertical in one market.

    sources: any of ("overture", "osm").
    enrich:  run the vertical's enrich_fn (fetches each business's site).
    enrich_cap: only enrich the top-N by collection order (cost control); None = all.
    out_stem: if set, also writes <stem>_crm.csv and <stem>.xlsx.
    config_override: shallow-merged over the vertical's config for THIS run only
                     (e.g. competitor seed URLs typed into the GUI). The shared
                     vertical object is never mutated.
    exclude_names: normalized company names to drop (e.g. the user's existing CRM)
                   — see load_crm_names(). Matches are removed, not just flagged.
    demo: if True, use bundled offline sample data + fixture enrichment (no network).
    Returns the scored, sorted list of lead dicts. Output file paths, when written,
    are attached as run_pipeline.last_outputs = (csv_path, xlsx_path).
    """
    run_pipeline.last_outputs = None
    run_pipeline.last_stats = {
        "target_market": market, "radius_km": radius_km,
        "raw_records": 0, "geographic_rejected": 0, "duplicates_removed": 0,
        "category_excluded": 0, "enrichment": {},
    }
    # Per-run config: copy so concurrent runs / the registry stay clean.
    cfg = dict(vertical.config)
    if config_override:
        cfg.update(config_override)

    # 1. COLLECT
    leads: list[dict] = []
    attempted_sources: list[str] = []
    successful_sources: list[str] = []
    source_failures: list[tuple[str, Exception]] = []
    if demo:
        from .demo import demo_records
        leads = demo_records()
        log(f"DEMO MODE — using {len(leads)} bundled sample businesses (no network).")
    else:
        bbox, label = resolve_market(market)
        log(f"Market: {label}  bbox={tuple(round(x,3) for x in bbox)}")
        if "overture" in sources:
            attempted_sources.append("overture")
            try:
                leads += overture_collect(bbox, vertical.overture_categories, limit, log)
                successful_sources.append("overture")
            except Exception as e:
                source_failures.append(("overture", e))
                log(f"  Overture failed: {e}")
        if "osm" in sources and vertical.osm_tags:
            attempted_sources.append("osm")
            try:
                leads += osm_collect(bbox, vertical.osm_tags, log)
                successful_sources.append("osm")
            except Exception as e:
                source_failures.append(("osm", e))
                log(f"  OSM failed: {e}")
        if "socrata" in sources:
            try:
                leads += socrata_collect(label, limit=limit, log=log,
                                         datasets=cfg.get("socrata_datasets"))
            except Exception as e:
                log(f"  Socrata failed: {e}")
        if "npi" in sources:
            try:
                leads += npi_collect(label, limit=limit, log=log,
                                     taxonomies=cfg.get("npi_taxonomies"))
            except Exception as e:
                log(f"  NPI failed: {e}")
        if "foursquare" in sources:
            try:
                leads += foursquare_collect(bbox, vertical.overture_categories,
                                            limit, log)
            except Exception as e:
                log(f"  Foursquare failed: {e}")
        if "arcgis" in sources:
            try:
                leads += arcgis_collect(label, limit=limit, log=log,
                                        layers=cfg.get("arcgis_layers"))
            except Exception as e:
                log(f"  ArcGIS failed: {e}")
        # Extra no-key sources (lazy import so they're fully optional).
        if {"wikidata", "ckan", "localfile", "url_csv"} & set(sources):
            from .extra_sources import (wikidata_collect, ckan_collect,
                                        localfile_collect, url_csv_collect)
            if "wikidata" in sources:
                try:
                    leads += wikidata_collect(bbox, limit=limit, log=log)
                except Exception as e:
                    log(f"  Wikidata failed: {e}")
            if "ckan" in sources:
                try:
                    leads += ckan_collect(label, limit=limit, log=log)
                except Exception as e:
                    log(f"  CKAN failed: {e}")
            if "localfile" in sources and cfg.get("localfile_path"):
                try:
                    leads += localfile_collect(cfg["localfile_path"], limit=limit, log=log)
                except Exception as e:
                    log(f"  Local file failed: {e}")
            if "url_csv" in sources and cfg.get("url_csv"):
                try:
                    leads += url_csv_collect(cfg["url_csv"], limit=limit, log=log)
                except Exception as e:
                    log(f"  URL CSV failed: {e}")
        if attempted_sources and not successful_sources and source_failures:
            details = "; ".join(f"{name}: {error}" for name, error in source_failures)
            raise RuntimeError(f"All selected sources failed; no empty success is reported. {details}")
    # Optional radius filter: require source coordinates and keep only records inside
    # the geocoded center radius. Unknown coordinates are excluded when radius is set.
    if radius_km is not None and radius_km > 0 and not demo:
        center = resolve_center(market)
        from .quality import haversine_m, valid_coordinates
        before = len(leads)
        for r in leads:
            if not valid_coordinates(r.get("lat"), r.get("lon")):
                r["geographic_match"] = "UNKNOWN"
                r["geographic_match_confidence"] = "No valid coordinates"
                r["excluded_reason"] = "Missing or invalid coordinates for radius check"
                run_pipeline.last_stats["geographic_rejected"] += 1
                continue
            distance = haversine_m(center["lat"], center["lon"], r["lat"], r["lon"]) / 1000
            r["distance_km"] = round(distance, 3)
            r["geographic_match"] = "IN_RADIUS" if distance <= radius_km else "OUTSIDE_RADIUS"
            r["geographic_match_confidence"] = "Coordinate distance"
            if distance > radius_km:
                r["excluded_reason"] = f"Outside requested radius ({distance:.2f} km)"
                run_pipeline.last_stats["geographic_rejected"] += 1
        log(f"  radius filter {radius_km:g} km: kept {before - run_pipeline.last_stats['geographic_rejected']} of {before}")

    # Apply the requested business group before enrichment so irrelevant sites are not visited.
    if category_filter and category_filter != "all":
        before = len(leads)
        for r in leads:
            if not _matches_business_category(r, category_filter):
                r["excluded_reason"] = f"Category does not match '{category_filter}'"
                r["category_excluded"] = True
                run_pipeline.last_stats["category_excluded"] += 1
        # Keep excluded records for the All Businesses sheet and manual review.
        log(f"  category filter '{category_filter}': retained {len(leads)} with exclusions from {before}")
    for r in leads:
        reason = _category_exclusion(r)
        if reason:
            r["category_excluded"] = True
            r["excluded_reason"] = reason
            run_pipeline.last_stats["category_excluded"] += 1

    # Preserve the user's target market separately from source-provided address fields.
    for r in leads:
        r.setdefault("target_market", market)
        r.setdefault("category_group", category_filter or "all")
        r.setdefault("owner_name", "")
        r.setdefault("owner_role", "")
        r.setdefault("owner_source", "")
        r.setdefault("employee_estimate", "")
        r.setdefault("company_size", "Unknown")
        r.setdefault("company_size_source", "")
        r.setdefault("company_size_confidence", "Unknown")
        r.setdefault("website_status", "NO_WEBSITE_FOUND" if not r.get("website")
                    else "UNKNOWN")
        r.setdefault("website_status_evidence", "Source URL absence is not proof of no website.")
        r.setdefault("business_email", r.get("email") or "")
        r.setdefault("original_phone", r.get("phone") or "")
        r.setdefault("field_sources", {})
        r.setdefault("source_references", [{
            "source": r.get("source", ""), "source_id": r.get("source_id", ""),
            "source_url": r.get("source_url", "")
        }])
        r.setdefault("website_audit_url", "")
        r.setdefault("contact_confidence", "Low" if not (r.get("phone") or r.get("email")) else "Source-listed")
        r.setdefault("contact_source", r.get("source_url") if (r.get("phone") or r.get("email")) else "")
        r.setdefault("phone_source", r.get("field_sources", {}).get("phone", ""))
        r.setdefault("audit_notes", "")
        r.setdefault("checked_at", datetime.now().strftime("%Y-%m-%d %H:%M"))
        r["location_confidence"] = "Source-listed" if r.get("city") else "Target area only; city not present in source"

    # Drop permanently-closed businesses, then normalize phones (helps dialing +
    # cross-source dedupe identity).
    from .quality import cross_source_dedupe, normalize_phone, normalize_locality, is_closed
    before = len(leads)
    leads = [r for r in leads if not is_closed(r)]
    if before - len(leads):
        log(f"  dropped {before - len(leads)} closed businesses")
    for r in leads:
        r.setdefault("city_original", r.get("city") or "")
        r["city_normalized"] = normalize_locality(r.get("city") or "")
        r.setdefault("country", "India" if "india" in market.lower() else "")
        if r.get("phone"):
            r["original_phone"] = r.get("original_phone") or r["phone"]
            r["normalized_phone"] = normalize_phone(r["phone"], default_country="IN")
            r["phone"] = r["normalized_phone"] or r["phone"]
    run_pipeline.last_stats["raw_records"] = len(leads)
    run_pipeline.last_stats["sources_attempted"] = attempted_sources
    run_pipeline.last_stats["sources_succeeded"] = successful_sources
    run_pipeline.last_stats["source_failures"] = [
        {"source": name, "error": str(error)} for name, error in source_failures
    ]
    log(f"Collected {len(leads)} raw; deduping across sources…")
    before_dedupe = len(leads)
    leads = cross_source_dedupe(leads)
    run_pipeline.last_stats["duplicates_removed"] = before_dedupe - len(leads)
    log(f"  {len(leads)} after dedupe")
    # 1a. drop businesses already in the user's CRM (never hand back a dupe)
    if exclude_names:
        before = len(leads)
        leads = [r for r in leads if norm(r.get("name", "")) not in exclude_names]
        log(f"  removed {before - len(leads)} already in your CRM ({len(exclude_names)} names)")

    # 1b. chain handling — verticals that don't keep chains drop branded records
    if not vertical.keep_chains:
        before = len(leads)
        for r in leads:
            if r.get("brand"):
                r["excluded_reason"] = "Branded or chain business"
                r["chain_excluded"] = True
        leads = [r for r in leads if not r.get("chain_excluded")]
        log(f"  dropped {before - len(leads)} chain/branded records")

    if limit is not None and limit > 0 and len(leads) > limit:
        leads = leads[:limit]
        log(f"  applied global collection limit: {limit}")

    # 2. SUPPRESSION (competitor clients) — build once, applied during scoring.
    # Skipped in demo mode: it fetches competitor sites, and demo must stay offline.
    suppressed: dict[str, str] = {}
    if vertical.suppression_fn and not demo:
        log("Building competitor-suppression set…")
        suppressed = vertical.suppression_fn(cfg) or {}

    # 3. ENRICH (per-business website visit) — parallel, capped
    if enrich and vertical.enrich_fn and demo:
        # Offline enrichment: feed bundled fixture HTML through the same hooks.
        from .demo import demo_html_for
        ctx = {"config": cfg, "demo_html": demo_html_for}
        log(f"Enriching {len(leads)} sample businesses from bundled pages…")
        for r in leads:
            try:
                vertical.enrich_fn(r, ctx)
            except Exception as e:
                r["enrich_error"] = str(e)
    elif enrich and vertical.enrich_fn:
        candidates = [r for r in leads if not r.get("excluded_reason")]
        targets = candidates if enrich_cap is None else candidates[:enrich_cap]
        log(f"Enriching {len(targets)} businesses (parallel)…")
        enrich_stats = {}
        run_pipeline.last_stats["enrichment"] = enrich_stats
        ctx = {"config": cfg, "stats": enrich_stats}
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(vertical.enrich_fn, r, ctx): r for r in targets}
            done = 0
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    futs[f]["enrich_error"] = str(e)
                    enrich_stats["failures"] = enrich_stats.get("failures", 0) + 1
                    enrich_stats.setdefault("failure_reasons", []).append(type(e).__name__)
                done += 1
                if done % 25 == 0:
                    log(f"  enriched {done}/{len(targets)}")

    # 4. Normalize website status for honest lead qualification.
    from .audit import classify_website
    for r in leads:
        site = str(r.get("website") or "").strip()
        audit = r.get("audit") or {}
        status, evidence = classify_website(site, audit)
        r["website_status"] = status
        r["website_status_evidence"] = evidence
        r["website_status_confidence"] = "Automated evidence; manual review required"
        r["audit_notes"] = "; ".join(audit.get("audit_notes") or [])
        r["website_audit_url"] = site if audit else ""
        if r.get("geographic_match") == "OUTSIDE_RADIUS" or r.get("category_excluded"):
            r["qualification"] = "Excluded"

    # 5. SCORE (+ apply suppression flag) + opener
    for r in leads:
        key = norm(r.get("name", ""))
        if key in suppressed:
            r["suppressed"] = True
            r["suppressed_by"] = suppressed[key]
        s, tier, reasons = vertical.score(r)
        r["score"], r["tier"], r["why"] = s, tier, reasons
        from .scoring import contact_confidence_score, qualification_label
        r["website_opportunity_score"] = max(0, min(100, int(s)))
        r["contact_confidence_score"] = contact_confidence_score(r)
        r["qualification"] = qualification_label(
            r["website_opportunity_score"], r["contact_confidence_score"]
        )
        if r.get("excluded_reason"):
            r["qualification"] = "Excluded"
        if vertical.opener_fn:
            r["opener"] = vertical.opener_fn(r)
        from .export import quality_warnings
        r["quality_warnings"] = "; ".join(quality_warnings(r))
    leads.sort(key=lambda x: -x.get("score", 0))

    n = {"A": 0, "B": 0, "C": 0}
    for r in leads:
        n[r.get("tier", "C")] = n.get(r.get("tier", "C"), 0) + 1
    log(f"Scored {len(leads)} — A={n['A']} B={n['B']} C={n['C']}")

    # 6. EXPORT
    if out_stem:
        # Slugify only the filename; keep any directory the caller supplied intact.
        import os
        d, base = os.path.split(out_stem)
        stem = os.path.join(d, re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_"))
        run_pipeline.last_outputs = write_outputs(leads, vertical.columns, stem, log)

    return leads
