from odoo import models,fields

class HelpdeskSla(models.Model):
    _name="helpdesk.sla"
    _description="Helpdesk SLA"

    name = fields.Char(string="SLA Name",required=True)
    priority = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High")],
        string="Priority",
        default="medium",
        required=True,
    )

    response_time = fields.Float(string="Response Time(Hours)", required=True)
    resolution_time = fields.Float(string="Resolution Time(Hours)", required=True)
