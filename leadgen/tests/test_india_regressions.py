import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from leadgen.audit import classify_website
from leadgen.quality import normalize_phone
from leadgen.sources import _overture_category_expression


def test_overture_schema_fallbacks():
    assert _overture_category_expression({"taxonomy.primary", "names"}) == "taxonomy.primary"
    assert _overture_category_expression({"taxonomy", "names"}) == "taxonomy.primary"
    assert _overture_category_expression({"basic_category", "names"}) == "basic_category"
    assert _overture_category_expression({"categories", "names"}) == "NULL"


def test_indian_phone_normalization():
    assert normalize_phone("+91 98765 43210", default_country="IN") == "+919876543210"
    assert normalize_phone("079-2345-6789", default_country="IN") == "+917923456789"
    assert normalize_phone("0000000000", default_country="IN") == ""


def test_website_classification_is_conservative():
    assert classify_website("")[0] == "NO_WEBSITE_FOUND"
    assert classify_website("https://facebook.com/example")[0] == "SOCIAL_ONLY"
    assert classify_website("https://example.test", {"error": "TIMEOUT"})[0] == "WEBSITE_BROKEN"
    assert classify_website(
        "https://example.test", {"reachable": True, "mobile_viewport": True, "audit_notes": []}
    )[0] == "FUNCTIONAL_WEBSITE"
