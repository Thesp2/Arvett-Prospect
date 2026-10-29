"""
Vertical: web-design leads — the shipped worked example.

Target: local businesses with no website, a social-only presence, or a weak/slow
DIY site — i.e. good prospects for a web designer. This is the reference vertical:
read it alongside docs/ADD_A_VERTICAL.md to build your own.

It shows the full pattern: an enrich_fn that audits each site, a score_fn that
turns those signals into a tier, and an opener_fn that drafts a pitch angle. The
enrich_fn is written to also work offline (demo mode) by reading bundled HTML
from ctx, so `--demo` / the GUI "Try a demo" button need no network.
"""
from __future__ import annotations

from .. import register, Vertical
from ..audit import is_weak_url, DIY_BUILDERS
from ._common import audit_enrich, BROAD_OSM_TAGS

CONFIG = {"diy_builders": DIY_BUILDERS}


def _score(rec: dict) -> tuple[int, str, str]:
    score, reasons = 0, []
    site = (rec.get("website") or "").strip()
    weak, why = is_weak_url(site)
    audit = rec.get("audit") or {}

    # Missing source URL is a prospecting signal, not proof the business has no site.
    status = rec.get("website_status", "")
    if status == "VERIFIED_NO_WEBSITE":
        score += 55
        reasons.append("verified lack of independent website")
        tier = "A"
    elif status == "NO_WEBSITE_FOUND" or not site:
        score += 60 if (not status and rec.get("phone")) else (35 if not status else 55)
        reasons.append("NO WEBSITE found in source (unverified)")
        tier = "A" if rec.get("phone") else "B"
    elif status == "SOCIAL_ONLY" or weak:
        score += 60 if not status else 55
        reasons.append(
            f"non-site link ({why})" if not status else f"social/directory-only URL ({why})"
        )
        tier = "A" if (not status or rec.get("phone")) else "B"
    elif status in {"WEBSITE_BROKEN", "OUTDATED_WEBSITE"}:
        score += 55 if status == "WEBSITE_BROKEN" else 25
        reasons.append(
            "website unreachable — recheck before contacting"
            if status == "WEBSITE_BROKEN"
            else "outdated website"
        )
        tier = "A" if status == "WEBSITE_BROKEN" and rec.get("phone") else "B"
    elif audit.get("reachable") is False:
        score += 55 if rec.get("phone") else 20
        reasons.append("website unreachable on one check — recheck before pitching")
        tier = "A" if rec.get("phone") else "C"
    else:
        tier = "C"
        if not audit:
            reasons.append("website URL listed; quality not checked")
        else:
            if not audit.get("https"):
                score += 15; reasons.append("no HTTPS")
            if not audit.get("mobile_viewport"):
                score += 15; reasons.append("mobile usability concern")
            if (audit.get("load_ms") or 0) > 4000:
                score += 10; reasons.append(f"slow ({audit['load_ms']}ms)")
            if audit.get("builder") in CONFIG["diy_builders"]:
                score += 8; reasons.append(f"DIY builder ({audit['builder']})")
            if score >= 25:
                tier = "B"
            if not reasons:
                reasons.append("no obvious issues detected; manual review recommended")
    if rec.get("phone") or rec.get("email"):
        score += 5
        reasons.append("public business contact listed")
    return min(100, score), tier, "; ".join(reasons)


def _opener(rec: dict) -> str:
    site = (rec.get("website") or "").lower()
    if not site:
        return "No website URL in the source; verify independently before pitching a new site."
    if any(host in site for host in ("facebook.com", "instagram.com", "linktr.ee", "tiktok.com")):
        return "Social-only listing; check whether the business wants an independent website."
    if site.startswith("http://"):
        return "HTTP URL detected; verify current site and HTTPS before proposing an upgrade."
    if (rec.get("audit") or {}).get("reachable") is False:
        return "Website did not respond on one check; recheck later before contacting."
    return "Review the site manually for specific improvements before contacting."


COLUMNS = [
    ("Tier", "tier"), ("Opportunity score", "score"),
    ("Website opportunity score", "website_opportunity_score"),
    ("Contact confidence score", "contact_confidence_score"),
    ("Qualification", "qualification"),
    ("Business Name", "name"), ("Business Category", "category"),
    ("Category exclusion", "category_excluded"),
    ("Exclusion reason", "excluded_reason"),
    ("Category filter", "category_group"), ("City / Locality", "city"),
    ("Normalized city", "city_normalized"), ("Country", "country"),
    ("State from source", "state"), ("Target market", "target_market"),
    ("Location confidence", "location_confidence"), ("Geographic match", "geographic_match"),
    ("Distance km", "distance_km"), ("Business Address", "address"),
    ("Business Phone", "phone"), ("Original phone", "original_phone"),
    ("Normalized phone", "normalized_phone"),
    ("Phone source", "phone_source"), ("Business Email", "business_email"),
    ("Contact confidence", "contact_confidence"),
    ("Owner / Decision-Maker", "owner_name"), ("Decision-Maker Role", "owner_role"),
    ("Decision-maker source", "owner_source"), ("Employee estimate", "employee_estimate"),
    ("Company size", "company_size"),
    ("Company size evidence", "company_size_source"), ("Website status", "website_status"),
    ("Website Evidence", "website_status_evidence"),
    ("Website status confidence", "website_status_confidence"),
    ("Website URL", "website"), ("Website Quality Notes", "audit_notes"),
    ("Suggested Outreach Opener", "opener"), ("Qualification Reason", "why"),
    ("Data source", "source"), ("Source ID", "source_id"),
    ("Source record URL", "source_url"), ("Source references", "source_references"),
    ("Contact source URL", "contact_source"), ("Website audit URL", "website_audit_url"),
    ("Last checked", "checked_at"),
]

register(Vertical(
    key="web_design",
    label="Website opportunities: no website, social-only, or weak/outdated site",
    description=("Finds local businesses with no website, a social-only page, or "
                 "a weak/outdated site — good prospects for web-design work."),
    overture_categories=[],          # all categories; broad by design
    osm_tags=BROAD_OSM_TAGS,
    keep_chains=False,               # chains don't buy from local web designers
    score_fn=_score,
    enrich_fn=audit_enrich,
    opener_fn=_opener,
    config=CONFIG,
    columns=COLUMNS,
))
