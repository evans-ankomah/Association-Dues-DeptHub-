# Architecture

DeptHub is a **modular monolith**: one Django project, one PostgreSQL database, and clear module boundaries. There are no microservices; background work runs on Celery with Redis.

```
Browser ──HTTP──> Django (gunicorn)
                    ├── middleware: request id, department context, sessions, CSRF
                    ├── views (HTML + HTMX)  ─┐
                    ├── /api/v1 (DRF)        ─┼─> services (business rules, permissions, locking, audit)
                    └── webhooks (Paystack)  ─┘            │
                                                           ▼
                                                     PostgreSQL ── v_dues_balances (view)
                    Celery worker + beat <── Redis ──> notifications, reminders, checkout clean-up
```

## Modules (`apps/`)

| App | Owns |
|---|---|
| `common` | Base model (UUID key, timestamps), money fields, navigation, seed loader, health check |
| `institutions` | Institutions, faculties, departments, academic sessions |
| `accounts` | Login identity (`User`), profiles, sign-in, password reset, login throttling |
| `memberships` | Department membership per session, member roster |
| `access` | Roles, permissions, scoped role assignments, `has_perm`, department/session context |
| `audit` | Append-only audit events, request ids |
| `finance` | Dues types, assessments, waivers, payments, allocations, receipts, Paystack, refunds, reconciliation, budgets, expenses, reminders |
| `communications` | Notices, notifications (in-app, email, SMS) |
| `events` | Events, approval, registration with capacity and waitlist, QR and manual attendance |
| `service_requests` | Member requests, triage and assignment, comments (requester-visible or staff-only), restricted categories |
| `workspace` | Committees and their members, meetings with minutes, tasks linked to committees, events and meetings, handover notes |
| `documents` | Document metadata, private file storage, versions, review, visibility and audited downloads |
| `resources` | Assets (equipment, facilities, venues), condition and availability, bookings with overlap protection |
| `reporting` | Dashboards, finance report, exports |
| `api` | REST API v1 and OpenAPI schema |

Views and API endpoints are thin. Business rules live in `apps/*/services*.py`, and those services:

- re-check permissions, so they're safe to call from anywhere;
- run in a database transaction and lock the rows they change (`select_for_update`);
- write an audit event.

## Money rules

- All amounts are `numeric(12,2)` with an explicit currency (GHS). Floats are never used.
- **Balances are calculated, never stored.** `balance = assessed − waived − allocations on received payments + amounts reversed by processed adjustments`. The same rule exists as the ORM annotation `finance.services.balances.with_balances` and as the SQL view `v_dues_balances`. Tests check both against the seed validator.
- A payment counts as income only once it's `confirmed` (or later refunded). Online payments are confirmed only from a signed Paystack webhook or a server-side verify call, and only when status, reference, currency and amount all match.
- Completed records are never edited or deleted. Corrections go through refunds, reversals or voids, each requested, approved and processed by different people.
- Only `paid` expenses count as cash outflow.

## Department and session context

`DepartmentContextMiddleware` puts `request.department` and `request.academic_session` on every request. The department is always chosen from the user's own role assignments; a value in the session isn't trusted on its own. Every query is filtered by `request.department`, so another department's record returns 404.

## Events

- An organiser (`events.manage`) drafts an event and sends it for approval; a **different** `events.manage` holder approves it. Members only ever see published, completed and cancelled events; drafts are a 404 to them.
- Registration locks the event row (`select_for_update`) before counting places, so two members can't take the last place at once. Once full, registrations go on a waitlist and the earliest waitlisted member is promoted (and notified) when a place frees up.
- Check-in: the organiser shows a QR code that points to a signed check-in URL. Members scan it and confirm while signed in, from one hour before the start until the end; arriving more than 15 minutes late is recorded as *late*. Organisers can also mark attendance by hand. Every attendance change is audited.
- Attendance rate (workbook `v_event_attendance_summary`): registered members who attended ÷ registrations holding a place. Cancelled and waitlisted registrations are not expected attendees; events without registration have no rate.

## Service requests

- Members submit a request of a listed type; each type has a target resolution time that sets `due_at`. Members can't mark their own request urgent.
- **Restricted categories** (currently results/academic-record queries) never enter the general queue. They are assigned on submission to a holder of the category's handling role (HoD / faculty adviser), and only the requester, the assignee and current holders of that role can see them. If nobody holds the role, submission is refused rather than leaving the request unseen.
- **Staff-only comments** are never shown or sent to the requester, including when the requester is themselves an officer. Nobody can handle their own request.
- Audit events record status, priority, assignee and visibility changes, never the description, resolution or comment text. Notifications for restricted requests omit the title.
- Welfare and disciplinary cases are deliberately not offered as request types (product plan §3.5, §12).
- SLA summary (workbook `v_service_request_sla_summary`): resolution time is calendar time from submission to resolution, without pausing while waiting on the requester; withdrawn requests are excluded; on-time = resolved by `due_at`.

## Committees, meetings and tasks

- Committees belong to one academic session. Ending a seat keeps it as history (only one *active* seat per member, enforced by a partial unique index); archiving a committee at the end of a session ends every seat. Handover notes carry knowledge to the next session's committee.
- A committee's chair manages its members, meetings and tasks without needing `committees.manage`, but only for that committee: they can't move a meeting or task into or out of it.
- Visibility: managers see everything in the department; members see committees they sit on, those committees' meetings and tasks, and tasks assigned to them. Executive meetings (no committee) are visible to department executives only.
- The assignee moves a task through to-do, in progress, blocked and done; cancelling, reassigning and re-dating are for managers. A done task always has a completion time (database check).

## Documents

- Files are stored in `STORAGES["documents"]`: a `FileSystemStorage` under `PRIVATE_MEDIA_ROOT` (default `private_media/`) with no public URL. It is outside `MEDIA_ROOT` and the static folders, and nothing serves it. Every download goes through `/documents/<id>/download/`, which re-checks access, forces `Content-Disposition: attachment` with `nosniff`, and writes a `document.downloaded` audit event. For production, point `STORAGES["documents"]` at a **private** object-storage bucket.
- Uploads are limited to PDF, Office (docx/xlsx/pptx), CSV, text, PNG and JPEG, up to `DOCUMENT_MAX_UPLOAD_BYTES` (10 MB). The contents are checked against the extension (magic bytes / OOXML structure), so a renamed HTML or executable is rejected. Size and SHA-256 checksum are recorded. Storage keys are random.
- Versions chain through `previous_version_document`. Approving a new version archives the previously approved one.
- Who sees what:
  - *Only me*: the uploader only, not even stewards.
  - *Financial evidence* (category `financial`): finance officers (anyone with a finance role, plus the President and HoD as expense approvers) and the uploader, whatever visibility was chosen.
  - *Committee*: that committee's current members and stewards.
  - *Department*: everyone in the department once **approved** by a steward (`committees.manage`) other than the uploader. Drafts and pending uploads are visible to the uploader and stewards only.
- `collaboration` and `public` visibility exist in the schema but aren't offered until Phase 4.
- Meeting minutes and expense evidence link to documents through `meetings.minutes_document_id` and `*.supporting_document_id`. Linking re-checks that the user can see the document.

## Assets and bookings

- Custodians (`assets.manage`) keep the inventory. Asset tags are unique per department.
- Anyone with a role in the department can request a booking. A custodian other than the requester approves, rejects, checks out and records the return.
- **Overlapping bookings are blocked twice.** The service locks the asset row and refuses a booking that overlaps an approved or checked-out one, with a clear message. A PostgreSQL exclusion constraint (`ex_booking_no_overlap`, `btree_gist`, half-open `[start, end)`) makes such overlaps impossible even under concurrent approvals or direct writes. Back-to-back bookings are allowed.
- Overlapping *requests* may coexist. Approving one automatically rejects the others for that slot and tells their requesters.
- Assets under maintenance or retired can't be booked. Taking an asset out of service needs upcoming approved bookings cleared first. Returning an asset as *needs repair* puts it into maintenance.
- Members see when an asset is busy, but not who booked it or why.
- Utilisation: booked hours (approved, checked out, returned) clipped to the reporting window ÷ hours in the window.

## Background jobs

| Job | Schedule | What it does |
|---|---|---|
| `finance.tasks.send_dues_reminders` | daily 09:00 | due-soon / overdue / final reminders, once per member and stage |
| `finance.tasks.resolve_abandoned_checkouts` | hourly | asks Paystack about old pending checkouts, cancels the rest |
| `communications.tasks.deliver_notification` | on demand | sends one email or SMS notification |
| `communications.tasks.fan_out_announcement` | on demand | notifies the audience of an important or urgent notice |

No job sweeps the notification queue. Only notifications the app creates are delivered, so imported rows are never sent.

## Workbook alignment

Table and column names follow `.product/DeptHub_Database_Design.xlsx`, so `load_umat_seed` loads the sample data 1:1. A few additions outside the workbook are marked in the models:

- `waiver_requests`
- `receipt_counters`
- `announcement_acknowledgements`
- notification `link` and `dedupe_key`
- profile notification preferences
- `payments.intended_allocations`
