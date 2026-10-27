"""Sidebar navigation. Each item is shown only when the user holds one of its permissions
in the current department (None means any signed-in user)."""

from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse


@dataclass(frozen=True)
class NavItem:
    label: str
    url_name: str
    permissions: tuple[str, ...] | None = None
    platform_admin_only: bool = False
    # Key into VISIBILITY_CHECKS, for items that depend on data rather than permissions (e.g.
    # sitting on a committee). The item shows when the permissions OR this check allow it.
    visible_if: str | None = None


def _workspace_visible(user, department):
    from apps.workspace.services import can_see_workspace

    return can_see_workspace(user, department)


VISIBILITY_CHECKS = {"workspace": _workspace_visible}


FINANCE_VIEWERS = (
    "dues.assessment.create",
    "dues.waiver.approve",
    "finance.payment.record",
    "finance.payment.verify",
    "finance.reconciliation.view",
    "finance.report.export",
)

SPENDING_VIEWERS = (
    "finance.budget.create",
    "finance.budget.approve",
    "finance.expense.create",
    "finance.expense.approve",
    "finance.report.export",
    "finance.reconciliation.view",
)

NAV_SECTIONS: list[tuple[str, list[NavItem]]] = [
    (
        "",
        [
            NavItem("Home", "home"),
            NavItem("My dues", "finance:my_dues", ("dues.own.view",)),
            NavItem("My payments", "finance:my_payments", ("dues.own.view", "finance.receipt.view")),
            NavItem(
                "Notices", "communications:announcements", ("announcements.view", "announcements.publish")
            ),
        ],
    ),
    (
        "Finance",
        [
            NavItem("Dues", "finance:dues_types", FINANCE_VIEWERS),
            NavItem("Payments", "finance:payments", FINANCE_VIEWERS),
            NavItem("Waivers", "finance:waivers", ("dues.waiver.approve", "dues.assessment.create")),
            NavItem(
                "Refunds",
                "finance:adjustments",
                ("finance.adjustment.approve", "finance.payment.verify", "finance.payment.record"),
            ),
            NavItem("Reconciliation", "finance:reconciliation", ("finance.reconciliation.view",)),
            NavItem("Budgets", "finance:budgets", SPENDING_VIEWERS),
            NavItem("Expenses", "finance:expense_requests", SPENDING_VIEWERS),
            NavItem("Reports", "reporting:finance", FINANCE_VIEWERS),
        ],
    ),
    ("Activities", [NavItem("Events", "events:list", ("events.register", "events.manage"))]),
    (
        "Services",
        [
            NavItem("My requests", "service_requests:mine", ("requests.create",)),
            NavItem("Request queue", "service_requests:queue", ("requests.manage",)),
        ],
    ),
    (
        "Workspace",
        [
            NavItem("Tasks", "workspace:tasks", ("committees.manage",), visible_if="workspace"),
            NavItem("Committees", "workspace:committees", ("committees.manage",), visible_if="workspace"),
            NavItem("Meetings", "workspace:meetings", ("committees.manage",), visible_if="workspace"),
            NavItem("Documents", "documents:list"),
        ],
    ),
    ("Members", [NavItem("Roster", "members:roster", ("members.roster.view", "members.roster.edit"))]),
    ("Account", [NavItem("Profile", "accounts:profile")]),
    ("Administration", [NavItem("Platform admin", "admin:index", platform_admin_only=True)]),
]


def build_nav(request, perms):
    sections = []
    path = request.path
    checked = {}
    for title, items in NAV_SECTIONS:
        visible = []
        for item in items:
            if item.platform_admin_only and not request.user.is_staff:
                continue
            if item.permissions is not None and not perms.intersection(item.permissions):
                if item.visible_if is None:
                    continue
                if item.visible_if not in checked:
                    department = getattr(request, "department", None)
                    checked[item.visible_if] = department is not None and VISIBILITY_CHECKS[item.visible_if](
                        request.user, department
                    )
                if not checked[item.visible_if]:
                    continue
            try:
                url = reverse(item.url_name)
            except NoReverseMatch:
                continue
            active = path == url if url == "/" else path.startswith(url)
            visible.append({"label": item.label, "url": url, "active": active})
        if visible:
            sections.append({"title": title, "items": visible})
    return sections
