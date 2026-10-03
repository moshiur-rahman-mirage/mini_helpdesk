from odoo import models, fields, api
from odoo.exceptions import UserError, ValidationError


class HelpDeskTicket(models.Model):
    _name = "helpdesk.ticket"
    _description = "Helpdesk Ticket"

    name = fields.Char(string="Ticket Name", required=True)
    description = fields.Text(string="Descriptioin", required=True)

    team_id = fields.Many2one("helpdesk.team", string="Team")

    ticket_no = fields.Char(string="Ticket Number", readonly=True, copy=False)

    team_user_ids = fields.Many2many(
        "res.users",
        related="team_id.user_ids",
        string="Team Users",
        readonly=True,
    )

    team_leader_id = fields.Many2one(
        "res.users",
        string="Team Leader",
        # compute="_compute_team_leader"
        related="team_id.leader_id",
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

    priority = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")],
        string="Priority",
        default="medium",
    )

    stage_is_new = fields.Boolean(compute="_compute_stage_flags")

    stage_is_in_progress = fields.Boolean(compute="_compute_stage_flags")

    stage_is_resolved = fields.Boolean(compute="_compute_stage_flags")

    def action_start_progress(self):
        self.stage_id = self.env.ref("mini_helpdesk.stage_in_progress")

    def action_resolve(self):
        for ticket in self:
            if ticket.stage_id != self.env.ref("mini_helpdesk.stage_in_progress"):
                raise UserError("Only tickets in progress can be resolved.")

            ticket.stage_id = self.env.ref("mini_helpdesk.stage_resolved")

    def action_close(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage = self.env.ref("mini_helpdesk.stage_closed")

        for ticket in self:
            if ticket.stage_id != resolved_stage:
                raise UserError("Only resolved tickets can be closed.")

            ticket.stage_id = closed_stage

    @api.depends("stage_id")
    def _compute_stage_flags(self):
        new_stage = self.env.ref("mini_helpdesk.stage_new")
        progress_stage = self.env.ref("mini_helpdesk.stage_in_progress")
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")

        for ticket in self:
            ticket.stage_is_new = ticket.stage_id == new_stage
            ticket.stage_is_in_progress = ticket.stage_id == progress_stage
            ticket.stage_is_resolved = ticket.stage_id == resolved_stage

    @api.constrains("stage_id", "user_id")
    def _check_closed_ticket_assignment(self):
        resolved_stage = self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage = self.env.ref("mini_helpdesk.stage_closed")

        for ticket in self:
            if ticket.stage_id in (resolved_stage, closed_stage) and not ticket.user_id:
                raise ValidationError(
                    "A resolved or closed ticket must have an assigned user."
                )

    @api.onchange("team_id")
    def _onchange_team_id(self):
        if self.user_id and self.user_id not in self.team_id.user_ids:
            self.user_id = False

    # @api.depends("team_id")
    # def _compute_team_leader(self):
    #     for ticket in self:
    #         ticket.team_leader_id=ticket.team_id.leader_id

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("ticket_no"):
                vals["ticket_no"] = self.env["ir.sequence"].next_by_code("helpdesk.ticket")

        ticket = super().create(vals_list)
        if ticket.priority == "urgent":
            ticket.description = "[URGENT] " + ticket.description
        return ticket

    def write(self,vals):
        if "ticket_no" in vals:
            raise UserError("Ticket number can't be changed.")
        result=super().write(vals)
        return result

    def unlink(self):
        resolved_stage=self.env.ref("mini_helpdesk.stage_resolved")
        closed_stage=self.env.ref("mini_helpdesk.stage_closed")

        for ticket in self:
            if ticket.stage_id in (resolved_stage,closed_stage):
                raise UserError(
                    "Resolved or closed tickets cannot be deleted."
                )
            return super().unlink()

    def action_find_urgent_tickets(self):
        progress_stage=self.env.ref("mini_helpdesk.stage_in_progress")
        tickets = self.env["helpdesk.ticket"].search(
            [("priority", "=", "urgent"), ("stage_id", "=", progress_stage.id)]
        )

        return {
            'type':'ir.actions.act_window',
            'name':'Urgent Tickets',
            'res_model':'helpdesk.ticket',
            'view_mode':'list,form',
            'domain':[('id','in',tickets.ids)]
        }
