# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AiAgentSession(models.Model):
    _name = 'jpc.ai.agent.session'
    _description = 'Sesión de Agente IA'
    _order = 'create_date desc'
    _inherit = ['mail.thread']

    name = fields.Char(string='Sesión', required=True, default=lambda self: _('Nueva Sesión'))
    agent_id = fields.Many2one(
        'jpc.ai.agent.config',
        string='Agente',
        required=True,
        ondelete='cascade'
    )
    user_id = fields.Many2one(
        'res.users',
        string='Usuario',
        default=lambda self: self.env.user,
        required=True
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Contacto',
        help='Contacto asociado a esta sesión (opcional)'
    )
    state = fields.Selection([
        ('active', 'Activa'),
        ('closed', 'Cerrada'),
    ], string='Estado', default='active', required=True)
    
    message_ids = fields.One2many(
        'jpc.ai.agent.message',
        'session_id',
        string='Mensajes'
    )
    
    message_count = fields.Integer(
        string='Mensajes',
        compute='_compute_message_count'
    )
    
    last_message_date = fields.Datetime(
        string='Último Mensaje',
        compute='_compute_last_message'
    )
    
    @api.depends('message_ids')
    def _compute_message_count(self):
        for session in self:
            session.message_count = len(session.message_ids)
    
    @api.depends('message_ids.create_date')
    def _compute_last_message(self):
        for session in self:
            if session.message_ids:
                session.last_message_date = max(session.message_ids.mapped('create_date'))
            else:
                session.last_message_date = False
    
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nueva Sesión')) == _('Nueva Sesión'):
                agent = self.env['jpc.ai.agent.config'].browse(vals.get('agent_id'))
                vals['name'] = f"{agent.name} - {fields.Datetime.now()}"
        return super().create(vals_list)
    
    def action_close_session(self):
        self.write({'state': 'closed'})
    
    def action_reopen_session(self):
        self.write({'state': 'active'})
    
    def action_send_message(self, message_text):
        """Enviar mensaje al agente y obtener respuesta."""
        self.ensure_one()
        if self.state != 'active':
            raise UserError(_('La sesión está cerrada. Abre una nueva sesión.'))
        
        # Guardar mensaje del usuario
        self.env['jpc.ai.agent.message'].create({
            'session_id': self.id,
            'role': 'user',
            'content': message_text,
        })
        
        # Ejecutar agente
        result = self.agent_id.execute_agent(
            message=message_text,
            session_id=f"odoo-session-{self.id}",
        )
        
        # Guardar respuesta del asistente
        self.env['jpc.ai.agent.message'].create({
            'session_id': self.id,
            'role': 'assistant',
            'content': result.get('output', ''),
            'tool_name': ', '.join(result.get('tools_used', [])),
        })
        
        return result
