"""Customer comparison units. An effective quotient is never a published rate."""
import math


def positive(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def is_rate_profile(profile):
    return not profile.get('is_minimum_profile') and float(profile.get('weight_kg', 0)) >= 100


def tariff_value(item, profile):
    if item.get('price_is_minimum'):
        return None
    value = item.get('published_rate_per_kg') if is_rate_profile(profile) else item.get('comparison_value')
    return float(value) if positive(value) else None


def tariff_unit(profile):
    return '₽/кг' if is_rate_profile(profile) else '₽'


def price_signature(item):
    """Equal totals may conceal different published rates under a minimum."""
    return tuple(item.get(k) for k in ('kind', 'price', 'rate_per_kg', 'minimum', 'tariff_kind'))
