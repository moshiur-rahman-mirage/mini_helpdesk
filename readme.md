# Mini Helpdesk (Odoo 19) — Implementation Guide

A small ticketing system with **SLA tracking, auto-assignment, escalation, email-to-ticket, a customer portal, and reports**. Written so you can build it phase by phase, copy the code, and understand *why* each piece exists.

| Item | Value |
| --- | --- |
| Technical name | `mini_helpdesk` |
| Odoo version | 19.0 (Community) |
| Depends on | `base`, `mail`, `portal` (no `hr`, no `website`) |
| Estimated effort | 8–10 days at a relaxed pace |
| Goal | Learn how Odoo works internally. **Do not turn it into a full helpdesk product.** |

> **Version note.** This guide uses Odoo 19 syntax. If you ever port it to 17/18:
> `<chatter/>` → `<div class="oe_chatter">…</div>`; kanban `t-name="card"` → `t-name="kanban-box"` (17); `models.Constraint(...)` → `_sql_constraints = [...]`; `_read_group` API is the same from 17 on.
> If a line errors on your exact 19.x build, compare with the same feature in `odoo/addons/project` — that module is the best reference for almost everything here.

---

## 1. Learning map

### 1.1 What each feature teaches

| Odoo concept | Where it appears in this project |
| --- | --- |
| Models, fields | Ticket, Team, Stage, Tag, SLA Policy |
| Many2one / One2many / Many2many | Ticket→Team, Team→Tickets, Ticket↔Tags, Team↔Members |
| Computed + stored fields, `@api.depends` | SLA policy, deadlines, SLA status, partner email |
| Constraints (Python + SQL) | Resolution note required, unique names, positive SLA hours |
| CRUD overrides (`create`, `write`) | Numbering, auto-assign, closed date |
| Business actions (buttons) | Assign to me, Resolve, Reopen, Escalate |
| `ir.sequence` | `HD-2026-00001` |
| `mail.thread` + `mail.activity.mixin` | Chatter, tracking, To-Do activities |
| Security groups, ACL, record rules | Agent / Manager / Portal customer |
| Views | List, Form, Kanban (drag & drop stages), Search, Pivot, Graph |
| `ir.cron` | Escalation + unassigned reminder |
| Mail gateway (`message_new`) | Email creates a ticket |
| Mail templates | "We received your request" |
| Portal controller + QWeb | Customer ticket list, detail, new ticket form |
| QWeb PDF report | Ticket summary PDF |
| Tests (`TransactionCase`) | Logic regression tests |

### 1.2 Spring Boot analogies (and where they break)

| Odoo | Closest Spring Boot idea | Where the analogy breaks |
| --- | --- | --- |
| `models.Model` class | JPA `@Entity` + repository + service in one class | Active-record style: data, queries and business methods live together. No separate layers. |
| Recordset (`self`) | `List<Entity>` | `self` can hold 0, 1 or many records. Methods run on the whole set; use `for rec in self` or `ensure_one()`. |
| `ir.model.access.csv` | Role check per entity type (`@PreAuthorize("hasRole('AGENT')")`) | It is table-level: "can this group read/write/create/delete this model at all?" |
| Record rule (`ir.rule`) | Hibernate `@Filter` / Spring Data Specification applied *automatically* to every query | The ORM injects it for you; you never call it. Superuser/`sudo()` bypasses it. |
| `@api.depends` compute | `@Formula` / derived getter | Odoo builds a dependency graph, caches, and recomputes only what changed (and can store the result in DB). |
| `@api.constrains` / `models.Constraint` | Bean Validation (`@AssertTrue`) / DB constraint | Python constraints run on create/write of the listed fields; SQL constraints live in the DB schema. |
| `_inherit` with a list of mixins | Interfaces with default methods | Mixins are merged into the class at registry-build time. |
| `super().create(vals_list)` | Overriding `save()` and calling `super.save()` | `create` receives a **list** of dicts (batch) in modern Odoo. |
| `ir.cron` | `@Scheduled` | Defined as a **database record** (XML data), editable in the UI. |
| `mail.thread` | Audit log (Envers) + notification service + event bus | One mixin gives chatter, field tracking, followers, email in/out. |
| `@http.route` | `@GetMapping` / `@PostMapping` | Needs `auth=` ("user", "public") and CSRF handling for POST. |
| XML views | Thymeleaf/JSP templates | Views are *declarative definitions* rendered by the JS client; there is no controller per screen. |
| `-u module` (upgrade) | Flyway migration + redeploy | Reloads XML/CSV data and syncs DB columns. **Python changes need a restart; XML/CSV changes need `-u`.** |

---

## 2. Functional requirements

### 2.1 Roles

| Role | Technical group | Can do |
| --- | --- | --- |
| Customer | `base.group_portal` | Create tickets in the portal, see **own** tickets, reply |
| Agent | `mini_helpdesk.group_helpdesk_user` | Work tickets of teams they belong to, reply, resolve, escalate |
| Manager | `mini_helpdesk.group_helpdesk_manager` | Everything: all tickets, configuration (teams, stages, SLA, tags) |

Manager implies Agent. Agent implies Internal User.

### 2.2 Ticket lifecycle (stages)

```text
        ┌─────────┐   first agent reply   ┌─────────────┐
 new ──►│   New   │──────────────────────►│ In Progress │◄───────┐
        └─────────┘                       └──────┬──────┘        │
                                                 │ ▲             │ Reopen
                                  waiting on     │ │ customer    │
                                  customer       ▼ │ replies     │
                                          ┌──────────────┐       │
                                          │ Waiting on   │       │
                                          │ Customer     │       │
                                          └──────┬───────┘       │
                                                 ▼               │
                                          ┌──────────────┐       │
                                          │  Resolved    │───────┘
                                          │ (closing)    │
                                          └──────────────┘
                                          ┌──────────────┐
                                          │  Cancelled   │ (closing)
                                          └──────────────┘
```

Stages are **data**, not code (a `helpdesk.stage` model), so a manager can add/reorder them. Two flags give them behaviour:

* `is_closed` — ticket is finished (sets `closed_date`, stops SLA, hides from "Open").
* `require_note` — moving into this stage requires a resolution note.

### 2.3 Business rules

| ID | Rule |
| --- | --- |
| BR-01 | Each ticket gets a unique number `HD-<year>-<5 digits>` from `ir.sequence`. |
| BR-02 | New tickets default to the first stage and to the creator's team (or the first team). |
| BR-03 | Team assignment method: `manual`, `round_robin`, or `least_loaded`. On create, if no assignee is given and the method is not manual, the ticket is auto-assigned to a team member. |
| BR-04 | Assigning a ticket to someone else schedules a **To-Do activity** for them. |
| BR-05 | SLA policy is chosen by **priority** (and optionally team; team-specific beats global). It yields a *response deadline* and a *resolution deadline* counted from creation. |
| BR-06 | `first_response_date` is set when an **internal** user posts a **message** (not an internal note). If the ticket is still "New" it moves to "In Progress". |
| BR-07 | Entering a closing stage sets `closed_date`; leaving it clears it. Closing marks the open To-Do activities as done. |
| BR-08 | A stage with `require_note` cannot be entered while `resolution_note` is empty. |
| BR-09 | Cron every 15 min: open, non-escalated tickets with a missed response or resolution deadline are **escalated** (flag + internal note + To-Do for the team leader). |
| BR-10 | Cron hourly: tickets unassigned > 1 hour trigger **one** reminder To-Do for the team leader. |
| BR-11 | An email sent to the support alias creates a ticket; the sender is matched to a partner by email. |
| BR-12 | Customers receive an acknowledgement email when a ticket is created for them. |
| BR-13 | Portal customers see only their own tickets. Agents see tickets of their teams or assigned to them. Managers see all. |
| BR-14 | SLA hours must be > 0 and resolution hours ≥ response hours. Team names are unique per company. Tag names are unique. |

### 2.4 User stories (acceptance = Section 6 checklist)

* **US-01** As a customer I can open a ticket from the portal and follow the conversation.
* **US-02** As an agent I see a kanban board of my team's tickets and drag them between stages.
* **US-03** As an agent I get an activity when a ticket is assigned to me.
* **US-04** As a manager I configure teams, stages, tags and SLA policies.
* **US-05** As a manager I am alerted (activity) when an SLA is breached.
* **US-06** As anyone I can email `support@…` and a ticket appears.
* **US-07** As a manager I can analyse tickets by team/stage/priority and print a ticket PDF.

---

## 3. Data model

```text
helpdesk.team ──< helpdesk.ticket >── helpdesk.stage
     │                │  │
     │ Many2many      │  └── Many2many ── helpdesk.tag
     ▼                │
 res.users            ├── Many2one ── res.partner (customer)
 (members, leader)    ├── Many2one ── res.users   (assignee)
                      └── Many2one ── helpdesk.sla.policy (computed)
```

### 3.1 `helpdesk.stage`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | Char, required, translate | |
| `sequence` | Integer, default 10 | ordering / drag handle |
| `fold` | Boolean | folded column in kanban |
| `is_closed` | Boolean | closing stage |
| `require_note` | Boolean | needs resolution note |

### 3.2 `helpdesk.tag`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | Char, required | SQL unique |
| `color` | Integer | random default |

### 3.3 `helpdesk.team`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | Char, required | unique per company |
| `active` | Boolean | archive |
| `company_id` | Many2one `res.company` | |
| `leader_id` | Many2one `res.users` | receives escalations |
| `member_ids` | Many2many `res.users` | table `helpdesk_team_user_rel` |
| `assignment_method` | Selection | `manual` / `round_robin` / `least_loaded` |
| `last_assigned_user_id` | Many2one `res.users` | round-robin pointer |
| `open_ticket_count` | Integer, computed | smart button |

### 3.4 `helpdesk.sla.policy`

| Field | Type | Notes |
| --- | --- | --- |
| `name` | Char, required | |
| `active` | Boolean | |
| `team_id` | Many2one `helpdesk.team` | empty = all teams |
| `priority` | Selection `0..3` | Low, Medium, High, Urgent |
| `response_hours` | Float | |
| `resolution_hours` | Float | |
| `company_id` | Many2one | |

### 3.5 `helpdesk.ticket`

| Field | Type | Notes |
| --- | --- | --- |
| `number` | Char, readonly, copy=False | from sequence |
| `name` | Char (Subject), required, tracking | |
| `description` | Html | |
| `ticket_type` | Selection | question / incident / request |
| `priority` | Selection `0..3`, tracking | default `1` |
| `tag_ids` | Many2many `helpdesk.tag` | |
| `color` | Integer | |
| `active` | Boolean | |
| `company_id` | Many2one | |
| `partner_id` | Many2one `res.partner` (Customer), tracking | |
| `partner_email` | Char, compute+store, readonly=False | |
| `team_id` | Many2one, required, tracking | |
| `team_member_ids` | Many2many related | only to build the assignee domain |
| `user_id` | Many2one `res.users` (Assigned To), tracking | |
| `stage_id` | Many2one, tracking, `group_expand` | |
| `is_closed` | Boolean related, stored | `stage_id.is_closed` |
| `resolution_note` | Text, tracking | |
| `sla_policy_id` | Many2one, compute+store | |
| `response_deadline` / `resolution_deadline` | Datetime, compute+store | |
| `sla_status` | Selection, compute (not stored) | none / on_track / at_risk / failed / achieved |
| `first_response_date` | Datetime, readonly | |
| `closed_date` | Datetime, readonly | |
| `escalated` | Boolean, readonly | |
| `unassigned_reminded` | Boolean, readonly | prevents repeated reminders |

---

## 4. Module structure

```text
custom-addons/mini_helpdesk/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   ├── constants.py
│   ├── helpdesk_stage.py
│   ├── helpdesk_tag.py
│   ├── helpdesk_sla_policy.py
│   ├── helpdesk_team.py
│   └── helpdesk_ticket.py
├── controllers/
│   ├── __init__.py
│   └── portal.py
├── security/
│   ├── helpdesk_groups.xml
│   ├── ir.model.access.csv
│   └── helpdesk_rules.xml
├── data/
│   ├── ir_sequence_data.xml
│   ├── helpdesk_stage_data.xml
│   ├── helpdesk_sla_data.xml
│   ├── helpdesk_team_data.xml
│   ├── mail_template_data.xml
│   └── ir_cron_data.xml
├── views/
│   ├── helpdesk_stage_views.xml
│   ├── helpdesk_tag_views.xml
│   ├── helpdesk_sla_policy_views.xml
│   ├── helpdesk_team_views.xml
│   ├── helpdesk_ticket_views.xml
│   ├── helpdesk_menus.xml
│   └── helpdesk_portal_templates.xml
├── report/
│   └── helpdesk_ticket_report.xml
├── demo/
│   └── helpdesk_demo.xml
└── tests/
    ├── __init__.py
    └── test_helpdesk_ticket.py
```

Your Docker setup mounts `custom-addons/` at `/mnt/extra-addons`, so create the folder there.

---

## 5. Implementation plan

| Phase | Deliverable | Day |
| --- | --- | --- |
| 0 | Module skeleton installs | 1 |
| 1 | Config models (stage, tag, team, SLA) + groups + ACL + menus | 1 |
| 2 | Ticket model (fields only) + views + sequence | 2 |
| 3 | Ticket business logic (create/write, buttons, SLA, constraints) | 3 |
| 4 | Auto-assignment | 4 |
| 5 | Record rules | 4 |
| 6 | Cron jobs + escalation | 5 |
| 7 | Email to ticket + templates | 6 |
| 8 | Customer portal | 7 |
| 9 | Reports (pivot, graph, PDF) | 8 |
| 10 | Tests, demo data, polish | 9–10 |

**Rhythm for every phase:** write code → restart/upgrade → click through → fix → commit. Never write two phases blind.

### Useful commands (PowerShell, from your Docker Compose project folder)

Replace `<odoo>` with your Odoo service name and `<db>` with your database.

```powershell
# Install the module for the first time
docker compose exec <odoo> odoo -c /etc/odoo/odoo.conf -d <db> -i mini_helpdesk --stop-after-init

# After changing XML / CSV / data / model fields: upgrade
docker compose exec <odoo> odoo -c /etc/odoo/odoo.conf -d <db> -u mini_helpdesk --stop-after-init

# After changing ONLY Python method bodies: restart is enough
docker compose restart <odoo>

# Run the tests
docker compose exec <odoo> odoo -c /etc/odoo/odoo.conf -d <test_db> -i mini_helpdesk --test-tags /mini_helpdesk --stop-after-init
```

If your container uses another config path, use the one from your own setup. Use a **separate database for tests**.

---

## Phase 0 — Skeleton

### `__init__.py`

```python
from . import models
from . import controllers
```

### `models/__init__.py`

```python
from . import constants
from . import helpdesk_stage
from . import helpdesk_tag
from . import helpdesk_sla_policy
from . import helpdesk_team
from . import helpdesk_ticket
```

### `controllers/__init__.py`

```python
from . import portal
```

(Create `controllers/portal.py` as an empty file for now; fill it in Phase 8. Comment out the `controllers` import until then if you prefer.)

### `__manifest__.py`

The **order of `data` matters**: groups → access → rules → data → views → menus last.

```python
{
    'name': 'Mini Helpdesk',
    'version': '19.0.1.0.0',
    'category': 'Services/Helpdesk',
    'summary': 'Ticketing with SLA, assignment, escalation and customer portal',
    'depends': ['base', 'mail', 'portal'],
    'data': [
        'security/helpdesk_groups.xml',
        'security/ir.model.access.csv',
        'security/helpdesk_rules.xml',
        'data/ir_sequence_data.xml',
        'data/helpdesk_stage_data.xml',
        'data/helpdesk_sla_data.xml',
        'data/helpdesk_team_data.xml',
        'data/mail_template_data.xml',
        'data/ir_cron_data.xml',
        'views/helpdesk_stage_views.xml',
        'views/helpdesk_tag_views.xml',
        'views/helpdesk_sla_policy_views.xml',
        'views/helpdesk_team_views.xml',
        'views/helpdesk_ticket_views.xml',
        'report/helpdesk_ticket_report.xml',
        'views/helpdesk_portal_templates.xml',
        'views/helpdesk_menus.xml',
    ],
    'demo': ['demo/helpdesk_demo.xml'],
    'application': True,
    'installable': True,
    'license': 'LGPL-3',
}
```

> While building, **only list the files that exist so far**. Add each file to the manifest the moment you create it. A missing file listed here = install error.

---

## Phase 1 — Configuration models, groups, ACL, menus

### `models/constants.py`

```python
TICKET_PRIORITIES = [
    ('0', 'Low'),
    ('1', 'Medium'),
    ('2', 'High'),
    ('3', 'Urgent'),
]
```

### `models/helpdesk_stage.py`

```python
from odoo import fields, models


class HelpdeskStage(models.Model):
    _name = 'helpdesk.stage'
    _description = 'Helpdesk Stage'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    fold = fields.Boolean('Folded in Kanban')
    is_closed = fields.Boolean(
        'Closing Stage',
        help="Tickets in this stage are finished: SLA stops and they leave the 'Open' filter.")
    require_note = fields.Boolean(
        'Requires Resolution Note',
        help="A ticket cannot enter this stage without a resolution note.")
```

### `models/helpdesk_tag.py`

```python
from random import randint

from odoo import fields, models


class HelpdeskTag(models.Model):
    _name = 'helpdesk.tag'
    _description = 'Helpdesk Tag'

    name = fields.Char(required=True, translate=True)
    color = fields.Integer(default=lambda self: randint(1, 11))

    _name_uniq = models.Constraint('UNIQUE(name)', 'This tag already exists.')
```

> Odoo ≤ 18 equivalent: `_sql_constraints = [('name_uniq', 'unique(name)', 'This tag already exists.')]`

### `models/helpdesk_sla_policy.py`

```python
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .constants import TICKET_PRIORITIES


class HelpdeskSlaPolicy(models.Model):
    _name = 'helpdesk.sla.policy'
    _description = 'Helpdesk SLA Policy'
    _order = 'priority desc, id'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    team_id = fields.Many2one(
        'helpdesk.team', string='Team',
        help="Leave empty to apply to every team.")
    priority = fields.Selection(TICKET_PRIORITIES, required=True, default='1')
    response_hours = fields.Float('First Response (hours)', required=True, default=4.0)
    resolution_hours = fields.Float('Resolution (hours)', required=True, default=48.0)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    _positive_hours = models.Constraint(
        'CHECK(response_hours > 0 AND resolution_hours > 0)',
        'SLA hours must be greater than zero.')

    @api.constrains('response_hours', 'resolution_hours')
    def _check_hours_order(self):
        for policy in self:
            if policy.resolution_hours < policy.response_hours:
                raise ValidationError(_("Resolution time cannot be shorter than first response time."))
```

### `models/helpdesk_team.py`

(The assignment method `_get_next_assignee` is included now; you will test it in Phase 4.)

```python
from odoo import api, fields, models


class HelpdeskTeam(models.Model):
    _name = 'helpdesk.team'
    _description = 'Helpdesk Team'
    _order = 'name'

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    leader_id = fields.Many2one('res.users', string='Team Leader')
    member_ids = fields.Many2many(
        'res.users', 'helpdesk_team_user_rel', 'team_id', 'user_id', string='Members')
    assignment_method = fields.Selection([
        ('manual', 'Manual'),
        ('round_robin', 'Round Robin'),
        ('least_loaded', 'Least Loaded'),
    ], default='manual', required=True)
    last_assigned_user_id = fields.Many2one('res.users', readonly=True, copy=False)
    open_ticket_count = fields.Integer(compute='_compute_open_ticket_count')

    _name_company_uniq = models.Constraint(
        'UNIQUE(name, company_id)', 'Team name must be unique per company.')

    def _compute_open_ticket_count(self):
        data = self.env['helpdesk.ticket']._read_group(
            [('team_id', 'in', self.ids), ('is_closed', '=', False)],
            ['team_id'], ['__count'])
        counts = {team.id: count for team, count in data}
        for team in self:
            team.open_ticket_count = counts.get(team.id, 0)

    def action_open_tickets(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('mini_helpdesk.helpdesk_ticket_action')
        action['domain'] = [('team_id', '=', self.id), ('is_closed', '=', False)]
        action['context'] = {'default_team_id': self.id}
        return action

    def _get_next_assignee(self):
        """Return the next member (res.users) according to the assignment method,
        or an empty recordset when nothing should be assigned."""
        self.ensure_one()
        members = self.member_ids.filtered('active').sorted('id')
        if not members or self.assignment_method == 'manual':
            return self.env['res.users']

        if self.assignment_method == 'round_robin':
            last_id = self.last_assigned_user_id.id or 0
            later = members.filtered(lambda user: user.id > last_id)
            chosen = (later or members)[:1]
        else:  # least_loaded
            data = self.env['helpdesk.ticket']._read_group(
                [('user_id', 'in', members.ids), ('is_closed', '=', False)],
                ['user_id'], ['__count'])
            load = {user.id: count for user, count in data}
            chosen = min(members, key=lambda user: (load.get(user.id, 0), user.id))

        # sudo: agents creating tickets have no write access on the team.
        self.sudo().last_assigned_user_id = chosen
        return chosen
```

> `_read_group` returns a list of tuples: `(group value(s)..., aggregate(s)...)`. With `['team_id']` and `['__count']` you get `(team_record, count)`.

### `security/helpdesk_groups.xml`

Odoo 19 attaches groups to a **privilege** (`res.groups.privilege`).

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="privilege_helpdesk" model="res.groups.privilege">
        <field name="name">Mini Helpdesk</field>
        <field name="category_id" ref="base.module_category_services"/>
    </record>

    <record id="group_helpdesk_user" model="res.groups">
        <field name="name">Agent</field>
        <field name="privilege_id" ref="privilege_helpdesk"/>
        <field name="implied_ids" eval="[(4, ref('base.group_user'))]"/>
    </record>

    <record id="group_helpdesk_manager" model="res.groups">
        <field name="name">Manager</field>
        <field name="privilege_id" ref="privilege_helpdesk"/>
        <field name="implied_ids" eval="[(4, ref('group_helpdesk_user'))]"/>
    </record>
</odoo>
```

> If your build rejects `privilege_id` / `res.groups.privilege`, delete the privilege record and the two `privilege_id` lines. The groups still work; they just appear under "Other" in the user form.
> After installing, give **yourself** the Manager group (Settings → Users).

### `security/ir.model.access.csv` (Phase 1 lines)

```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_helpdesk_team_agent,helpdesk.team agent,model_helpdesk_team,group_helpdesk_user,1,0,0,0
access_helpdesk_team_manager,helpdesk.team manager,model_helpdesk_team,group_helpdesk_manager,1,1,1,1
access_helpdesk_stage_agent,helpdesk.stage agent,model_helpdesk_stage,group_helpdesk_user,1,0,0,0
access_helpdesk_stage_manager,helpdesk.stage manager,model_helpdesk_stage,group_helpdesk_manager,1,1,1,1
access_helpdesk_stage_portal,helpdesk.stage portal,model_helpdesk_stage,base.group_portal,1,0,0,0
access_helpdesk_tag_agent,helpdesk.tag agent,model_helpdesk_tag,group_helpdesk_user,1,1,1,0
access_helpdesk_tag_manager,helpdesk.tag manager,model_helpdesk_tag,group_helpdesk_manager,1,1,1,1
access_helpdesk_sla_agent,helpdesk.sla.policy agent,model_helpdesk_sla_policy,group_helpdesk_user,1,0,0,0
access_helpdesk_sla_manager,helpdesk.sla.policy manager,model_helpdesk_sla_policy,group_helpdesk_manager,1,1,1,1
```

Rules for this file: no blank lines, no spaces after commas, `model_id:id` is `model_` + model name with dots replaced by underscores, and groups from your own module need no prefix (others need `module.`).

### `data/helpdesk_stage_data.xml`

`noupdate="1"` = loaded once at install, never overwritten on upgrade (so managers' edits survive). **During development this means edits to this file are ignored on `-u`** — delete the records in the UI (or reinstall on a scratch DB) when you need to re-test them.

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="stage_new" model="helpdesk.stage">
            <field name="name">New</field>
            <field name="sequence">10</field>
        </record>
        <record id="stage_in_progress" model="helpdesk.stage">
            <field name="name">In Progress</field>
            <field name="sequence">20</field>
        </record>
        <record id="stage_waiting" model="helpdesk.stage">
            <field name="name">Waiting on Customer</field>
            <field name="sequence">30</field>
        </record>
        <record id="stage_resolved" model="helpdesk.stage">
            <field name="name">Resolved</field>
            <field name="sequence">40</field>
            <field name="is_closed" eval="True"/>
            <field name="require_note" eval="True"/>
            <field name="fold" eval="True"/>
        </record>
        <record id="stage_cancelled" model="helpdesk.stage">
            <field name="name">Cancelled</field>
            <field name="sequence">50</field>
            <field name="is_closed" eval="True"/>
            <field name="fold" eval="True"/>
        </record>
    </data>
</odoo>
```

### `data/helpdesk_sla_data.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="sla_low" model="helpdesk.sla.policy">
            <field name="name">Low priority</field>
            <field name="priority">0</field>
            <field name="response_hours">8</field>
            <field name="resolution_hours">72</field>
        </record>
        <record id="sla_medium" model="helpdesk.sla.policy">
            <field name="name">Medium priority</field>
            <field name="priority">1</field>
            <field name="response_hours">4</field>
            <field name="resolution_hours">48</field>
        </record>
        <record id="sla_high" model="helpdesk.sla.policy">
            <field name="name">High priority</field>
            <field name="priority">2</field>
            <field name="response_hours">2</field>
            <field name="resolution_hours">24</field>
        </record>
        <record id="sla_urgent" model="helpdesk.sla.policy">
            <field name="name">Urgent priority</field>
            <field name="priority">3</field>
            <field name="response_hours">1</field>
            <field name="resolution_hours">8</field>
        </record>
    </data>
</odoo>
```

### `data/helpdesk_team_data.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="team_support" model="helpdesk.team">
            <field name="name">Customer Support</field>
            <field name="leader_id" ref="base.user_admin"/>
            <field name="member_ids" eval="[(4, ref('base.user_admin'))]"/>
        </record>
    </data>
</odoo>
```

### Views for the configuration models

Odoo 17+ rules you must follow in XML: **`<tree>` is now `<list>`**, and `attrs="{...}"` / `states="..."` are gone — use `invisible="expression"`, `readonly="expression"`, `required="expression"` directly.

#### `views/helpdesk_stage_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="helpdesk_stage_view_list" model="ir.ui.view">
        <field name="name">helpdesk.stage.list</field>
        <field name="model">helpdesk.stage</field>
        <field name="arch" type="xml">
            <list editable="bottom">
                <field name="sequence" widget="handle"/>
                <field name="name"/>
                <field name="is_closed"/>
                <field name="require_note"/>
                <field name="fold"/>
            </list>
        </field>
    </record>

    <record id="helpdesk_stage_action" model="ir.actions.act_window">
        <field name="name">Stages</field>
        <field name="res_model">helpdesk.stage</field>
        <field name="view_mode">list</field>
    </record>
</odoo>
```

#### `views/helpdesk_tag_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="helpdesk_tag_view_list" model="ir.ui.view">
        <field name="name">helpdesk.tag.list</field>
        <field name="model">helpdesk.tag</field>
        <field name="arch" type="xml">
            <list editable="bottom">
                <field name="name"/>
                <field name="color" widget="color_picker"/>
            </list>
        </field>
    </record>

    <record id="helpdesk_tag_action" model="ir.actions.act_window">
        <field name="name">Tags</field>
        <field name="res_model">helpdesk.tag</field>
        <field name="view_mode">list</field>
    </record>
</odoo>
```

#### `views/helpdesk_sla_policy_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="helpdesk_sla_policy_view_list" model="ir.ui.view">
        <field name="name">helpdesk.sla.policy.list</field>
        <field name="model">helpdesk.sla.policy</field>
        <field name="arch" type="xml">
            <list editable="bottom">
                <field name="name"/>
                <field name="team_id"/>
                <field name="priority"/>
                <field name="response_hours"/>
                <field name="resolution_hours"/>
                <field name="company_id" groups="base.group_multi_company"/>
            </list>
        </field>
    </record>

    <record id="helpdesk_sla_policy_action" model="ir.actions.act_window">
        <field name="name">SLA Policies</field>
        <field name="res_model">helpdesk.sla.policy</field>
        <field name="view_mode">list</field>
    </record>
</odoo>
```

#### `views/helpdesk_team_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="helpdesk_team_view_list" model="ir.ui.view">
        <field name="name">helpdesk.team.list</field>
        <field name="model">helpdesk.team</field>
        <field name="arch" type="xml">
            <list>
                <field name="name"/>
                <field name="leader_id"/>
                <field name="assignment_method"/>
                <field name="open_ticket_count"/>
            </list>
        </field>
    </record>

    <record id="helpdesk_team_view_form" model="ir.ui.view">
        <field name="name">helpdesk.team.form</field>
        <field name="model">helpdesk.team</field>
        <field name="arch" type="xml">
            <form>
                <sheet>
                    <div class="oe_button_box" name="button_box">
                        <button name="action_open_tickets" type="object"
                                class="oe_stat_button" icon="fa-ticket">
                            <field name="open_ticket_count" widget="statinfo" string="Open Tickets"/>
                        </button>
                    </div>
                    <widget name="web_ribbon" title="Archived" bg_color="text-bg-danger"
                            invisible="active"/>
                    <group>
                        <group>
                            <field name="name"/>
                            <field name="leader_id"/>
                            <field name="company_id" groups="base.group_multi_company"/>
                            <field name="active" invisible="1"/>
                        </group>
                        <group>
                            <field name="assignment_method"/>
                            <field name="last_assigned_user_id"
                                   invisible="assignment_method != 'round_robin'"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Members" name="members">
                            <field name="member_ids" widget="many2many_avatar_user"/>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <record id="helpdesk_team_action" model="ir.actions.act_window">
        <field name="name">Teams</field>
        <field name="res_model">helpdesk.team</field>
        <field name="view_mode">list,form</field>
    </record>
</odoo>
```

> The team form's smart button points at `helpdesk_ticket_action`, which you create in Phase 2. Until then, leave the button out or the install fails when you click it (the XML loads fine; the error shows at runtime).

### `views/helpdesk_menus.xml` (temporary version, extend in Phase 2)

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <menuitem id="menu_helpdesk_root" name="Helpdesk" sequence="40"
              groups="group_helpdesk_user"/>

    <menuitem id="menu_helpdesk_config" name="Configuration" parent="menu_helpdesk_root"
              sequence="90" groups="group_helpdesk_manager"/>
    <menuitem id="menu_helpdesk_team" name="Teams" parent="menu_helpdesk_config"
              action="helpdesk_team_action" sequence="10"/>
    <menuitem id="menu_helpdesk_stage" name="Stages" parent="menu_helpdesk_config"
              action="helpdesk_stage_action" sequence="20"/>
    <menuitem id="menu_helpdesk_tag" name="Tags" parent="menu_helpdesk_config"
              action="helpdesk_tag_action" sequence="30"/>
    <menuitem id="menu_helpdesk_sla" name="SLA Policies" parent="menu_helpdesk_config"
              action="helpdesk_sla_policy_action" sequence="40"/>
</odoo>
```

At this point `models/helpdesk_ticket.py` does not exist yet, but `helpdesk_team.py` references the model `helpdesk.ticket` inside methods only (not at import time), so the module loads. **Remove `'data/mail_template_data.xml'`, `'data/ir_cron_data.xml'`, the ticket views, report, portal, and rules from the manifest for now**, and comment out `action_open_tickets` usage. Install and check:

**Verify Phase 1**
* [ ] Module installs without errors.
* [ ] Helpdesk menu appears for a Manager (give yourself the group, refresh).
* [ ] Create a team, add yourself as a member; stages and SLA policies are listed.
* [ ] Try to save an SLA policy with resolution hours < response hours → validation error.
* [ ] Try to create two tags with the same name → DB constraint error.

---

## Phase 2 — The ticket model (fields + views)

### `data/ir_sequence_data.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="seq_helpdesk_ticket" model="ir.sequence">
            <field name="name">Helpdesk Ticket</field>
            <field name="code">helpdesk.ticket</field>
            <field name="prefix">HD-%(year)s-</field>
            <field name="padding">5</field>
            <field name="company_id" eval="False"/>
        </record>
    </data>
</odoo>
```

### `models/helpdesk_ticket.py` — fields only (logic comes in Phase 3)

```python
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from odoo.tools import email_split

from .constants import TICKET_PRIORITIES

AT_RISK_WINDOW = timedelta(hours=2)


class HelpdeskTicket(models.Model):
    _name = 'helpdesk.ticket'
    _description = 'Helpdesk Ticket'
    _inherit = ['portal.mixin', 'mail.thread', 'mail.activity.mixin']
    _order = 'priority desc, id desc'

    # ---- defaults -------------------------------------------------------
    def _default_team_id(self):
        Team = self.env['helpdesk.team']
        return Team.search([('member_ids', 'in', self.env.uid)], limit=1) or Team.search([], limit=1)

    def _default_stage_id(self):
        return self.env['helpdesk.stage'].search([], limit=1)  # ordered by sequence

    @api.model
    def _read_group_stage_ids(self, stages, domain):
        """Show every stage as a kanban column, even the empty ones."""
        return stages.search([], order=stages._order)

    # ---- fields ---------------------------------------------------------
    number = fields.Char(readonly=True, copy=False, default='New', index=True)
    name = fields.Char('Subject', required=True, tracking=True)
    description = fields.Html()
    ticket_type = fields.Selection([
        ('question', 'Question'),
        ('incident', 'Incident'),
        ('request', 'Service Request'),
    ], default='question', tracking=True)
    priority = fields.Selection(TICKET_PRIORITIES, default='1', tracking=True, index=True)
    tag_ids = fields.Many2many('helpdesk.tag', string='Tags')
    color = fields.Integer()
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)

    partner_id = fields.Many2one('res.partner', string='Customer', tracking=True, index=True)
    partner_email = fields.Char(
        'Customer Email', compute='_compute_partner_email', store=True, readonly=False)

    team_id = fields.Many2one(
        'helpdesk.team', string='Team', required=True, tracking=True,
        default=_default_team_id, index=True)
    team_member_ids = fields.Many2many(related='team_id.member_ids', string='Team Members')
    user_id = fields.Many2one(
        'res.users', string='Assigned To', tracking=True, index=True, copy=False)

    stage_id = fields.Many2one(
        'helpdesk.stage', string='Stage', tracking=True, copy=False, index=True,
        default=_default_stage_id, group_expand='_read_group_stage_ids',
        ondelete='restrict')
    is_closed = fields.Boolean(related='stage_id.is_closed', store=True)
    resolution_note = fields.Text(tracking=True)

    sla_policy_id = fields.Many2one(
        'helpdesk.sla.policy', compute='_compute_sla_policy', store=True)
    response_deadline = fields.Datetime(compute='_compute_sla_deadlines', store=True)
    resolution_deadline = fields.Datetime(compute='_compute_sla_deadlines', store=True)
    sla_status = fields.Selection([
        ('none', 'No SLA'),
        ('on_track', 'On Track'),
        ('at_risk', 'At Risk'),
        ('failed', 'Failed'),
        ('achieved', 'Achieved'),
    ], compute='_compute_sla_status')

    first_response_date = fields.Datetime(readonly=True, copy=False)
    closed_date = fields.Datetime(readonly=True, copy=False)
    escalated = fields.Boolean(readonly=True, copy=False)
    unassigned_reminded = fields.Boolean(readonly=True, copy=False)

    # ---- display --------------------------------------------------------
    @api.depends('number', 'name')
    def _compute_display_name(self):
        for ticket in self:
            ticket.display_name = f"[{ticket.number}] {ticket.name}" if ticket.number else ticket.name

    # ---- computes -------------------------------------------------------
    @api.depends('partner_id')
    def _compute_partner_email(self):
        for ticket in self:
            if ticket.partner_id and ticket.partner_id.email != ticket.partner_email:
                ticket.partner_email = ticket.partner_id.email

    def _find_sla_policy(self):
        """Team-specific policy wins over a global one (team_id empty)."""
        self.ensure_one()
        policies = self.env['helpdesk.sla.policy'].search([
            ('priority', '=', self.priority),
            '|', ('team_id', '=', self.team_id.id), ('team_id', '=', False),
        ])
        specific = policies.filtered('team_id')
        return (specific or policies)[:1]

    @api.depends('team_id', 'priority')
    def _compute_sla_policy(self):
        for ticket in self:
            ticket.sla_policy_id = ticket._find_sla_policy()

    @api.depends('sla_policy_id', 'create_date')
    def _compute_sla_deadlines(self):
        for ticket in self:
            policy = ticket.sla_policy_id
            start = ticket.create_date or fields.Datetime.now()
            ticket.response_deadline = (
                start + timedelta(hours=policy.response_hours) if policy else False)
            ticket.resolution_deadline = (
                start + timedelta(hours=policy.resolution_hours) if policy else False)

    @api.depends('resolution_deadline', 'is_closed', 'closed_date')
    def _compute_sla_status(self):
        now = fields.Datetime.now()
        for ticket in self:
            deadline = ticket.resolution_deadline
            if not deadline:
                ticket.sla_status = 'none'
            elif ticket.is_closed:
                met = ticket.closed_date and ticket.closed_date <= deadline
                ticket.sla_status = 'achieved' if met else 'failed'
            elif now > deadline:
                ticket.sla_status = 'failed'
            elif deadline - now <= AT_RISK_WINDOW:
                ticket.sla_status = 'at_risk'
            else:
                ticket.sla_status = 'on_track'
```

> **Design decisions worth understanding**
> * `sla_status` is **not stored** because it depends on "now", which Odoo's dependency system cannot track. That means you can't group/search on it — which is why the "SLA Breached" filter later uses the *deadline field* and `datetime.datetime.now()` instead.
> * Deadlines are counted from `create_date`. If an agent changes priority later, the SLA is re-evaluated from the original creation time. (A real product would have rules for this; here it keeps the code simple.)

Add to `models/__init__.py` (already listed in Phase 0).

### Extra ACL lines for the ticket (append to `ir.model.access.csv`)

```csv
access_helpdesk_ticket_agent,helpdesk.ticket agent,model_helpdesk_ticket,group_helpdesk_user,1,1,1,0
access_helpdesk_ticket_manager,helpdesk.ticket manager,model_helpdesk_ticket,group_helpdesk_manager,1,1,1,1
access_helpdesk_ticket_portal,helpdesk.ticket portal,model_helpdesk_ticket,base.group_portal,1,0,0,0
```

### `views/helpdesk_ticket_views.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>

    <!-- LIST -->
    <record id="helpdesk_ticket_view_list" model="ir.ui.view">
        <field name="name">helpdesk.ticket.list</field>
        <field name="model">helpdesk.ticket</field>
        <field name="arch" type="xml">
            <list decoration-danger="sla_status == 'failed'"
                  decoration-warning="sla_status == 'at_risk'"
                  decoration-muted="is_closed">
                <field name="number"/>
                <field name="name"/>
                <field name="partner_id"/>
                <field name="team_id" optional="show"/>
                <field name="user_id" widget="many2one_avatar_user"/>
                <field name="priority" widget="priority"/>
                <field name="stage_id"/>
                <field name="resolution_deadline" optional="show"/>
                <field name="sla_status" widget="badge" optional="show"
                       decoration-danger="sla_status == 'failed'"
                       decoration-warning="sla_status == 'at_risk'"
                       decoration-success="sla_status in ('achieved', 'on_track')"/>
                <field name="is_closed" column_invisible="True"/>
                <field name="activity_ids" widget="list_activity" optional="show"/>
            </list>
        </field>
    </record>

    <!-- FORM -->
    <record id="helpdesk_ticket_view_form" model="ir.ui.view">
        <field name="name">helpdesk.ticket.form</field>
        <field name="model">helpdesk.ticket</field>
        <field name="arch" type="xml">
            <form>
                <header>
                    <button name="action_assign_to_me" string="Assign to Me" type="object"
                            invisible="user_id or is_closed"/>
                    <button name="action_resolve" string="Resolve" type="object"
                            class="btn-primary" invisible="is_closed"/>
                    <button name="action_escalate" string="Escalate" type="object"
                            invisible="is_closed or escalated"/>
                    <button name="action_reopen" string="Reopen" type="object"
                            invisible="not is_closed"/>
                    <field name="stage_id" widget="statusbar" options="{'clickable': '1'}"/>
                </header>
                <sheet>
                    <widget name="web_ribbon" title="Escalated" bg_color="text-bg-danger"
                            invisible="not escalated"/>
                    <widget name="web_ribbon" title="Archived" bg_color="text-bg-secondary"
                            invisible="active"/>
                    <div class="oe_title">
                        <h1><field name="name" placeholder="Subject…"/></h1>
                        <div class="text-muted"><field name="number"/></div>
                    </div>
                    <group>
                        <group name="left">
                            <field name="partner_id"/>
                            <field name="partner_email" widget="email"/>
                            <field name="ticket_type"/>
                            <field name="tag_ids" widget="many2many_tags"
                                   options="{'color_field': 'color'}"/>
                        </group>
                        <group name="right">
                            <field name="team_id"/>
                            <field name="team_member_ids" invisible="1"/>
                            <field name="user_id" domain="[('id', 'in', team_member_ids)]"/>
                            <field name="priority" widget="priority"/>
                            <field name="company_id" groups="base.group_multi_company"/>
                            <field name="active" invisible="1"/>
                            <field name="is_closed" invisible="1"/>
                            <field name="escalated" invisible="1"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Description" name="description">
                            <field name="description" nolabel="1"
                                   placeholder="Describe the problem…"/>
                        </page>
                        <page string="Resolution" name="resolution">
                            <field name="resolution_note" nolabel="1"
                                   placeholder="What was done to fix it? (required to resolve)"/>
                        </page>
                        <page string="SLA" name="sla">
                            <group>
                                <group>
                                    <field name="sla_policy_id"/>
                                    <field name="sla_status"/>
                                </group>
                                <group>
                                    <field name="response_deadline"/>
                                    <field name="first_response_date"/>
                                    <field name="resolution_deadline"/>
                                    <field name="closed_date"/>
                                </group>
                            </group>
                        </page>
                    </notebook>
                </sheet>
                <chatter/>
            </form>
        </field>
    </record>

    <!-- KANBAN -->
    <record id="helpdesk_ticket_view_kanban" model="ir.ui.view">
        <field name="name">helpdesk.ticket.kanban</field>
        <field name="model">helpdesk.ticket</field>
        <field name="arch" type="xml">
            <kanban default_group_by="stage_id" group_create="false">
                <templates>
                    <t t-name="card">
                        <field name="name" class="fw-bold fs-5"/>
                        <div class="text-muted">
                            <field name="number"/>
                            <t t-if="record.partner_id.value"> · <field name="partner_id"/></t>
                        </div>
                        <field name="tag_ids" widget="many2many_tags"
                               options="{'color_field': 'color'}"/>
                        <footer>
                            <field name="priority" widget="priority"/>
                            <field name="activity_ids" widget="kanban_activity"/>
                            <field name="user_id" widget="many2one_avatar_user" class="ms-auto"/>
                        </footer>
                    </t>
                </templates>
            </kanban>
        </field>
    </record>

    <!-- SEARCH -->
    <record id="helpdesk_ticket_view_search" model="ir.ui.view">
        <field name="name">helpdesk.ticket.search</field>
        <field name="model">helpdesk.ticket</field>
        <field name="arch" type="xml">
            <search>
                <field name="number"/>
                <field name="name"/>
                <field name="partner_id"/>
                <field name="user_id"/>
                <field name="team_id"/>
                <field name="tag_ids"/>
                <filter name="my_tickets" string="My Tickets"
                        domain="[('user_id', '=', uid)]"/>
                <filter name="unassigned" string="Unassigned"
                        domain="[('user_id', '=', False)]"/>
                <separator/>
                <filter name="open" string="Open" domain="[('is_closed', '=', False)]"/>
                <filter name="closed" string="Closed" domain="[('is_closed', '=', True)]"/>
                <separator/>
                <filter name="high_priority" string="High / Urgent"
                        domain="[('priority', 'in', ['2', '3'])]"/>
                <filter name="escalated" string="Escalated"
                        domain="[('escalated', '=', True)]"/>
                <filter name="sla_failed" string="SLA Breached"
                        domain="[('is_closed', '=', False), ('resolution_deadline', '&lt;', datetime.datetime.now())]"/>
                <separator/>
                <filter name="archived" string="Archived" domain="[('active', '=', False)]"/>
                <group string="Group By">
                    <filter name="group_stage" string="Stage" context="{'group_by': 'stage_id'}"/>
                    <filter name="group_team" string="Team" context="{'group_by': 'team_id'}"/>
                    <filter name="group_user" string="Assignee" context="{'group_by': 'user_id'}"/>
                    <filter name="group_priority" string="Priority" context="{'group_by': 'priority'}"/>
                    <filter name="group_type" string="Type" context="{'group_by': 'ticket_type'}"/>
                    <filter name="group_month" string="Created (month)"
                            context="{'group_by': 'create_date:month'}"/>
                </group>
            </search>
        </field>
    </record>

    <!-- PIVOT + GRAPH (used by Analysis menu and as extra view modes) -->
    <record id="helpdesk_ticket_view_pivot" model="ir.ui.view">
        <field name="name">helpdesk.ticket.pivot</field>
        <field name="model">helpdesk.ticket</field>
        <field name="arch" type="xml">
            <pivot>
                <field name="team_id" type="row"/>
                <field name="stage_id" type="col"/>
            </pivot>
        </field>
    </record>

    <record id="helpdesk_ticket_view_graph" model="ir.ui.view">
        <field name="name">helpdesk.ticket.graph</field>
        <field name="model">helpdesk.ticket</field>
        <field name="arch" type="xml">
            <graph type="bar">
                <field name="stage_id"/>
                <field name="team_id"/>
            </graph>
        </field>
    </record>

    <!-- ACTIONS -->
    <record id="helpdesk_ticket_action" model="ir.actions.act_window">
        <field name="name">Tickets</field>
        <field name="res_model">helpdesk.ticket</field>
        <field name="view_mode">kanban,list,form,pivot,graph</field>
        <field name="search_view_id" ref="helpdesk_ticket_view_search"/>
        <field name="context">{'search_default_open': 1}</field>
        <field name="help" type="html">
            <p class="o_view_nocontent_smiling_face">Create your first ticket</p>
            <p>Tickets track customer problems from first contact to resolution.</p>
        </field>
    </record>

    <record id="helpdesk_ticket_analysis_action" model="ir.actions.act_window">
        <field name="name">Ticket Analysis</field>
        <field name="res_model">helpdesk.ticket</field>
        <field name="view_mode">pivot,graph</field>
        <field name="search_view_id" ref="helpdesk_ticket_view_search"/>
    </record>
</odoo>
```

### Extend `views/helpdesk_menus.xml`

Add above the Configuration menu:

```xml
    <menuitem id="menu_helpdesk_tickets" name="Tickets" parent="menu_helpdesk_root"
              action="helpdesk_ticket_action" sequence="10"/>
    <menuitem id="menu_helpdesk_reporting" name="Reporting" parent="menu_helpdesk_root"
              sequence="80"/>
    <menuitem id="menu_helpdesk_analysis" name="Ticket Analysis" parent="menu_helpdesk_reporting"
              action="helpdesk_ticket_analysis_action" sequence="10"/>
```

Add `data/ir_sequence_data.xml` and `views/helpdesk_ticket_views.xml` to the manifest, then upgrade.

> The form's buttons call methods that don't exist yet. Odoo does not check this at install, but clicking them raises an error until Phase 3.

**Verify Phase 2**
* [ ] Kanban shows five columns, including empty ones (that's `group_expand`).
* [ ] A new ticket shows `New` as its number for now (real numbering arrives in Phase 3).
* [ ] The **Assigned To** dropdown only lists the team's members.
* [ ] Changing priority shows a tracking line in the chatter. Changing the customer fills the email.
* [ ] Create a Medium ticket → the SLA tab shows *Medium priority* policy and deadlines (+4h / +48h).
* [ ] Dragging a card between columns works.

---

## Phase 3 — Business logic

Append these to `HelpdeskTicket` (same class, below the compute methods).

```python
    # ---- constraints ----------------------------------------------------
    @api.constrains('stage_id', 'resolution_note')
    def _check_resolution_note(self):
        for ticket in self:
            if ticket.stage_id.require_note and not (ticket.resolution_note or '').strip():
                raise ValidationError(_(
                    "Ticket %(number)s needs a resolution note before it can move to '%(stage)s'.",
                    number=ticket.number, stage=ticket.stage_id.name))

    # ---- CRUD overrides -------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('number') or vals['number'] == 'New':
                vals['number'] = self.env['ir.sequence'].next_by_code('helpdesk.ticket') or 'New'
            # Auto-assign BEFORE creation so the record is written once.
            if not vals.get('user_id'):
                team = (self.env['helpdesk.team'].browse(vals['team_id'])
                        if vals.get('team_id') else self._default_team_id())
                assignee = team._get_next_assignee() if team else False
                if assignee:
                    vals['user_id'] = assignee.id
        tickets = super().create(vals_list)
        tickets._schedule_assignment_activity()
        for ticket in tickets.filtered('partner_id'):
            ticket.message_subscribe(partner_ids=ticket.partner_id.ids)
        tickets._send_received_email()
        return tickets

    def write(self, vals):
        vals = dict(vals)
        closing = False
        if 'stage_id' in vals:
            stage = self.env['helpdesk.stage'].browse(vals['stage_id'])
            closing = stage.is_closed
            vals['closed_date'] = fields.Datetime.now() if closing else False
        res = super().write(vals)
        if 'user_id' in vals:
            self._schedule_assignment_activity()
        if closing:
            self.activity_feedback(['mail.mail_activity_data_todo'])
        return res

    # ---- helpers --------------------------------------------------------
    def _schedule_assignment_activity(self):
        for ticket in self.filtered('user_id'):
            if ticket.user_id != self.env.user:  # don't nag yourself
                ticket.activity_schedule(
                    'mail.mail_activity_data_todo',
                    user_id=ticket.user_id.id,
                    summary=_("Handle ticket %s", ticket.number),
                    note=ticket.name)

    def _send_received_email(self):
        template = self.env.ref('mini_helpdesk.mail_template_ticket_received', raise_if_not_found=False)
        if not template:
            return
        for ticket in self.filtered('partner_email'):
            template.send_mail(ticket.id, force_send=False)

    # ---- first response tracking ---------------------------------------
    def message_post(self, **kwargs):
        message = super().message_post(**kwargs)
        is_agent_reply = (
            message.message_type == 'comment'
            and message.subtype_id == self.env.ref('mail.mt_comment')  # not an internal note
            and not self.env.user.share)                               # internal user
        if is_agent_reply:
            # raise_if_not_found=False: a manager may have deleted these stages.
            new_stage = self.env.ref('mini_helpdesk.stage_new', raise_if_not_found=False)
            progress = self.env.ref('mini_helpdesk.stage_in_progress', raise_if_not_found=False)
            for ticket in self.filtered(lambda t: not t.first_response_date):
                vals = {'first_response_date': fields.Datetime.now()}
                if new_stage and progress and ticket.stage_id == new_stage:
                    vals['stage_id'] = progress.id
                ticket.write(vals)
        return message

    # ---- buttons --------------------------------------------------------
    def action_assign_to_me(self):
        self.write({'user_id': self.env.uid})

    def action_resolve(self):
        # The constraint enforces the resolution note and shows a clear error.
        self.write({'stage_id': self.env.ref('mini_helpdesk.stage_resolved').id})

    def action_reopen(self):
        self.write({'stage_id': self.env.ref('mini_helpdesk.stage_in_progress').id})

    def action_escalate(self):
        self._escalate(_("Manually escalated by %s.", self.env.user.name))

    def _escalate(self, reason):
        for ticket in self:
            ticket.escalated = True
            ticket.message_post(body=reason, subtype_xmlid='mail.mt_note')
            leader = ticket.team_id.leader_id
            if leader:
                ticket.activity_schedule(
                    'mail.mail_activity_data_todo',
                    user_id=leader.id,
                    summary=_("Escalated: %s", ticket.number),
                    note=reason)
```

Create `data/mail_template_data.xml` now (it is referenced by `_send_received_email`, which silently skips if missing, but add it in Phase 7 properly). Until then the `raise_if_not_found=False` keeps things safe.

**Verify Phase 3**
* [ ] New ticket gets `HD-2026-00001`; the next one `…00002`.
* [ ] Assign a ticket to a *different* user → that user sees a To-Do (log in as them or check the activity icon).
* [ ] Click **Resolve** with an empty Resolution tab → error message; fill the note → ticket moves to *Resolved*, `closed_date` set, ribbon/buttons switch to **Reopen**.
* [ ] Drag a card to *Resolved* in kanban without a note → same validation error (the constraint protects every path, not only the button).
* [ ] As an **agent**, send a message from the chatter → `first_response_date` filled and stage goes *New → In Progress*. An **internal note** must *not* do that.
* [ ] Click **Escalate** → red ribbon, note in chatter, To-Do for the team leader.
* [ ] **Reopen** → `closed_date` is cleared.

---

## Phase 4 — Auto-assignment

The logic is already in `HelpdeskTeam._get_next_assignee()` and is called from `HelpdeskTicket.create()`. This phase is about **understanding and testing it**.

How it works:

| Method | Algorithm |
| --- | --- |
| `manual` | Returns nothing; tickets stay unassigned. |
| `round_robin` | Members sorted by id; picks the first member with an id greater than `last_assigned_user_id`, wrapping to the first. Stores the pick in `last_assigned_user_id`. |
| `least_loaded` | Counts each member's open tickets with one `_read_group` query and picks the minimum (ties broken by lowest id). |

Why `self.sudo().last_assigned_user_id = chosen`? Agents do not have write access on `helpdesk.team` (ACL: read only). Updating the pointer is a **system bookkeeping action**, not something the agent is allowed to do on purpose, so we elevate just that one write.

**Verify Phase 4**
1. Create 2–3 internal users, add them to the team, set the method to **Round Robin**.
2. Create 4 tickets without an assignee → assignees rotate A, B, C, A.
3. Switch to **Least Loaded**, assign several open tickets manually to A, create a new ticket → it goes to the member with the fewest open tickets.
4. Switch to **Manual** → new tickets stay unassigned.

---

## Phase 5 — Record rules

Access rights (`ir.model.access.csv`) answer **"may this group touch this model at all?"**
Record rules (`ir.rule`) answer **"which records of that model?"**

### `security/helpdesk_rules.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Agents: tickets of their teams, or assigned to them -->
    <record id="rule_ticket_agent" model="ir.rule">
        <field name="name">Helpdesk ticket: agent sees own teams</field>
        <field name="model_id" ref="model_helpdesk_ticket"/>
        <field name="domain_force">['|', ('user_id', '=', user.id), ('team_id.member_ids', 'in', [user.id])]</field>
        <field name="groups" eval="[(4, ref('group_helpdesk_user'))]"/>
    </record>

    <!-- Managers: everything -->
    <record id="rule_ticket_manager" model="ir.rule">
        <field name="name">Helpdesk ticket: manager sees all</field>
        <field name="model_id" ref="model_helpdesk_ticket"/>
        <field name="domain_force">[(1, '=', 1)]</field>
        <field name="groups" eval="[(4, ref('group_helpdesk_manager'))]"/>
    </record>

    <!-- Portal customers: only their own, read-only -->
    <record id="rule_ticket_portal" model="ir.rule">
        <field name="name">Helpdesk ticket: portal sees own</field>
        <field name="model_id" ref="model_helpdesk_ticket"/>
        <field name="domain_force">[('partner_id', '=', user.partner_id.id)]</field>
        <field name="groups" eval="[(4, ref('base.group_portal'))]"/>
        <field name="perm_write" eval="False"/>
        <field name="perm_create" eval="False"/>
        <field name="perm_unlink" eval="False"/>
    </record>

    <!-- Multi-company: global rule (no groups) -->
    <record id="rule_ticket_company" model="ir.rule">
        <field name="name">Helpdesk ticket: multi-company</field>
        <field name="model_id" ref="model_helpdesk_ticket"/>
        <field name="domain_force">['|', ('company_id', '=', False), ('company_id', 'in', company_ids)]</field>
    </record>
</odoo>
```

How rules combine (important!):

* Rules **with groups** are combined with **OR** within the user's groups. A manager is also an agent, so both rules apply → OR → sees everything.
* **Global rules** (no groups) are combined with **AND** with the rest. The multi-company rule therefore always narrows the result.
* `sudo()` and the superuser (cron, `OdooBot`) **bypass** all rules.

Add `security/helpdesk_rules.xml` to the manifest (after the CSV) and upgrade.

**Verify Phase 5**
1. Create two agents A and B, in different teams. Make a ticket in each team.
2. Log in as A (use an incognito window): only team A's tickets are visible. B likewise.
3. Assign a ticket of team B directly to A → A now sees it (the `user_id` branch of the rule).
4. As manager you see all tickets.
5. Create a portal user (Contact → *Grant portal access*) and verify later in Phase 8 that they only see their own tickets.

---

## Phase 6 — Scheduled actions (cron) and escalation

### Add the methods to `HelpdeskTicket`

```python
    # ---- cron entry points ---------------------------------------------
    @api.model
    def _cron_escalate_overdue(self):
        """Escalate open tickets whose first-response or resolution SLA is missed."""
        now = fields.Datetime.now()
        overdue = self.search([
            ('is_closed', '=', False),
            ('escalated', '=', False),
            '|',
                '&', ('first_response_date', '=', False), ('response_deadline', '<', now),
                ('resolution_deadline', '<', now),
        ])
        for ticket in overdue:
            if ticket.resolution_deadline and ticket.resolution_deadline < now:
                reason = _("Resolution SLA breached (deadline was %s).", ticket.resolution_deadline)
            else:
                reason = _("First-response SLA breached (deadline was %s).", ticket.response_deadline)
            ticket._escalate(reason)

    @api.model
    def _cron_remind_unassigned(self):
        """One reminder to the team leader for tickets unassigned for more than 1 hour."""
        limit = fields.Datetime.now() - timedelta(hours=1)
        tickets = self.search([
            ('is_closed', '=', False),
            ('user_id', '=', False),
            ('unassigned_reminded', '=', False),
            ('create_date', '<', limit),
        ])
        for ticket in tickets:
            leader = ticket.team_id.leader_id
            if leader:
                ticket.activity_schedule(
                    'mail.mail_activity_data_todo',
                    user_id=leader.id,
                    summary=_("Unassigned ticket %s", ticket.number),
                    note=_("This ticket has had no assignee for over an hour."))
            ticket.unassigned_reminded = True
```

Domain reading guide for the escalation query (Polish/prefix notation):

```text
is_closed = False
AND escalated = False
AND (  (first_response_date = False AND response_deadline < now)
       OR resolution_deadline < now )
```

### `data/ir_cron_data.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="ir_cron_helpdesk_escalate" model="ir.cron">
            <field name="name">Helpdesk: escalate SLA-breached tickets</field>
            <field name="model_id" ref="model_helpdesk_ticket"/>
            <field name="state">code</field>
            <field name="code">model._cron_escalate_overdue()</field>
            <field name="interval_number">15</field>
            <field name="interval_type">minutes</field>
            <field name="active" eval="True"/>
        </record>

        <record id="ir_cron_helpdesk_unassigned" model="ir.cron">
            <field name="name">Helpdesk: remind about unassigned tickets</field>
            <field name="model_id" ref="model_helpdesk_ticket"/>
            <field name="state">code</field>
            <field name="code">model._cron_remind_unassigned()</field>
            <field name="interval_number">1</field>
            <field name="interval_type">hours</field>
            <field name="active" eval="True"/>
        </record>
    </data>
</odoo>
```

Crons run as the superuser: record rules do **not** apply, so they see all tickets.

**Verify Phase 6** (do not wait 15 minutes!)
1. Activate developer mode → Settings → Technical → **Scheduled Actions** → open your cron → **Run Manually**.
2. Make a ticket "late" quickly: in the DB shell (or developer mode edit) set `response_deadline` to yesterday:
   ```python
   # docker compose exec <odoo> odoo shell -c /etc/odoo/odoo.conf -d <db>
   t = env['helpdesk.ticket'].browse(1)
   t.response_deadline = '2020-01-01 00:00:00'
   env['helpdesk.ticket']._cron_escalate_overdue()
   env.cr.commit()
   ```
3. Ticket is `escalated`, has a chatter note, and the team leader has a To-Do.
4. Running the cron again must **not** escalate it twice (idempotency, thanks to the `escalated` flag).

---

## Phase 7 — Email to ticket

### 7.1 Override `message_new` on `HelpdeskTicket`

Odoo's mail gateway calls `message_new` when an email arrives for an alias that targets this model. You turn the email dict into `create()` values.

```python
    @api.model
    def message_new(self, msg_dict, custom_values=None):
        defaults = {
            'name': msg_dict.get('subject') or _("No Subject"),
            'description': msg_dict.get('body'),  # the gateway already provides HTML
        }
        sender = (email_split(msg_dict.get('email_from') or '') or [False])[0]
        if sender:
            defaults['partner_email'] = sender
            partner = self.env['res.partner'].search([('email', '=ilike', sender)], limit=1)
            if partner:
                defaults['partner_id'] = partner.id
        defaults.update(custom_values or {})
        return super().message_new(msg_dict, custom_values=defaults)
```

### 7.2 Create the alias (UI, once)

Developer mode → Settings → Technical → Email → **Aliases** → New:

| Field | Value |
| --- | --- |
| Alias Name | `support` |
| Aliased Model | Helpdesk Ticket |
| Default Values | `{'team_id': <id of your team>}` |
| Alias Contact Security | Everyone |

An alias only receives real mail when an incoming mail server / `catchall` domain is configured. In development, **simulate** an incoming email from the Odoo shell:

```python
raw = b"""From: Jane Doe <jane@example.com>
To: support@example.com
Subject: Printer on floor 2 is jammed
Message-Id: <test-001@example.com>
Content-Type: text/plain; charset=utf-8

The printer shows a paper jam error and nothing I do clears it."""

env['mail.thread'].message_process('helpdesk.ticket', raw)
env.cr.commit()
```

### 7.3 Acknowledgement email — `data/mail_template_data.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="mail_template_ticket_received" model="mail.template">
            <field name="name">Helpdesk: Ticket Received</field>
            <field name="model_id" ref="model_helpdesk_ticket"/>
            <field name="subject">[{{ object.number }}] We received your request</field>
            <field name="email_from">{{ user.email_formatted }}</field>
            <field name="email_to">{{ object.partner_email }}</field>
            <field name="auto_delete" eval="True"/>
            <field name="body_html" type="html">
                <div style="margin: 0; padding: 0; font-size: 14px;">
                    <p>Hello <t t-out="object.partner_id.name or 'there'">there</t>,</p>
                    <p>
                        We received your request
                        <strong t-out="object.name">Subject</strong>
                        and registered it as <strong t-out="object.number">HD-00001</strong>.
                        A member of our team will get back to you soon.
                    </p>
                    <p>Just reply to this email to add information.</p>
                    <p>Best regards,<br/><t t-out="object.team_id.name">Support</t></p>
                </div>
            </field>
        </record>
    </data>
</odoo>
```

Odoo expressions in `mail.template` use `{{ ... }}` for **inline** fields (subject, email_to) and `t-out` / `t-if` for the HTML body.

**Verify Phase 7**
* [ ] Simulated email creates a ticket named after the subject, partner matched if a contact with that email exists, otherwise only `partner_email` is filled.
* [ ] Creating a ticket for a customer with an email queues a mail (Settings → Technical → Email → **Emails**; you will see it in *Outgoing* — in dev it won't actually be sent unless an SMTP server is configured).
* [ ] Replying to that ticket by email threads into the same ticket (same `Message-Id`/`References`).

---

## Phase 8 — Customer portal

The portal lets a customer: **list** their tickets, **open** one and chat, **create** a new one.

### `controllers/portal.py`

```python
from odoo import _, http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request
from odoo.tools import plaintext2html

from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager


class HelpdeskPortal(CustomerPortal):

    # -- counter shown on the /my home page ------------------------------
    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'ticket_count' in counters:
            Ticket = request.env['helpdesk.ticket']
            values['ticket_count'] = (
                Ticket.search_count(self._ticket_domain()) if Ticket.has_access('read') else 0)
        return values

    def _ticket_domain(self):
        return [('partner_id', '=', request.env.user.partner_id.id)]

    # -- list -------------------------------------------------------------
    @http.route(['/my/tickets', '/my/tickets/page/<int:page>'],
                type='http', auth='user', website=True)
    def my_tickets(self, page=1, **kw):
        Ticket = request.env['helpdesk.ticket']
        domain = self._ticket_domain()
        total = Ticket.search_count(domain)
        pager = portal_pager(
            url='/my/tickets', total=total, page=page, step=self._items_per_page)
        tickets = Ticket.search(
            domain, order='create_date desc',
            limit=self._items_per_page, offset=pager['offset'])
        values = self._prepare_portal_layout_values()
        values.update({'tickets': tickets, 'pager': pager, 'page_name': 'ticket'})
        return request.render('mini_helpdesk.portal_my_tickets', values)

    # -- detail -----------------------------------------------------------
    @http.route('/my/tickets/<int:ticket_id>', type='http', auth='public', website=True)
    def my_ticket(self, ticket_id, access_token=None, **kw):
        try:
            ticket_sudo = self._document_check_access('helpdesk.ticket', ticket_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my')
        values = {
            'ticket': ticket_sudo,
            'object': ticket_sudo,          # needed by the message thread widget
            'token': access_token,
            'page_name': 'ticket',
        }
        return request.render('mini_helpdesk.portal_ticket_page', values)

    # -- create -----------------------------------------------------------
    @http.route('/my/tickets/new', type='http', auth='user', website=True)
    def my_ticket_new(self, **kw):
        types = request.env['helpdesk.ticket']._fields['ticket_type'].selection
        return request.render('mini_helpdesk.portal_ticket_new', {
            'ticket_types': types, 'page_name': 'ticket'})

    @http.route('/my/tickets/submit', type='http', auth='user',
                methods=['POST'], website=True, csrf=True)
    def my_ticket_submit(self, **post):
        name = (post.get('name') or '').strip()
        if not name:
            return request.redirect('/my/tickets/new')
        Ticket = request.env['helpdesk.ticket']
        valid_types = dict(Ticket._fields['ticket_type'].selection)
        ticket_type = post.get('ticket_type') if post.get('ticket_type') in valid_types else 'question'
        # sudo(): portal users have read-only ACL on tickets. We create on their behalf
        # but force the customer to be THEM (never trust a partner id from the form).
        ticket = Ticket.sudo().create({
            'name': name,
            'description': plaintext2html(post.get('description') or ''),
            'ticket_type': ticket_type,
            'partner_id': request.env.user.partner_id.id,
        })
        return request.redirect(ticket.get_portal_url())
```

Also add to the ticket model so `portal.mixin` knows the URL:

```python
    def _compute_access_url(self):
        super()._compute_access_url()
        for ticket in self:
            ticket.access_url = f'/my/tickets/{ticket.id}'
```

### `views/helpdesk_portal_templates.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- Tile on /my -->
    <template id="portal_my_home_helpdesk" name="Show Tickets" customize_show="True"
              inherit_id="portal.portal_my_home" priority="40">
        <xpath expr="//div[hasclass('o_portal_docs')]" position="inside">
            <t t-call="portal.portal_docs_entry">
                <t t-set="title">Support Tickets</t>
                <t t-set="url" t-value="'/my/tickets'"/>
                <t t-set="placeholder_count" t-value="'ticket_count'"/>
            </t>
        </xpath>
    </template>

    <!-- List -->
    <template id="portal_my_tickets" name="My Tickets">
        <t t-call="portal.portal_layout">
            <t t-set="breadcrumbs_searchbar" t-value="False"/>
            <div class="d-flex justify-content-between align-items-center mb-3">
                <h3 class="mb-0">My Tickets</h3>
                <a href="/my/tickets/new" class="btn btn-primary">New Ticket</a>
            </div>
            <t t-if="not tickets">
                <p class="alert alert-info">You have no tickets yet.</p>
            </t>
            <t t-else="">
                <table class="table table-hover">
                    <thead>
                        <tr>
                            <th>Number</th><th>Subject</th><th>Stage</th><th>Created</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr t-foreach="tickets" t-as="ticket">
                            <td>
                                <a t-attf-href="/my/tickets/#{ticket.id}">
                                    <t t-out="ticket.number"/>
                                </a>
                            </td>
                            <td><t t-out="ticket.name"/></td>
                            <td><span class="badge text-bg-secondary" t-out="ticket.stage_id.name"/></td>
                            <td><span t-field="ticket.create_date" t-options="{'widget': 'date'}"/></td>
                        </tr>
                    </tbody>
                </table>
                <div t-if="pager" class="d-flex justify-content-center">
                    <t t-call="portal.pager"/>
                </div>
            </t>
        </t>
    </template>

    <!-- Detail -->
    <template id="portal_ticket_page" name="Ticket Portal Page">
        <t t-call="portal.portal_layout">
            <div class="mb-4">
                <h3>
                    <t t-out="ticket.number"/> — <t t-out="ticket.name"/>
                </h3>
                <p>
                    <span class="badge text-bg-primary" t-out="ticket.stage_id.name"/>
                    <span class="text-muted ms-2">
                        Opened <span t-field="ticket.create_date" t-options="{'widget': 'date'}"/>
                    </span>
                </p>
                <div t-if="ticket.description" class="mb-3" t-out="ticket.description"/>
                <div t-if="ticket.is_closed and ticket.resolution_note" class="alert alert-success">
                    <strong>Resolution:</strong> <t t-out="ticket.resolution_note"/>
                </div>
            </div>
            <div id="ticket_communication">
                <h4>Conversation</h4>
                <t t-call="portal.message_thread">
                    <t t-set="object" t-value="ticket"/>
                </t>
            </div>
        </t>
    </template>

    <!-- New ticket form -->
    <template id="portal_ticket_new" name="New Ticket">
        <t t-call="portal.portal_layout">
            <h3 class="mb-3">New Ticket</h3>
            <form action="/my/tickets/submit" method="post" class="col-lg-8 p-0">
                <input type="hidden" name="csrf_token" t-att-value="request.csrf_token()"/>
                <div class="mb-3">
                    <label class="form-label" for="name">Subject</label>
                    <input type="text" name="name" id="name" class="form-control" required="required"/>
                </div>
                <div class="mb-3">
                    <label class="form-label" for="ticket_type">Type</label>
                    <select name="ticket_type" id="ticket_type" class="form-select">
                        <option t-foreach="ticket_types" t-as="t" t-att-value="t[0]">
                            <t t-out="t[1]"/>
                        </option>
                    </select>
                </div>
                <div class="mb-3">
                    <label class="form-label" for="description">Description</label>
                    <textarea name="description" id="description" rows="6" class="form-control"/>
                </div>
                <button type="submit" class="btn btn-primary">Submit</button>
                <a href="/my/tickets" class="btn btn-link">Cancel</a>
            </form>
        </t>
    </template>
</odoo>
```

> The portal APIs change slightly between Odoo versions. If the message thread or the home tile misbehaves, open `odoo/addons/sale/controllers/portal.py` and `odoo/addons/sale/views/sale_portal_templates.xml` in your source tree and mirror how `sale.order` does it — your version of those files is the ground truth.

**Verify Phase 8**
1. Contacts → pick a customer with an email → *Action → Grant Portal Access* → set a password (or use the invitation link).
2. Log in as them → `/my` shows the **Support Tickets** tile.
3. Create a ticket via **New Ticket** → redirected to its page; internally it exists with that customer.
4. They only see **their** tickets (try another customer's ticket id in the URL → redirected to `/my`).
5. Customer posts a message in the conversation → an agent sees it in the chatter. (It must **not** set `first_response_date`; the message author is a portal user.)
6. Agent replies from the backend → the customer sees it in the portal.

---

## Phase 9 — Reports and analysis

### Pivot / Graph

Already created in Phase 2. Practice on **Reporting → Ticket Analysis**:

* Rows: Team → Assignee; Columns: Stage; Measure: Count.
* Group by *Created (month)* and switch to the line chart.

### PDF — `report/helpdesk_ticket_report.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="action_report_helpdesk_ticket" model="ir.actions.report">
        <field name="name">Ticket Summary</field>
        <field name="model">helpdesk.ticket</field>
        <field name="report_type">qweb-pdf</field>
        <field name="report_name">mini_helpdesk.report_ticket_document</field>
        <field name="report_file">mini_helpdesk.report_ticket_document</field>
        <field name="print_report_name">'Ticket-%s' % object.number</field>
        <field name="binding_model_id" ref="model_helpdesk_ticket"/>
        <field name="binding_type">report</field>
    </record>

    <template id="report_ticket_document">
        <t t-call="web.html_container">
            <t t-foreach="docs" t-as="o">
                <t t-call="web.internal_layout">
                    <div class="page">
                        <h2>Ticket <span t-field="o.number"/></h2>
                        <h4 t-field="o.name"/>
                        <table class="table table-sm mt-3">
                            <tbody>
                                <tr>
                                    <th>Customer</th><td><span t-field="o.partner_id"/></td>
                                    <th>Team</th><td><span t-field="o.team_id"/></td>
                                </tr>
                                <tr>
                                    <th>Assigned to</th><td><span t-field="o.user_id"/></td>
                                    <th>Priority</th><td><span t-field="o.priority"/></td>
                                </tr>
                                <tr>
                                    <th>Stage</th><td><span t-field="o.stage_id"/></td>
                                    <th>SLA status</th><td><span t-field="o.sla_status"/></td>
                                </tr>
                                <tr>
                                    <th>Created</th><td><span t-field="o.create_date"/></td>
                                    <th>Resolution deadline</th><td><span t-field="o.resolution_deadline"/></td>
                                </tr>
                            </tbody>
                        </table>
                        <h5>Description</h5>
                        <div t-field="o.description"/>
                        <t t-if="o.resolution_note">
                            <h5 class="mt-4">Resolution</h5>
                            <p t-field="o.resolution_note"/>
                        </t>
                    </div>
                </t>
            </t>
        </t>
    </template>
</odoo>
```

`report_name` must equal the **template's full XML id** (`module.template_id`). PDF rendering needs `wkhtmltopdf`, which the official Odoo Docker image includes. Open a ticket → **Print → Ticket Summary**.

---

## Phase 10 — Tests, demo data, polish

### `tests/__init__.py`

```python
from . import test_helpdesk_ticket
```

### `tests/test_helpdesk_ticket.py`

```python
from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHelpdeskTicket(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users']
        cls.agent_1 = Users.create({'name': 'Agent One', 'login': 'agent1@example.com'})
        cls.agent_2 = Users.create({'name': 'Agent Two', 'login': 'agent2@example.com'})
        cls.team = cls.env['helpdesk.team'].create({
            'name': 'Test Team',
            'leader_id': cls.agent_1.id,
            'member_ids': [(6, 0, [cls.agent_1.id, cls.agent_2.id])],
            'assignment_method': 'round_robin',
        })

    def _ticket(self, **vals):
        vals.setdefault('name', 'Test ticket')
        vals.setdefault('team_id', self.team.id)
        return self.env['helpdesk.ticket'].create(vals)

    def test_number_is_generated(self):
        self.assertTrue(self._ticket().number.startswith('HD-'))

    def test_sla_deadline_for_urgent(self):
        ticket = self._ticket(priority='3')
        expected = ticket.create_date + timedelta(hours=8)
        self.assertLess(abs(ticket.resolution_deadline - expected), timedelta(seconds=5))

    def test_round_robin_alternates(self):
        users = [self._ticket().user_id for _i in range(3)]
        self.assertEqual(users, [self.agent_1, self.agent_2, self.agent_1])

    def test_resolution_note_is_required(self):
        ticket = self._ticket()
        resolved = self.env.ref('mini_helpdesk.stage_resolved')
        with self.assertRaises(ValidationError), self.cr.savepoint():
            ticket.stage_id = resolved
        ticket.resolution_note = 'Replaced the cable.'
        ticket.stage_id = resolved
        self.assertTrue(ticket.closed_date)
        self.assertTrue(ticket.is_closed)

    def test_cron_escalates_once(self):
        ticket = self._ticket()
        ticket.response_deadline = fields.Datetime.now() - timedelta(hours=1)
        self.env['helpdesk.ticket']._cron_escalate_overdue()
        self.assertTrue(ticket.escalated)
        activities = ticket.activity_ids
        self.env['helpdesk.ticket']._cron_escalate_overdue()
        self.assertEqual(ticket.activity_ids, activities)  # no duplicates
```

Run with the command in Section 5.

### `demo/helpdesk_demo.xml`

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="demo_ticket_vpn" model="helpdesk.ticket">
        <field name="name">Cannot connect to VPN</field>
        <field name="partner_id" ref="base.res_partner_12"/>
        <field name="team_id" ref="team_support"/>
        <field name="priority">2</field>
        <field name="description"><![CDATA[<p>VPN client says "authentication failed" since this morning.</p>]]></field>
    </record>
    <record id="demo_ticket_invoice" model="helpdesk.ticket">
        <field name="name">Invoice PDF shows the wrong address</field>
        <field name="partner_id" ref="base.res_partner_12"/>
        <field name="team_id" ref="team_support"/>
        <field name="priority">1</field>
    </record>
    <record id="demo_ticket_outage" model="helpdesk.ticket">
        <field name="name">Website is down</field>
        <field name="partner_id" ref="base.res_partner_12"/>
        <field name="team_id" ref="team_support"/>
        <field name="priority">3</field>
        <field name="ticket_type">incident</field>
    </record>
</odoo>
```

### Polish checklist

* [ ] Add `static/description/icon.png` and `web_icon="mini_helpdesk,static/description/icon.png"` on the root menu.
* [ ] README with install steps and screenshots.
* [ ] Add a few tags in demo data.
* [ ] Read your own code once: remove unused imports (`ruff` is configured in your Odoo tree).

---

## 6. Acceptance checklist (end-to-end script)

Run this on a fresh database with demo data.

1. [ ] Install `mini_helpdesk` with no errors or warnings in the log.
2. [ ] As **Manager**: create team "Billing" with two members and Round Robin. Create tags *Billing*, *VPN*.
3. [ ] Create ticket A (priority High) for a customer → number `HD-<year>-0000X`, assignee set automatically, SLA tab shows +2h / +24h.
4. [ ] The assignee has a To-Do "Handle ticket …".
5. [ ] As that agent, reply from chatter → stage becomes *In Progress*, first response date filled. Add an internal note → nothing else changes.
6. [ ] Drag the card to *Resolved* without a note → blocked. Add a note → allowed. To-Do marked done.
7. [ ] Reopen → closed date cleared.
8. [ ] Another agent (other team) cannot see ticket A.
9. [ ] Force a missed deadline and run the cron → ticket escalated, leader notified. Run again → no duplicate.
10. [ ] Send a simulated email → ticket created, customer matched by email.
11. [ ] Portal customer sees only their tickets, creates one, replies, sees the agent's answer.
12. [ ] Pivot shows tickets by team/stage; PDF prints.
13. [ ] All 5 unit tests pass.

---

## 7. Common errors and how to fix them

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `External ID not found in the system: mini_helpdesk.xxx` | File order in the manifest, or typo in an xml id | Groups → ACL → rules → data → views → menus last. Use `module.id` for other modules. |
| `You are not allowed to access 'Helpdesk Ticket'` | Missing ACL line, or user not in a helpdesk group | Check CSV model id (`model_helpdesk_ticket`), then assign the group and refresh. |
| `Invalid view type: 'tree'` / `attrs` ignored | Pre-17 syntax | Use `<list>`; replace `attrs`/`states` with `invisible=`/`readonly=` expressions. |
| `Field 'x' does not exist` in a view | You added the field but didn't upgrade | `-u mini_helpdesk`. |
| My Python change has no effect | Server still running old code | Restart the container (Python), upgrade (XML/CSV/fields). |
| XML change in `data/*.xml` ignored | Record is under `noupdate="1"` | Delete the record or use a scratch DB; don't put views/rules in `noupdate`. |
| `KeyError: 'helpdesk.ticket'` | Model file not imported | Add it to `models/__init__.py`. |
| `ParseError … Element '<field name="…">' cannot be located in parent view` | XPath/inherit target missing | Check the parent module's current template. |
| Group/privilege error on install | Odoo 19.x differences in `res.groups.privilege` | See the note in Phase 1. |
| Cron never fires | Disabled, or not due yet | Settings → Technical → Scheduled Actions → **Run Manually**. |
| `Compute method failed to assign` | Compute path skipped a record | Make sure every branch assigns the field (use `else`). |
| `CacheMiss` / recursion in compute | Computing from a field that depends on itself | Check `@api.depends`; avoid reading the field you are assigning. |
| Emails not sent | No outgoing mail server in dev | They sit in *Technical → Emails*; configure SMTP or a local catcher (e.g. MailHog) if you want to see them. |

### Debugging habits for a new Odoo developer

* Turn on **developer mode** (`?debug=1`). Hover any field label to see its technical name and model.
* Use `odoo shell` to experiment: `env['helpdesk.ticket'].search([...])`, `.read_group`, `.mapped('user_id.name')`.
* Set a breakpoint in VS Code inside `create()` or `_cron_escalate_overdue()` — your Docker debugger attach already supports this — and step through to see the recordset in `self`.
* When stuck on "how does Odoo do X", search the `addons/project` and `addons/mail` source for a similar feature and read it.

---

## 8. Stretch goals (after everything above works)

1. **Resolve wizard** — a `TransientModel` popup asking for the resolution note instead of failing validation (teaches wizards, `target='new'`).
2. **Reopen on customer reply** — override `message_update`/`message_post` so a customer message in *Waiting on Customer* moves the ticket back to *In Progress*.
3. **Working-hours SLA** — use `resource.calendar` and `plan_hours()` instead of plain 24×7 hours.
4. **Priority bump on escalation** and a second escalation level.
5. **Customer rating** — inherit `rating.mixin`, send a satisfaction survey when resolved.
6. **Per-team email alias** — use `mail.alias.mixin` on `helpdesk.team` so each team gets its own address.
7. **Automated action** (`base.automation`) that auto-tags tickets containing "VPN" or "invoice".
8. **Team record rules** and a team-level dashboard with `read_group` counters in a kanban header.
9. **Merge duplicate tickets** action with a wizard.
10. **Website form** (`website` dependency) so non-logged-in visitors can submit a ticket.

---

## 9. Glossary

| Term | Meaning |
| --- | --- |
| Recordset | An ordered collection of records of one model; `self` in model methods. |
| ORM | Odoo's object-relational mapper — turns model classes into tables and queries. |
| Mixin | An abstract model you inherit to add behaviour (`mail.thread`, `portal.mixin`). |
| ACL | Access Control List: `ir.model.access.csv`, model-level permission per group. |
| Record rule | `ir.rule`: domain filter limiting which records a group can access. |
| Chatter | The message/log panel at the bottom of a form (from `mail.thread`). |
| Activity | A scheduled To-Do/Call/Email attached to a record (`mail.activity.mixin`). |
| Domain | A list of conditions, e.g. `[('is_closed', '=', False)]`, in prefix notation for `&`, `|`, `!`. |
| `sudo()` | Runs code with superuser rights (bypasses ACL and record rules). Use sparingly and deliberately. |
| `noupdate` | XML data loaded once and never overwritten by module upgrades. |
| XML ID | Stable name of a record: `module.record_id`. Used for references everywhere. |
