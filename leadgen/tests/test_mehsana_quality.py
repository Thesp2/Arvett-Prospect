import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from leadgen.pipeline import _category_exclusion
from leadgen.quality import haversine_m, normalize_locality, valid_coordinates


def test_radius_boundary_cases():
    centre = (23.5880, 72.3693)
    inside = (23.5889, 72.3693)
    outside = (23.5891, 72.3693)
    radius = haversine_m(*centre, *inside)
    assert haversine_m(*centre, *centre) == 0
    assert haversine_m(*centre, *inside) <= radius
    assert haversine_m(*centre, *outside) > radius


def test_invalid_coordinates_are_not_matches():
    assert not valid_coordinates(None, 72.3)
    assert not valid_coordinates(200, 72.3)
    assert valid_coordinates(23.5, 72.3)


def test_mehsana_variant_keeps_other_city_names():
    assert normalize_locality("Mahesana") == "Mehsana"
    assert normalize_locality("Visnagar") == "Visnagar"


def test_category_exclusions_are_explainable():
    assert "Financial" in _category_exclusion({"category": "bank"})
    assert _category_exclusion({"category": "restaurant"}) == ""
