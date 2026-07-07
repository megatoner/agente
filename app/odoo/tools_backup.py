from langchain.tools import tool
from typing import List, Dict, Any, Optional
from app.odoo.client import OdooClient, get_odoo_client


def create_odoo_tools(odoo_context: Optional[Dict[str, Any]] = None):
    """Factory that creates Odoo tools bound to a specific Odoo client.
    
    If odoo_context is provided, creates a new client with those credentials.
    Otherwise falls back to the singleton client from settings.
    """
    if odoo_context:
        odoo = OdooClient(
            url=odoo_context.get("url"),
            db=odoo_context.get("db"),
            username=odoo_context.get("username"),
            api_key=odoo_context.get("api_key"),
        )
    else:
        odoo = get_odoo_client()

    @tool
    def search_contacts(name: str = "", email: str = "", limit: int = 10) -> str:
        """Buscar contactos (res.partner) en Odoo por nombre o email."""
        domain = []
        if name:
            domain.append(("name", "ilike", name))
        if email:
            domain.append(("email", "ilike", email))
        
        partners = odoo.search_read("res.partner", domain, ["name", "email", "phone", "id"], limit)
        if not partners:
            return "No se encontraron contactos."
        lines = [f"ID: {p['id']} | Nombre: {p.get('name')} | Email: {p.get('email')} | Tel: {p.get('phone')}" for p in partners]
        return "\n".join(lines)

    @tool
    def create_contact(name: str, email: str = "", phone: str = "", company_type: str = "person") -> str:
        """Crear un nuevo contacto en Odoo. company_type puede ser 'person' o 'company'."""
        vals = {"name": name, "company_type": company_type}
        if email:
            vals["email"] = email
        if phone:
            vals["phone"] = phone
        new_id = odoo.create("res.partner", vals)
        return f"Contacto creado con ID: {new_id}"

    @tool
    def update_contact(contact_id: int, name: str = "", email: str = "", phone: str = "") -> str:
        """Actualizar un contacto existente por su ID. Solo envía los campos que quieras cambiar."""
        vals = {}
        if name:
            vals["name"] = name
        if email:
            vals["email"] = email
        if phone:
            vals["phone"] = phone
        if not vals:
            return "No se proporcionaron campos para actualizar."
        odoo.write("res.partner", [contact_id], vals)
        return f"Contacto {contact_id} actualizado correctamente."

    @tool
    def create_opportunity(name: str, partner_id: int = 0, expected_revenue: float = 0.0, description: str = "") -> str:
        """Crear una oportunidad (crm.lead) en Odoo. Si conoces el ID del contacto, pásalo en partner_id."""
        vals = {"name": name, "type": "opportunity"}
        if partner_id:
            vals["partner_id"] = partner_id
        if expected_revenue:
            vals["expected_revenue"] = expected_revenue
        if description:
            vals["description"] = description
        new_id = odoo.create("crm.lead", vals)
        return f"Oportunidad creada con ID: {new_id}"

    @tool
    def create_activity(res_model: str, res_id: int, activity_type: str, summary: str = "", note: str = "", date_deadline: str = "") -> str:
        """Crear una actividad en Odoo vinculada a un registro.
        res_model: modelo (ej: res.partner, crm.lead)
        res_id: ID del registro
        activity_type: 'email', 'call', 'meeting', 'todo', etc.
        date_deadline: formato YYYY-MM-DD
        """
        types = odoo.search_read("mail.activity.type", [[("name", "ilike", activity_type)]], ["id"], limit=1)
        if not types:
            return f"Tipo de actividad '{activity_type}' no encontrado."
        
        vals = {
            "res_model_id": odoo.execute_kw("ir.model", "search", [[("model", "=", res_model)]]),
            "res_id": res_id,
            "activity_type_id": types[0]["id"],
            "summary": summary,
            "note": note,
        }
        if date_deadline:
            vals["date_deadline"] = date_deadline
        
        if not vals.get("res_model_id"):
            return f"Modelo {res_model} no encontrado."
        
        new_id = odoo.create("mail.activity", vals)
        return f"Actividad creada con ID: {new_id}"

    @tool
    def send_email(partner_ids: List[int], subject: str, body: str) -> str:
        """Enviar un correo electrónico a uno o varios contactos por su ID."""
        vals = {
            "model": "res.partner",
            "res_id": partner_ids[0] if partner_ids else 0,
            "subject": subject,
            "body": body,
            "partner_ids": [(6, 0, partner_ids)],
        }
        msg_id = odoo.create("mail.message", vals)
        return f"Mensaje creado con ID: {msg_id}. Nota: en Odoo esto crea un mensaje en el chatter."

    @tool
    def search_odoo_model(model: str, domain_json: str = "[]", fields_json: str = "[]", limit: int = 20) -> str:
        """Buscar registros en cualquier modelo de Odoo.
        model: nombre técnico del modelo (ej: 'sale.order', 'product.product')
        domain_json: lista de tuplas en formato JSON string (ej: '[["name","ilike","test"]]')
        fields_json: lista de campos en formato JSON string (ej: '["name","id"]')
        """
        import json
        domain = json.loads(domain_json)
        fields = json.loads(fields_json)
        records = odoo.search_read(model, domain, fields or None, limit)
        if not records:
            return "No se encontraron registros."
        return json.dumps(records, ensure_ascii=False, default=str)

    @tool
    def get_sale_orders(state: str = "", partner_name: str = "", limit: int = 10) -> str:
        """Obtener órdenes de venta (sale.order) filtradas por estado o cliente."""
        import json
        domain = []
        if state:
            domain.append(("state", "=", state))
        if partner_name:
            domain.append(("partner_id.name", "ilike", partner_name))
        orders = odoo.search_read("sale.order", domain, ["name", "partner_id", "amount_total", "state", "date_order"], limit)
        if not orders:
            return "No se encontraron órdenes de venta."
        return json.dumps(orders, ensure_ascii=False, default=str)

    @tool
    def get_products(name: str = "", limit: int = 10) -> str:
        """Buscar productos (product.product) por nombre."""
        import json
        domain = [("name", "ilike", name)] if name else []
        products = odoo.search_read("product.product", domain, ["name", "list_price", "qty_available", "default_code"], limit)
        if not products:
            return "No se encontraron productos."
        return json.dumps(products, ensure_ascii=False, default=str)

    @tool
    def get_invoices(status: str = "", partner_name: str = "", limit: int = 10) -> str:
        """Obtener facturas de cliente (account.move de tipo 'out_invoice')."""
        import json
        domain = [("move_type", "=", "out_invoice")]
        if status:
            domain.append(("payment_state", "=", status))
        if partner_name:
            domain.append(("partner_id.name", "ilike", partner_name))
        invoices = odoo.search_read("account.move", domain, ["name", "partner_id", "amount_total", "payment_state", "invoice_date"], limit)
        if not invoices:
            return "No se encontraron facturas."
        return json.dumps(invoices, ensure_ascii=False, default=str)

    @tool
    def create_sale_order(partner_id: int, product_lines_json: str) -> str:
        """Crear una orden de venta en Odoo.
        product_lines_json: lista de dicts con 'product_id' y 'product_uom_qty'
        ej: '[{"product_id": 1, "product_uom_qty": 2}]'
        """
        import json
        lines = json.loads(product_lines_json)
        order_lines = []
        for line in lines:
            order_lines.append((0, 0, {
                "product_id": line["product_id"],
                "product_uom_qty": line.get("product_uom_qty", 1),
            }))
        vals = {
            "partner_id": partner_id,
            "order_line": order_lines,
        }
        new_id = odoo.create("sale.order", vals)
        return f"Orden de venta creada con ID: {new_id}"

    return [
        search_contacts,
        create_contact,
        update_contact,
        create_opportunity,
        create_activity,
        send_email,
        search_odoo_model,
        get_sale_orders,
        get_products,
        get_invoices,
        create_sale_order,
    ]


def get_odoo_tools():
    """Backward-compatible: returns tools using default Odoo client."""
    return create_odoo_tools()
