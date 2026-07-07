# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    agent_server_url = fields.Char(
        string='URL Servidor de Agentes',
        config_parameter='jpc_ai_agents.server_url',
        default='http://10.200.200.1:8000',
        help='URL del servidor FastAPI donde corren los agentes (via VPN)'
    )
    
    agent_server_api_key = fields.Char(
        string='API Key del Servidor',
        config_parameter='jpc_ai_agents.server_api_key',
        help='API Key para autenticar llamadas al servidor de agentes'
    )
    
    @api.model
    def test_agent_connection(self):
        """Test connection to agent server."""
        import requests
        url = f"{self.agent_server_url}/v1/agents/health"
        try:
            response = requests.get(
                url,
                headers={'X-API-Key': self.agent_server_api_key or ''},
                timeout=10,
            )
            response.raise_for_status()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Conexión Exitosa',
                    'message': f"Servidor responde: {response.json()}",
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error de Conexión',
                    'message': str(e),
                    'type': 'danger',
                    'sticky': False,
                }
            }
