from datetime import timedelta
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class HelpDeskTicket(models.Model):
    _name = "helpdesk.ticket"
    _description = "Helpdesk Ticket"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(string="Ticket Name", required=True)
    description = fields.Text(string="Description", required=True)

    team_id = fields.Many2one("helpdesk.team", string="Team")
    ticket_no = fields.Char(string="Ticket Number", readonly=True, copy=False)

    # SLA Deadlines
    response_deadline = fields.Datetime(
        string="Response Deadline", compute="_compute_sla_deadline", store=True
    )
    resolution_deadline = fields.Datetime(
        string="Resolution Deadline", compute="_compute_sla_deadline", store=True
    )

    # Action Tracking Dates (Standardized on resolution_date)
    response_date = fields.Datetime(string="Response Date", readonly=True)
    resolution_date = fields.Datetime(string="Resolution Date", readonly=True)

    team_user_ids = fields.Many2many(
        "res.users",
        related="team_id.user_ids",
        string="Team Users",
        readonly=True,
    )
    team_leader_id = fields.Many2one(
        "res.users",
        string="Team Leader",
        related="team_id.leader_id",
    )

    # SLA Statuses
    response_sla_status = fields.Selection(
        [
            ("on_track", "On Track"),
            ("completed_on_time", "Completed on time"),
            ("completed_late", "Completed late"),
            ("breached", "Breached"),
        ],
        string="Response SLA Status",
        compute="_compute_sla_status",
    )

    resolution_sla_status = fields.Selection(
        [
            ("on_track", "On Track"),
            ("completed_on_time", "Completed on time"),
            ("completed_late", "Completed late"),
            ("breached", "Breached"),
        ],
        string="Resolution SLA Status",
        compute="_compute_sla_status",
    )

    customer_id = fields.Many2one("res.partner", string="Customer")
    user_id = fields.Many2one(
        "res.users", string="Assigned To", default=lambda self: self.env.user
    )

    stage_id = fields.Many2one(
        "helpdesk.stage",
        string="Stage",
        default=lambda self: self.env.ref("mini_helpdesk.stage_new"),
    )
    tag_ids = fields.Many2many("helpdesk.tag", string="Tags")
    sla_id = fields.Many2one("helpdesk.sla", string="SLA")

    priority = fields.Selection(
        [
            ("low", "Low"),
            ("medium", "Medium"),
            ("high", "High"),
            ("urgent", "Urgent"),
        ],
        string="Priority",
        default="medium",
    )

    stage_is_new = fields.Boolean(compute="_compute_stage_flags")
    stage_is_in_progress = fields.Boolean(compute="_compute_stage_flags")
    stage_is_resolved = fields.Boolean(compute="_compute_stage_flags")

    # --- Computes ---

    @api.depends(
        "sla_id",
        "sla_id.response_time",
        "sla_id.resolution_time",
        "create_date",
    )
    def _compute_sla_deadline(self):
        for ticket in self:
            if not ticket.sla_id or not ticket.create_date:
                ticket.response_deadline = False
                ticket.resolution_deadline = False
            else:
                ticket.response_deadline = ticket.create_date + timedelta(
                    hours=ticket.sla_id.response_time
                )
                ticket.resolution_deadline = ticket.create_date + timedelta(
                    hours=ticket.sla_id.resolution_time
                )

    def _compute_sla_status(self):
        for ticket in self:
            now = fields.Datetime.now()

            # Response SLA evaluation
            if not ticket.response_deadline:
                ticket.response_sla_status = False

            elif ticket.response_date:
                if ticket.response_date > ticket.response_deadline:
                    ticket.response_sla_status = "completed_late"
                else:
                    ticket.response_sla_status = "completed_on_time"

            elif now > ticket.response_deadline:
                ticket.response_sla_status = "breached"
            else:
                ticket.response_sla_status = "on_track"

            # Resolution SLA evaluation
            if not ticket.resolution_deadline:
                ticket.resolution_sla_status = False

            elif ticket.resolution_date:
                if ticket.resolution_date > ticket.resolution_deadline:
                    ticket.resolution_sla_status = "completed_late"
                else:
                    ticket.resolution_sla_status = "completed_on_time"

            elif now > ticket.resolution_deadline:
                ticket.resolution_sla_status = "breached"

            else:
                ticket.resolution_sla_status = "on_track"

            if ticket.response_sla_status == "breached":
                existing_activity = ticket.acitivity_ids.filtered(
                    lambda activity: activity.summary == "Response SLA Breached"
                )
                if not existing_activity and ticket.user_id:
                    todo_type = self.env.ref("mail.mail_activity_data_todo")
                    ticket.activity_schedule(
                        activity_type_id=todo_type.id,
                        summary="Response SLA breached",
                        user_id=ticket.user_id.id,
                    )

            if ticket.resolution_sla_status == "breached":
                existing_activity = ticket.acitivity_ids.filtered(
                    lambda activity: activity.summary == "Resolution SLA Breached"
                )
                if not existing_activity and ticket.user_id:
                    todo_type = self.env.ref("mail.mail_activity_data_todo")
                    ticket.activity_schedule(
                        activity_type_id=todo_type.id,
                        summary="Resolution SLA breached",
                        user_id=ticket.user_id.id,
                    )

    @api.depends("stage_id")
    def _compute_stage_flags(self):
        new_stage = self.env.ref("mini_helpdesk.stage_new")
        progress_stage = self.env.ref("mini_helpdesk.stage_in_progress")
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")

        for ticket in self:
            ticket.stage_is_new = ticket.stage_id == new_stage
            ticket.stage_is_in_progress = ticket.stage_id == progress_stage
            ticket.stage_is_resolved = ticket.stage_id == resolved_stage

    # --- Actions ---

    def action_start_progress(self):
        for ticket in self:
            ticket.stage_id = self.env.ref("mini_helpdesk.stage_in_progress")
            if not ticket.response_date:
                ticket.response_date = fields.Datetime.now()

    def action_resolve(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        progress_stage = self.env.ref("mini_helpdesk.stage_in_progress")

        for ticket in self:
            if ticket.stage_id != progress_stage:
                raise UserError("Only tickets in progress can be resolved.")

            ticket.stage_id = resolved_stage
            if not ticket.resolution_date:
                ticket.resolution_date = fields.Datetime.now()

    def action_close(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage = self.env.ref("mini_helpdesk.stage_closed")

        for ticket in self:
            if ticket.stage_id != resolved_stage:
                raise UserError("Only resolved tickets can be closed.")

            ticket.stage_id = closed_stage

    def action_find_urgent_tickets2(self):
        progress_stage = self.env.ref("mini_helpdesk.stage_in_progress")
        tickets = self.env["helpdesk.ticket"].search(
            [("priority", "=", "urgent"), ("stage_id", "=", progress_stage.id)]
        )

        return {
            "type": "ir.actions.act_window",
            "name": "My Urgent Tickets",
            "res_model": "helpdesk.ticket",
            "view_mode": "list,form",
            "domain": [("id", "in", tickets.ids)],
        }

    @api.model
    def _cron_check_sla(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage = self.env.ref("mini_helpdesk.stage_closed")
        tickets = self.env["helpdesk.ticket"].search(
            [
                ("sla_id", "!=", False),
                ("stage_id", "not in", [resolved_stage.id, closed_stage.id]),
            ]
        )

        for ticket in tickets:
            if ticket.response_date and ticket.response_deadline < ticket.response_date:
                ticket.response_sla_status = "completed_late"
            else:
                ticket.response_sla_status = "completed_on_time"

            if (
                ticket.resolution_date
                and ticket.resolution_deadline < ticket.resolution_date
            ):
                ticket.resolution_sla_status = "breached"
            else:
                ticket.resolution_sla_status = "on_track"

    # --- Constraints & Overrides ---

    @api.constrains("stage_id", "user_id")
    def _check_closed_ticket_assignment(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage = self.env.ref("mini_helpdesk.stage_closed")

        for ticket in self:
            if (
                ticket.stage_id
                in (
                    resolved_stage,
                    closed_stage,
                )
                and not ticket.user_id
            ):
                raise ValidationError(
                    "A resolved or closed ticket must have an assigned user."
                )

    @api.onchange("team_id")
    def _onchange_team_id(self):
        if self.user_id and self.user_id not in self.team_id.user_ids:
            self.user_id = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("ticket_no"):
                vals["ticket_no"] = self.env["ir.sequence"].next_by_code(
                    "helpdesk.ticket"
                )

        tickets = super().create(vals_list)
        for ticket in tickets:
            if ticket.priority == "urgent" and ticket.description:
                ticket.description = "[URGENT] " + ticket.description
        return tickets

    def write(self, vals):
        if "ticket_no" in vals:
            raise UserError("Ticket number can't be changed.")
        return super().write(vals)

    def unlink(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage = self.env.ref("mini_helpdesk.stage_closed")

        for ticket in self:
            if ticket.stage_id in (resolved_stage, closed_stage):
                raise UserError("Resolved or closed tickets cannot be deleted.")
        return super().unlink()
