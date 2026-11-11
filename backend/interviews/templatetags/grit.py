"""Template helpers: IST date formatting, relative time, state pills, icons."""
from datetime import datetime

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ..utils import DAY, IST, day_start, fmt_date, fmt_time, ICONS, OUTCOME_PILL, STATE_META

register = template.Library()


def _dt(v):
    return v if isinstance(v, datetime) else None


@register.filter
def ist_date(v):
    return fmt_date(v) if _dt(v) else ""


@register.filter
def ist_time(v):
    return fmt_time(v) if _dt(v) else ""


@register.filter
def ist_long(v):
    return v.astimezone(IST).strftime("%A, %d %B").replace(" 0", " ") if _dt(v) else ""


@register.filter
def ist_day(v):
    return v.astimezone(IST).day if _dt(v) else ""


@register.filter
def ist_dow(v):
    return v.astimezone(IST).strftime("%a") if _dt(v) else ""


@register.filter
def short_date(v):
    """'31 Dec' (no weekday)."""
    return fmt_date(v).split(", ", 1)[1] if _dt(v) else ""


@register.simple_tag(takes_context=True)
def day_label(context, v):
    now = context.get("now")
    if not _dt(v) or not now:
        return ""
    diff = round((day_start(v) - day_start(now)) / DAY)
    return {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(diff, fmt_date(v))


@register.simple_tag(takes_context=True)
def rel(context, v):
    """'in 2h', '3 days ago'."""
    now = context.get("now")
    if not _dt(v) or not now:
        return ""
    secs = (v - now).total_seconds()
    a = abs(secs)
    if a < 60:
        return "now" if secs >= 0 else "just now"
    if a < 3600:
        s = f"{round(a / 60)} min"
    elif a < 86400:
        s = f"{round(a / 3600)}h"
    else:
        d = round(a / 86400)
        s = f"{d} day{'s' if d != 1 else ''}"
    return f"in {s}" if secs >= 0 else f"{s} ago"


@register.simple_tag
def icon(name, size=16, cls=""):
    return format_html(
        '<svg class="ic {}" width="{}" height="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>', cls, size, size, mark_safe(ICONS.get(name, "")))


@register.simple_tag
def pill(state, audience="team"):
    m = STATE_META.get(state)
    if not m:
        return ""
    return format_html('<span class="pill tone-{}">{}</span>', m["tone"], m["student"] if audience == "student" else m["label"])


@register.filter
def state_label(state):
    return STATE_META.get(state, {}).get("label", state)


@register.filter
def state_student(state):
    return STATE_META.get(state, {}).get("student", state)


@register.filter
def state_tone(state):
    return STATE_META.get(state, {}).get("tone", "muted")


@register.simple_tag
def outcome_pill(interview):
    outcome = getattr(interview, "outcome", None)
    tone, label = OUTCOME_PILL.get(outcome.result, ("muted", outcome.result)) if outcome else ("muted", "Cancelled")
    return format_html('<span class="pill tone-{}">{}</span>', tone, label)


@register.filter
def initials(name):
    return "".join(w[0] for w in str(name).split()[:2]).upper()


@register.filter
def get(d, key):
    try:
        return d.get(key) if hasattr(d, "get") else d[key]
    except (KeyError, IndexError, TypeError):
        return None


@register.filter
def pct(value, total):
    try:
        return min(100, round(float(value) / float(total) * 100)) if float(total) else 0
    except (TypeError, ValueError):
        return 0


@register.filter
def ms(v):
    return int(v.timestamp() * 1000) if _dt(v) else ""


@register.filter
def times(n):
    try:
        return range(int(n))
    except (TypeError, ValueError):
        return range(0)


@register.simple_tag(takes_context=True)
def days_until(context, v):
    now = context.get("now")
    if not _dt(v) or not now:
        return 0
    return max(0, -(-(v - now) // DAY))


@register.filter
def num(v):
    """1350 -> 1,350"""
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return v
