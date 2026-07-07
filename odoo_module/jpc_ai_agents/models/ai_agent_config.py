# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import requests
import json
import logging

_logger = logging.getLogger(__name__)


class AiAgentConfig(models.Model):
    _name = 'jpc.ai.agent.config'
    _description = 'Configuración de Agente IA'
    _order = 'name'
    _inherit = ['mail.thread']

    name = fields.Char(string='Nombre del Agente', required=True)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Descripción')
    
    model_id = fields.Many2one(
        'jpc.ai.model',
        string='Modelo IA',
        required=True,
        domain=[('active', '=', True)],
        help='Modelo de lenguaje que usará este agente'
    )
    
    system_prompt = fields.Text(
        string='System Prompt',
        default='Eres un asistente experto en Odoo ERP. Ayudas al usuario a gestionar el sistema.',
        help='Instrucciones de sistema que definen el comportamiento del agente'
    )
    
    tool_ids = fields.Many2many(
        'jpc.ai.agent.tool',
        string='Herramientas Permitidas',
        help='Tools de Odoo que este agente puede usar'
    )
    
    memory_enabled = fields.Boolean(
        string='Memoria Activada',
        default=True,
        help='Mantiene historial de conversación entre mensajes'
    )
    
    temperature = fields.Float(
        string='Temperatura',
        default=0.2,
        help='0.0 = muy determinista, 1.0 = muy creativo'
    )
    
    max_iterations = fields.Integer(
        string='Máximo de Iteraciones',
        default=15,
        help='Límite de pasos para evitar loops infinitos'
    )
    
    user_id = fields.Many2one(
        'res.users',
        string='Responsable',
        default=lambda self: self.env.user,
        help='Usuario responsable de este agente'
    )
    
    session_ids = fields.One2many(
        'jpc.ai.agent.session',
        'agent_id',
        string='Sesiones'
    )
    
    session_count = fields.Integer(
        string='Sesiones Activas',
        compute='_compute_session_count'
    )
    
    color = fields.Integer(string='Color', default=0)
    
    @api.depends('session_ids')
    def _compute_session_count(self):
        for agent in self:
            agent.session_count = len(agent.session_ids)
    
    def action_test_agent(self):
        """Abrir wizard para probar el agente."""
        self.ensure_one()
        session = self.env['jpc.ai.agent.session'].create({
            'agent_id': self.id,
            'name': f'Test - {fields.Datetime.now()}',
        })
        return {
            'type': 'ir.actions.act_window',
            'name': 'Probar Agente',
            'res_model': 'jpc.ai.agent.session',
            'res_id': session.id,
            'view_mode': 'form',
            'target': 'current',
        }
    
    def action_open_sessions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sesiones',
            'res_model': 'jpc.ai.agent.session',
            'domain': [('agent_id', '=', self.id)],
            'view_mode': 'tree,form',
        }
    
    def _get_agent_server_url(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'jpc_ai_agents.server_url', 'http://10.200.200.1:8000'
        )
    
    def _get_agent_server_api_key(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'jpc_ai_agents.server_api_key', ''
        )
    
    def execute_agent(self, message, session_id=None, user_context=None):
        """Ejecutar agente llamando al servidor externo via API."""
        self.ensure_one()
        
        url = f"{self._get_agent_server_url()}/v1/agents/run"
        api_key = self._get_agent_server_api_key()
        
        if not api_key:
            raise UserError(_('No está configurada la API Key del servidor de agentes. Ve a Ajustes > IA > Servidor de Agentes.'))
        
        # Obtener credenciales del usuario actual para que las tools actúen con sus permisos
        odoo_context = {
            'url': self.env['ir.config_parameter'].sudo().get_param('web.base.url', ''),
            'db': self.env.cr.dbname,
            'username': self.env.user.login,
            'api_key': user_context.get('api_key') if user_context else '',
        }
        
        # Si no hay api_key del usuario, usar la del sistema (modo servicio)
        if not odoo_context['api_key']:
            # En modo servicio, el servidor usa sus propias credenciales del .env
            odoo_context = {}
        
        payload = {
            'message': message,
            'agent_config': {
                'model': f"{self.model_id.provider_id.provider_type}/{self.model_id.name}",
                'system_prompt': self.system_prompt or '',
                'tools': [t.name for t in self.tool_ids],
                'temperature': self.temperature,
                'max_iterations': self.max_iterations,
                'memory_enabled': self.memory_enabled,
            },
            'session_id': session_id or f"odoo-{self.id}-{self.env.user.id}",
            'odoo_context': odoo_context,
        }
        
        try:
            response = requests.post(
                url,
                headers={
                    'Content-Type': 'application/json',
                    'X-API-Key': api_key,
                },
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
            result = response.json()
            
            # Registrar call log
            self.env['jpc.ai.call.log'].sudo().create({
                'model_id': self.model_id.id,
                'provider_id': self.model_id.provider_id.id,
                'success': result.get('success', True),
                'input_tokens': result.get('metrics', {}).get('input_tokens', 0),
                'output_tokens': result.get('metrics', {}).get('output_tokens', 0),
                'total_tokens': result.get('metrics', {}).get('total_tokens', 0),
                'cost_usd': result.get('metrics', {}).get('cost_usd', 0.0),
                'error_message': result.get('error', '') if not result.get('success') else '',
                'context_ref': f"agent:{self.id}",
                'context_model': self._name,
                'context_record_id': self.id,
                'user_id': self.env.user.id,
            })
            
            return result
            
        except requests.exceptions.RequestException as e:
            _logger.exception("Error llamando al servidor de agentes")
            raise UserError(_('Error de conexión con el servidor de agentes: %s') % str(e))
        except Exception as e:
            _logger.exception("Error inesperado en execute_agent")
            raise UserError(_('Error ejecutando el agente: %s') % str(e))
