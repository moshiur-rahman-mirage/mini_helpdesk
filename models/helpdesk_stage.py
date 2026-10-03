from odoo import models,fields

class HelpdeskStage(models.Model):
    _name="helpdesk.stage"
    _description="Helpdesk Stage"
    _order="sequence,id"
    
    name=fields.Char(
        string="Stage Name",
        required=True
    )
    
    sequence=fields.Integer(
        string="Sequence",
        default=10
    )
    fold=fields.Boolean(
        string="Folded"
    )
    is_closed=fields.Boolean(
        string="Closed Stage"
    )