"""
Shared hooks reused across verticals, so each vertical file stays about its
*scoring*, not its plumbing.
"""
from __future__ import annotations

import re
from ..audit import audit_website, audit_from_html, classify_website, is_weak_url
from ..enrich import fetch_pages, find_decision_maker

# Social platforms that count as "no real website of your own".
SOCIAL_HOSTS = ("facebook.com", "fb.com", "instagram.com", "linktr.ee",
                "linktree.com", "tiktok.com", "twitter.com", "x.com",
                "nextdoor.com", "youtube.com")

# Broad OSM coverage for "any local business" verticals. Key-only entries
# ("shop", "craft", "office") match ANY value of that key (every shop/trade/office
# with a name); the amenity/tourism/leisure entries add the business-y POIs that
# don't live under those keys. Far wider reach than a short hardcoded list.
BROAD_OSM_TAGS = [
    "shop", "craft", "office",
    "amenity=restaurant", "amenity=cafe", "amenity=bar", "amenity=pub",
    "amenity=fast_food", "amenity=fuel", "amenity=pharmacy", "amenity=clinic",
    "amenity=dentist", "amenity=doctors", "amenity=veterinary", "amenity=bank",
    "amenity=car_wash", "amenity=car_rental", "amenity=driving_school",
    "tourism=hotel", "tourism=guest_house", "tourism=motel",
    "leisure=fitness_centre",
]


def audit_enrich(rec: dict, ctx: dict) -> dict:
    """Audit a business's website (HTTPS / mobile / speed / DIY builder).

    Demo-aware: if ctx provides bundled HTML (offline demo / GUI "Try a sample"),
    score that instead of fetching, so the same hook works with no network.
    """
    site = rec.get("website") or ""
    stats = (ctx or {}).get("stats") or {}
    stats["selected"] = stats.get("selected", 0) + 1
    if not site or is_weak_url(site)[0]:
        rec["website_status"], rec["website_status_evidence"] = classify_website(site)
        if not site:
            stats["no_website_url"] = stats.get("no_website_url", 0) + 1
        return rec  # nothing real to audit — the scorer handles the gap
    demo_html = (ctx or {}).get("demo_html")
    if demo_html is not None:
        html = demo_html(site)
        rec["audit"] = audit_from_html(html, site, reachable=bool(html))
        stats["websites_visited"] = stats.get("websites_visited", 0) + 1
        if html:
            stats["websites_retrieved"] = stats.get("websites_retrieved", 0) + 1
    else:
        rec["audit"] = audit_website(site)
        stats["websites_visited"] = stats.get("websites_visited", 0) + 1
        if rec["audit"].get("reachable"):
            stats["websites_retrieved"] = stats.get("websites_retrieved", 0) + 1
        rec["website_status"], rec["website_status_evidence"] = classify_website(
            site, rec["audit"]
        )
        # Best-effort extraction of business-published contact details from its own site.
        # This does not guess personal contact details or search private databases.
        try:
            pages = fetch_pages(site, ["/contact", "/contact-us", "/about", "/about-us", "/team"], cap=3)
            html_blob = "\n".join(pages.values())
            if not rec.get("email"):
                emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", html_blob, re.I)
                emails = [e for e in emails if not any(x in e.lower() for x in ("example.com", "yourdomain", "wixpress"))]
                if emails:
                    rec["email"] = emails[0]
                    rec["business_email"] = emails[0]
                    rec.setdefault("field_sources", {})["email"] = site
                    rec["contact_source"] = site
                    stats["emails_found"] = stats.get("emails_found", 0) + 1
                    rec["contact_confidence"] = "Published on official website"
                    rec["contact_source"] = site
            if not rec.get("phone"):
                phones = re.findall(r'(?:tel:)?(\+?[0-9][0-9() .-]{7,}[0-9])', html_blob, re.I)
                phones = [re.sub(r"\s+", " ", p).strip(" .-") for p in phones]
                if phones:
                    rec["phone"] = phones[0]
                    rec.setdefault("original_phone", phones[0])
                    rec.setdefault("field_sources", {})["phone"] = site
                    rec["contact_confidence"] = "Published on official website"
                    rec["contact_source"] = site
                    stats["phones_found"] = stats.get("phones_found", 0) + 1
            if not rec.get("owner_name"):
                person, role = find_decision_maker(pages, [
                    "Founder", "Owner", "Proprietor", "Managing Director",
                    "CEO", "Chief Executive Officer", "Director", "Partner"
                ])
                if person:
                    rec["owner_name"], rec["owner_role"] = person, role
                    rec["owner_source"] = site
                    rec.setdefault("field_sources", {})["owner_name"] = site
                    stats["decision_makers_found"] = stats.get("decision_makers_found", 0) + 1
        except (ValueError, TypeError, IndexError) as exc:
            rec["enrich_error"] = type(exc).__name__
            stats["failures"] = stats.get("failures", 0) + 1
            rec["enrich_failure_reason"] = type(exc).__name__
    return rec


def is_social(url: str) -> bool:
    """True if the URL is a social-media page rather than an owned website."""
    from ..audit import hostname
    host = hostname(url)
    return any(host == d or host.endswith("." + d) for d in SOCIAL_HOSTS)
