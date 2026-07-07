# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import requests


class SyncToolsWizard(models.TransientModel):
    _name = 'jpc.ai.sync.tools.wizard'
    _description = 'Sincronizar Tools desde Servidor'

    result_message = fields.Char(string='Resultado', readonly=True)
    state = fields.Selection([
        ('start', 'Inicio'),
        ('done', 'Listo'),
    ], default='start')
    
    def action_sync_tools(self):
        """Sincronizar tools disponibles desde el servidor de agentes."""
        self.ensure_one()
        
        icp = self.env['ir.config_parameter'].sudo()
        url = icp.get_param('jpc_ai_agents.server_url', 'http://10.200.200.1:8000')
        api_key = icp.get_param('jpc_ai_agents.server_api_key', '')
        
        if not api_key:
            raise UserError(_('No está configurada la API Key del servidor.'))
        
        try:
            response = requests.get(
                f"{url}/v1/agents/tools",
                headers={'X-API-Key': api_key},
                timeout=30,
            )
            response.raise_for_status()
            tools_data = response.json()
            
            Tool = self.env['jpc.ai.agent.tool']
            created = 0
            updated = 0
            
            for tool_info in tools_data:
                existing = Tool.search([('name', '=', tool_info['name'])], limit=1)
                vals = {
                    'display_name': tool_info['name'].replace('_', ' ').title(),
                    'description': tool_info.get('description', ''),
                }
                if existing:
                    existing.write(vals)
                    updated += 1
                else:
                    vals['name'] = tool_info['name']
                    Tool.create(vals)
                    created += 1
            
            self.result_message = f"Sincronización completada: {created} creadas, {updated} actualizadas."
            self.state = 'done'
            
        except Exception as e:
            self.result_message = f"Error: {str(e)}"
            self.state = 'done'
        
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'jpc.ai.sync.tools.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
