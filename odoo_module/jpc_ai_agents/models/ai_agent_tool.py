# -*- coding: utf-8 -*-
from odoo import fields, models


class AiAgentTool(models.Model):
    _name = 'jpc.ai.agent.tool'
    _description = 'Tool disponible para Agentes IA'
    _order = 'name'

    name = fields.Char(string='Nombre Técnico', required=True)
    display_name = fields.Char(string='Nombre Visible', required=True)
    description = fields.Text(string='Descripción')
    active = fields.Boolean(default=True)
    category = fields.Selection([
        ('contacts', 'Contactos'),
        ('crm', 'CRM'),
        ('sales', 'Ventas'),
        ('accounting', 'Contabilidad'),
        ('inventory', 'Inventario'),
        ('generic', 'Genérico'),
    ], string='Categoría', default='generic')
