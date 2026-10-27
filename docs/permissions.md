# Roles and permissions

Access is role-based and scoped. A person holds a **role** in a **department**, optionally for an **academic session**, between a start and an optional end date. `apps.access.services.has_perm(user, code, department)` is the single check, used by:

- the web pages (`permission_required`, `DepartmentPermissionMixin`);
- the API (`HasDeptPermission`);
- every finance and communications service.

Hiding a button is never the only control.

The catalogue lives in `apps/access/catalog.py` and is installed by a migration. It matches the workbook's *Roles & Access* sheet; a test compares it with the seed data.

## Who can do what

The **member base** is these permissions:

- `profile.own.view`
- `dues.own.view`
- `finance.payment.initiate`
- `finance.receipt.view`
- `announcements.view`
- `events.register`
- `requests.create`

| Role | Permissions beyond the member base |
|---|---|
| Student / member | (member base only) |
| Financial Secretary | create dues and assessments, **record** offline payments, reconciliation view, roster view |
| Treasurer | **verify** payments, reconciliation, budgets, expenses, report export, roster view |
| Department Secretary | publish notices, committees, roster view and edit |
| President | publish notices, committees, events, **approve expenses (step 1)**, roster view, requests |
| HoD / Faculty Adviser | **approve waivers, refunds, budgets, expenses (step 2)**, report export, audit, roster view. No member base. |
| Auditor | read-only: audit, reconciliation, report export |
| Platform Administrator | technical admin (Django admin), roster edit, audit. **No finance permissions.** |
| Executive Officer | committees, events, roster view |

## Separation of duties, enforced in code

| Rule | Where |
|---|---|
| Whoever records a payment, and the payer, can't verify it | `finance.services.payments.verify_payment` |
| Whoever requests a waiver can't decide it; nobody decides a waiver on their own dues | `finance.services.dues.decide_waiver` |
| Refunds: the requester can't approve, and the approver can't process | `finance.services.adjustments` |
| Budgets: the proposer can't approve | `finance.services.spending.decide_budget` |
| Expense requests: the requester can't approve, and nobody approves both steps | `finance.services.spending.decide_request` |
| An expense is verified by someone other than its recorder | `finance.services.spending.verify_expense` |
| The platform admin's Django admin access never grants finance authority (finance actions go through services that check finance permissions) | `accounts.models.User.has_perm`, `finance.services.*` |
| Events: the organiser who created an event can't approve it | `events.services.decide_event` |
| Service requests: nobody handles their own request; restricted requests are visible only to their handler | `service_requests.services` |

## Operations modules

| Area | Members (`events.register`, `requests.create`) | Officers |
|---|---|---|
| Events | See published, completed and cancelled events; register, cancel their own registration, check in with the QR code | `events.manage`: create, approve (not their own), cancel, complete, mark attendance, export attendance |
| Service requests | Submit; see their own requests and requester-visible comments; reply, withdraw, confirm or reopen | `requests.manage`: the department queue, triage, assign, staff-only notes, resolve or decline. **Restricted** requests only for holders of the category's handling role (HoD / adviser). Never their own request. |
| Committees, meetings, tasks | See committees they sit on, their meetings and tasks, and tasks assigned to them; update their own tasks' status. Executives also see executive meetings. | `committees.manage`: everything in the department. A committee **chair** manages their own committee's members, meetings and tasks. |
| Documents | Approved department documents and approved documents of their committees | Upload: `committees.manage`, `announcements.publish`, finance roles, committee chairs (for their committee). Review: `committees.manage`, not your own upload. **Financial evidence** only for finance roles; **only-me** documents only for the uploader. |

Records belonging to another department always return 404, including through the API and check-in links.

## Assigning roles

Platform administrators assign roles in the Django admin (`/django-admin/` → Role assignments). Each assignment is validated: a department role needs a department, and a platform role must have no department. Only one active grant of the same role and scope can exist. At handover, end the outgoing executive's assignment (status *ended*, with an end date) and create the new one. History is kept.
