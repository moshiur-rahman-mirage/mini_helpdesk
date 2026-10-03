from odoo import models,fields

class HelpdeskTag(models.Model):
    _name="helpdesk.tag"
    _description="Helpdesk tag"
    
    name = fields.Char(
        string='Tag Name',
        required=True
    )