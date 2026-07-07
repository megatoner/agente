# -*- coding: utf-8 -*-
from odoo import fields, models


class AiAgentMessage(models.Model):
    _name = 'jpc.ai.agent.message'
    _description = 'Mensaje de Sesión IA'
    _order = 'create_date asc'

    session_id = fields.Many2one(
        'jpc.ai.agent.session',
        string='Sesión',
        required=True,
        ondelete='cascade'
    )
    role = fields.Selection([
        ('user', 'Usuario'),
        ('assistant', 'Asistente'),
        ('tool', 'Tool'),
        ('system', 'Sistema'),
    ], string='Rol', required=True)
    content = fields.Text(string='Contenido', required=True)
    tool_name = fields.Char(string='Tool Usada')
    tool_result = fields.Text(string='Resultado de Tool')
