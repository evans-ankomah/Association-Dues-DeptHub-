from decimal import Decimal, InvalidOperation

from django import template
from django.utils.html import format_html

register = template.Library()

BADGE_STYLES = {
    # assessment derived statuses
    "paid": "bg-brand-100 text-brand-800",
    "partial": "bg-sky-100 text-sky-800",
    "outstanding": "bg-amber-100 text-amber-800",
    "overdue": "bg-red-100 text-red-800",
    "waived": "bg-violet-100 text-violet-800",
    "cancelled": "bg-slate-200 text-slate-600",
    # payment / request statuses
    "confirmed": "bg-brand-100 text-brand-800",
    "pending": "bg-amber-100 text-amber-800",
    "failed": "bg-red-100 text-red-800",
    "refunded": "bg-violet-100 text-violet-800",
    "partially_refunded": "bg-violet-100 text-violet-800",
    "approved": "bg-brand-100 text-brand-800",
    "rejected": "bg-red-100 text-red-800",
    "processed": "bg-brand-100 text-brand-800",
    "requested": "bg-amber-100 text-amber-800",
    "pending_approval": "bg-amber-100 text-amber-800",
    "issued": "bg-brand-100 text-brand-800",
    "voided": "bg-slate-200 text-slate-600",
    "active": "bg-brand-100 text-brand-800",
    "draft": "bg-slate-100 text-slate-700",
    "closed": "bg-slate-200 text-slate-600",
    "important": "bg-amber-100 text-amber-800",
    "urgent": "bg-red-100 text-red-800",
    "scheduled": "bg-sky-100 text-sky-800",
    "published": "bg-brand-100 text-brand-800",
    "archived": "bg-slate-200 text-slate-600",
    "fulfilled": "bg-brand-100 text-brand-800",
    "pending_payment": "bg-amber-100 text-amber-800",
    "submitted": "bg-sky-100 text-sky-800",
    "withdrawn": "bg-slate-200 text-slate-600",
    "recorded": "bg-slate-100 text-slate-700",
    "returned": "bg-amber-100 text-amber-800",
    # events
    "completed": "bg-slate-200 text-slate-700",
    "registered": "bg-brand-100 text-brand-800",
    "waitlisted": "bg-amber-100 text-amber-800",
    "attended": "bg-brand-100 text-brand-800",
    "no_show": "bg-slate-200 text-slate-600",
    "present": "bg-brand-100 text-brand-800",
    "late": "bg-amber-100 text-amber-800",
    "absent": "bg-red-100 text-red-800",
    "excused": "bg-slate-100 text-slate-700",
    # service requests
    "triaged": "bg-sky-100 text-sky-800",
    "in_progress": "bg-sky-100 text-sky-800",
    "waiting": "bg-amber-100 text-amber-800",
    "resolved": "bg-brand-100 text-brand-800",
    "high": "bg-amber-100 text-amber-800",
    # workspace
    "todo": "bg-slate-100 text-slate-700",
    "blocked": "bg-red-100 text-red-800",
    "done": "bg-brand-100 text-brand-800",
    "held": "bg-brand-100 text-brand-800",
    "inactive": "bg-slate-200 text-slate-600",
    "ended": "bg-slate-200 text-slate-600",
    # documents
    "pending_review": "bg-amber-100 text-amber-800",
    # resources
    "available": "bg-brand-100 text-brand-800",
    "reserved": "bg-sky-100 text-sky-800",
    "maintenance": "bg-amber-100 text-amber-800",
    "retired": "bg-slate-200 text-slate-600",
    "checked_out": "bg-sky-100 text-sky-800",
    "excellent": "bg-brand-100 text-brand-800",
    "good": "bg-brand-100 text-brand-800",
    "fair": "bg-amber-100 text-amber-800",
    "needs_repair": "bg-red-100 text-red-800",
}

LABELS = {
    "partial": "Part paid",
    "partially_refunded": "Part refunded",
    "pending_approval": "Awaiting approval",
    "no_show": "No show",
}


@register.filter
def money(value, currency="GHS"):
    """Format as 'GHS 1,234.50'. Never goes through float."""
    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(value).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return value
    sign = "−" if amount < 0 else ""
    return f"{sign}{currency} {abs(amount):,.2f}"


@register.simple_tag
def status_badge(status, label=None):
    style = BADGE_STYLES.get(status, "bg-slate-100 text-slate-700")
    text = label or LABELS.get(status) or str(status).replace("_", " ").capitalize()
    return format_html('<span class="badge {}">{}</span>', style, text)


@register.filter
def percent(value):
    if value is None:
        return "—"
    return f"{Decimal(value):.1f}%"
