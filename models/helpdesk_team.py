from odoo import models, fields


class HelpdeskTeam(models.Model):
    _name = 'helpdesk.team'
    _description = 'Helpdesk Team'

    name = fields.Char(
        string='Team Name',
        required=True
    )

    user_ids = fields.Many2many(
        'res.users',
        string='Team Members'
    )
    
    leader_id = fields.Many2one(
    'res.users',
    string='Team Leader'
    )
    
    ticket_ids = fields.One2many(
        'helpdesk.ticket',
        'team_id',
        string='Tickets'
    )
    
