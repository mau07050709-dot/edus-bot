"""Cálculo de la próxima ventana de preparación, en hora de Costa Rica."""

from datetime import datetime, time, timedelta


def preparation_window(now, preparation, opening, scheduled):
    opens = datetime.combine(now.date(), time.fromisoformat(opening), now.tzinfo)
    if opens <= now:
        opens += timedelta(days=1)
    starts = datetime.combine(opens.date(), time.fromisoformat(preparation), now.tzinfo)
    if starts > opens:
        starts -= timedelta(days=1)
    return (max(starts, now) if scheduled else now), opens
