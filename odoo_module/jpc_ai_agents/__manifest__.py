# -*- coding: utf-8 -*-
{
    'name': 'JPC AI Agents - Servidor Externo',
    'version': '19.0.1.0.0',
    'category': 'AI',
    'summary': 'Configuración y ejecución de agentes IA en servidor externo via VPN',
    'description': """
        Módulo bridge para conectar Odoo 19 con el servidor de agentes IA externo.
        
        Permite:
        - Configurar agentes IA con modelo, tools, system prompt y memoria
        - Ejecutar agentes remotamente via API REST sobre VPN
        - Sincronizar tools disponibles desde el servidor
        - Guardar historial de conversaciones en Odoo
        - Tracking de costos integrado con jpc_ai_models
    """,
    'author': 'JPC',
    'depends': [
        'base',
        'mail',
        'jpc_ai_models',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/jpc_ai_agents_security.xml',
        'data/ai_agent_data.xml',
        'views/ai_agent_config_views.xml',
        'views/ai_agent_session_views.xml',
        'views/ai_agent_message_views.xml',
        'views/ai_agent_tool_views.xml',
        'views/res_config_settings_views.xml',
        'views/ai_agent_menus.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
