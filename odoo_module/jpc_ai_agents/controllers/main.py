# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json


class AiAgentsController(http.Controller):

    @http.route('/jpc_ai_agents/chat/send', type='jsonrpc', auth='user', methods=['POST'])
    def chat_send(self, session_id, message):
        """Endpoint para enviar mensaje desde la UI de Odoo."""
        session = request.env['jpc.ai.agent.session'].browse(int(session_id))
        if not session.exists():
            return {'error': 'Sesión no encontrada'}
        if session.user_id.id != request.env.user.id and not request.env.user.has_group('base.group_system'):
            return {'error': 'No tienes permiso para usar esta sesión'}
        
        result = session.action_send_message(message)
        return {
            'success': True,
            'response': result.get('output', ''),
            'tools_used': result.get('tools_used', []),
            'model_used': result.get('model_used', ''),
        }
    
    @http.route('/jpc_ai_agents/chat/history', type='jsonrpc', auth='user', methods=['POST'])
    def chat_history(self, session_id):
        """Obtener historial de mensajes de una sesión."""
        session = request.env['jpc.ai.agent.session'].browse(int(session_id))
        if not session.exists():
            return {'error': 'Sesión no encontrada'}
        
        messages = []
        for msg in session.message_ids:
            messages.append({
                'id': msg.id,
                'role': msg.role,
                'content': msg.content,
                'tool_name': msg.tool_name,
                'date': msg.create_date.isoformat(),
            })
        return {'messages': messages}
