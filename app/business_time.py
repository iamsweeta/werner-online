"""Calendar date used by Russian carrier price-list generators.

Compare effective dates in Moscow time, independently of the host timezone.
Actual future editions remain rejected; this is not a one-day grace period.
"""
from datetime import datetime,timezone,timedelta
MOSCOW=timezone(timedelta(hours=3))

def tariff_today():return datetime.now(MOSCOW).date()
