# -*- coding: utf-8 -*-

from odoo import fields, models, api, _, tools
import babel
from odoo.exceptions import UserError
from odoo.tools import pycompat
import datetime
from werkzeug import urls
import functools, itertools

import dateutil.relativedelta as relativedelta
import logging
_logger = logging.getLogger(__name__)


def format_date(env, date, pattern=False):
    if not date:
        return ''
    try:
        return tools.format_date(env, date, date_format=pattern)
    except babel.core.UnknownLocaleError:
        return date


def format_tz(env, dt, tz=False, format=False):
    record_user_timestamp = env.user.sudo().with_context(tz=tz or env.user.sudo().tz or 'UTC')
    timestamp = fields.Datetime.from_string(dt)

    ts = fields.Datetime.context_timestamp(record_user_timestamp, timestamp)

    # Babel allows to format datetime in a specific language without change locale
    # So month 1 = January in English, and janvier in French
    # Be aware that the default value for format is 'medium', instead of 'short'
    #     medium:  Jan 5, 2016, 10:20:31 PM |   5 janv. 2016 22:20:31
    #     short:   1/5/16, 10:20 PM         |   5/01/16 22:20
    if env.context.get('use_babel'):
        # Formatting available here : http://babel.pocoo.org/en/latest/dates.html#date-fields
        from babel.dates import format_datetime
        return format_datetime(ts, format or 'medium', locale=env.context.get("lang") or 'en_US')

    if format:
        return pycompat.text_type(ts.strftime(format))
    else:
        lang = env.context.get("lang")
        langs = env['res.lang']
        if lang:
            langs = env['res.lang'].search([("code", "=", lang)])
        format_date = langs.date_format or '%B-%d-%Y'
        format_time = langs.time_format or '%I-%M %p'

        fdate = pycompat.text_type(ts.strftime(format_date))
        ftime = pycompat.text_type(ts.strftime(format_time))
        return u"%s %s%s" % (fdate, ftime, (u' (%s)' % tz) if tz else u'')

def format_amount(env, amount, currency):
    fmt = "%.{0}f".format(currency.decimal_places)
    lang = env['res.lang']._lang_get(env.context.get('lang') or 'en_US')

    formatted_amount = lang.format(fmt, currency.round(amount), grouping=True, monetary=True)\
        .replace(r' ', u'\N{NO-BREAK SPACE}').replace(r'-', u'-\N{ZERO WIDTH NO-BREAK SPACE}')

    pre = post = u''
    if currency.position == 'before':
        pre = u'{symbol}\N{NO-BREAK SPACE}'.format(symbol=currency.symbol or '')
    else:
        post = u'\N{NO-BREAK SPACE}{symbol}'.format(symbol=currency.symbol or '')

    return u'{pre}{0}{post}'.format(formatted_amount, pre=pre, post=post)

try:
    # We use a jinja2 sandboxed environment to render mako templates.
    # Note that the rendering does not cover all the mako syntax, in particular
    # arbitrary Python statements are not accepted, and not all expressions are
    # allowed: only "public" attributes (not starting with '_') of objects may
    # be accessed.
    # This is done on purpose: it prevents incidental or malicious execution of
    # Python code that may break the security of the server.
    from jinja2.sandbox import SandboxedEnvironment
    mako_template_env = SandboxedEnvironment(
        block_start_string="<%",
        block_end_string="%>",
        variable_start_string="${",
        variable_end_string="}",
        comment_start_string="<%doc>",
        comment_end_string="</%doc>",
        line_statement_prefix="%",
        line_comment_prefix="##",
        trim_blocks=True,               # do not output newline after blocks
        autoescape=True,                # XML/HTML automatic escaping
    )
    mako_template_env.globals.update({
        'str': str,
        'quote': urls.url_quote,
        'urlencode': urls.url_encode,
        'datetime': datetime,
        'len': len,
        'abs': abs,
        'min': min,
        'max': max,
        'sum': sum,
        'filter': filter,
        'reduce': functools.reduce,
        'map': map,
        'round': round,

        # dateutil.relativedelta is an old-style class and cannot be directly
        # instanciated wihtin a jinja2 expression, so a lambda "proxy" is
        # is needed, apparently.
        'relativedelta': lambda *a, **kw : relativedelta.relativedelta(*a, **kw),
    })
except ImportError:
    _logger.warning("jinja2 not available, templating features will not work!")

class eCommerceProductPreset(models.AbstractModel):
    _name = 'ecommerce.product.preset'

    ecomm_categ_selector_id= fields.Many2one('ecommerce.category.selector')
    name = fields.Char()
    ecomm_categ_id = fields.Many2one('ecommerce.category', related='ecomm_categ_selector_id.ecomm_categ_id', store=True)
    platform_id = fields.Many2one('ecommerce.platform', required=True)
    product_tmpl_ids = fields.One2many('product.template', 'ecomm_product_preset_id', readonly=True)
    product_tmpl_id = fields.Many2one('product.template', ondelete='cascade', store=True,
            compute='_compute_product_tmpl_id', inverse='_inverse_product_tmpl_id')
    ecomm_attribute_lines = fields.One2many('ecommerce.product.preset.attribute.line', 'res_id', 'Category Attributes', 
            auto_join=True, domain = lambda self: [('res_model','=', self._name)])
    ecomm_product_image_ids = fields.One2many('ecommerce.product.image', 'res_id', 'Images', 
            auto_join=True, domain = lambda self: [('res_model','=',self._name)])

    _sql_constraints = [
            ('platform_product_unique', 'unique(platform_id, product_tmpl_id)','This product preset already exists in this platform')
            ]

    @api.depends('product_tmpl_ids')
    def _compute_product_tmpl_id(self):
        for s in self:
            s.product_tmpl_id = s.product_tmpl_ids and s.product_tmpl_ids[0]

    def _inverse_product_tmpl_id(self):
        for s in self:
            s.product_tmpl_ids = (s.product_tmpl_id)

    def unlink(self):
        self = self.exists()
        self.mapped('ecomm_attribute_lines').unlink()
        self.mapped('ecomm_product_image_ids').unlink()
        return super(eCommerceProductPreset, self).unlink()

    @api.onchange('ecomm_categ_id')
    def onchange_ecomm_categ_id(self):
        if self.platform_id:
            getattr(self, '_onchange_ecomm_categ_id_{}'.format(self.platform_id.platform))()


class eCommerceProductTemplate(models.Model):
    _name = 'ecommerce.product.template'
    _description = "eCommerce Product"

    name = fields.Char()
    description = fields.Text()
    t_name = fields.Char()
    t_description = fields.Text()
    sku = fields.Char()
    shop_id = fields.Many2one('ecommerce.shop', required=True)
    platform_id = fields.Many2one('ecommerce.platform', related="shop_id.platform_id", store=True, copy=False)
    platform_item_idn = fields.Char(string=_("ID Number"),index=True, copy=False)
    product_tmpl_id = fields.Many2one('product.template')
    product_product_id = fields.Many2one('product.product', string=_("Single Variant"))
    ecomm_product_product_ids = fields.One2many('ecommerce.product.product', 'ecomm_product_tmpl_id', string=_("Variants"), copy=True, store=True)
    carrier_ids = fields.One2many('ecommerce.product.carrier', 'ecomm_product_tmpl_id', auto_join=True, string=_('Delivery Methods'))
    #add_image_ids = fields.One2many('ir.attachment', 'res_id',
    #        domain= lambda self: [('res_model', '=', self._name),('mimetype', 'ilike', 'image')],
    #        string='Add Images')
    ecomm_product_image_ids = fields.One2many('ecommerce.product.image', 'res_id', string=_("Images"),
            auto_join=True, domain = [('res_model','=','ecommerce.product.template')], copy=True)
    ecomm_variant_image_ids = fields.One2many('ecommerce.product.image', string="Variant Images", compute='compute_variant_image_ids', inverse='inverse_variant_image_ids')
    auto_update_stock = fields.Boolean()
    has_preset = fields.Boolean(compute='compute_has_preset')
    stock = fields.Integer(readonly=True)
    price = fields.Float()
    _last_info_update = fields.Datetime(string=_("Info Updated On"))
    _last_sync = fields.Datetime(strong=_("Last Sync"))
    attribute_line_ids = fields.One2many('ecommerce.product.template.attribute.line', 'ecomm_product_tmpl_id', 'Variation Attributes', copy=True)
    #_sync_res = fields.Selection([('fail',_("Fail")),('success',_("Success"))], string=_("Sync Result"))
    t_product_tmpl_id = fields.Integer()

    model_object_field = fields.Many2one('ir.model.fields', string="Field")
    sub_object = fields.Many2one('ir.model', 'Sub-model', readonly=True,
                                 help="When a relationship field is selected as first field, "
                                      "this field shows the document model the relationship goes to.")
    sub_model_object_field = fields.Many2one('ir.model.fields', 'Sub-field',
                                             help="When a relationship field is selected as first field, "
                                                  "this field lets you select the target field within the "
                                                  "destination document model (sub-model).")
    null_value = fields.Char('Default Value', help="Optional value to use if the target field is empty")
    copy_value = fields.Char('Placeholder Expression', help="Final placeholder expression, to be copy-pasted in the desired template field.")
    lang = fields.Char('Language',
                       help="Optional translation language (ISO code) to select when sending out an email. "
                            "If not set, the english version will be used. "
                            "This should usually be a placeholder expression "
                            "that provides the appropriate language, e.g. "
                            "${object.partner_id.lang}.",
                       placeholder="${object.partner_id.lang}")
    
    def build_expression(self, field_name, sub_field_name, null_value):
        expression = ''
        if field_name:
            expression = "${object." + field_name
            if sub_field_name:
                expression += "." + sub_field_name
            if null_value:
                expression += " or '''%s'''" % null_value
            expression += "}"
        return expression

    @api.onchange('model_object_field', 'sub_model_object_field', 'null_value')
    def onchange_sub_model_object_value_field(self):
        if self.model_object_field:
            if self.model_object_field.ttype in ['many2one', 'one2many', 'many2many']:
                model = self.env['ir.model']._get(self.model_object_field.relation)
                if model:
                    self.sub_object = model.id
                    self.copy_value = self.build_expression(self.model_object_field.name, self.sub_model_object_field and self.sub_model_object_field.name or False, self.null_value or False)
            else:
                self.sub_object = False
                self.sub_model_object_field = False
                self.copy_value = self.build_expression(self.model_object_field.name, False, self.null_value or False)
        else:
            self.sub_object = False
            self.copy_value = False
            self.sub_model_object_field = False
            self.null_value = False

    def _render_template(self, template_txt):
        self.ensure_one()
        try:
            template = mako_template_env.from_string(tools.ustr(template_txt))
        except Exception:
            _logger.info("Failed to load template %r", template_txt, exc_info=True)
            raise UserError(_("Failed to load template %r")% template)

        # prepare template variables
        variables = {
            'format_date': lambda date, format=False, context=self._context: format_date(self.env, date, format),
            'format_tz': lambda dt, tz=False, format=False, context=self._context: format_tz(self.env, dt, tz, format),
            'format_amount': lambda amount, currency, context=self._context: format_amount(self.env, amount, currency),
            'user': self.env.user,
            'ctx': self._context,  # context kw would clash with mako internals
        }
        variables['object'] = self
        try:
            render_result = template.render(variables)
        except Exception:
            _logger.info("Failed to render template %r using values %r" % (template, variables), exc_info=True)
            raise UserError(_("Failed to render template %r using values %r")% (template, variables))

        return render_result

    def get_template(self):
        self.ensure_one()
        lang =  self._render_template(self.lang)
        if lang:
            template = self.with_context(lang=lang)
        else:
            template = self
        return template

    def generate_values(self, fields=None):
        self.ensure_one()
        if fields is None: 
            fields = ['t_name','t_description']
        template = self.get_template()
        
        return {field[2:]: template._render_template(getattr(template, field)) for field in fields}

    def preview(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "ecommerce.product.preview",
            "views": [[False, "form"]],
            "res_id": self.env['ecommerce.product.preview'].create(self.generate_values()).id,
            "target": "new",
        }

    def update_info(self, context=None, data={}, image=False):
        for p in self:
            #if image: getattr(p, '_update_image_{}'.format(p.platform_id.platform))()
            getattr(p, "_update_info_{}".format(p.platform_id.platform))(data=data)

    def add_to_shop(self, context=None, data=None):
        for p in self:
            getattr(p, '_add_to_shop_{}'.format(p.platform_id.platform))(data=data)

    def update_stock(self):
        if self:
            platform_id = self.mapped('platform_id')
            platform_id.ensure_one()
            getattr(self, "_update_stock_{}".format(platform_id.platform))()

    @api.model
    def cron_update_stock(self):
        for shop in self.env['ecommerce.shop'].search([('auto_sync','=',True)]):
            self.env['ecommerce.product.template'].search([('shop_id','=', shop.id),('auto_update_stock','=',True)]).update_stock()

    def sync_and_match(self):
        self.sync_info()
        self.match_sku()

    def sync_info(self):
        for p in self:
            getattr(p, '_sync_info_{}'.format(p.platform_id.platform))()

    def match_sku(self):
        for item in self:
            if item.ecomm_product_product_ids.filtered('sku'):
                item.product_product_id = False
                if item.product_tmpl_id and item.product_tmpl_id.active:
                    d = {}
                    for v in item.ecomm_product_product_ids:
                        if v.sku:
                            d.update({v: self.env['product.product'].search([
                                ('product_tmpl_id','=',item.product_tmpl_id.id),
                                ('default_code','=',v.sku)])[:1].id
                            })
                    if all(d.values()):
                        for v in d:
                            v.write({'product_product_id': d[v]})
                    else:
                        item.write({
                            'product_tmpl_id': False,
                            'ecomm_product_product_ids': [(1, v.id, {'product_product_id': False}) for v in item.ecomm_product_product_ids]
                        })
                else:
                    item.product_tmpl_id = self.env['product.template'].search([
                        ('product_variant_ids.default_code','in',[v.sku]) for v in item.ecomm_product_product_ids if v.sku
                    ])[:1]
                    item.write({
                        'ecomm_product_product_ids': [(1, v.id, {
                            'product_product_id': self.env['product.product'].search([
                                ('product_tmpl_id','=',item.product_tmpl_id.id),
                                ('default_code','=',v.sku)
                            ])[:1].id}) for v in item.ecomm_product_product_ids if v.sku]
                        })
            elif item.sku:
                if not item.product_product_id or not item.product_product_id.active or item.product_product_id.default_code != item.sku:
                    p = self.env['product.product'].search([
                        ('default_code','=',item.sku)])
                    item.write({
                        'product_product_id': p and p[0].id,
                        'product_tmpl_id': p and p[0].product_tmpl_id.id
                    })
            else:
                item.write({
                    'product_tmpl_id': False,
                    'product_product_id': False,
                    'ecomm_product_product_ids': [(1, p.id, {'product_product_id': False}) for p in item.ecomm_product_product_ids]
                })
            if item.platform_item_idn and item.product_tmpl_id and not item.product_tmpl_id['{}_product_preset_id'.format(item.platform_id.platform)]:
                item.make_preset()

#    @api.onchange('product_tmpl_id', 'product_product_id', 'ecomm_product_product_ids')
#    def onchange_product_id(self):
#        if self.platform_id:
#            getattr(self, '_onchange_product_id_{}'.format(self.platform_id.platform))()
#        elif self._context.get('platform'):
#            getattr(self, '_onchange_product_id_{}'.format(self._context.get('platform')))()

    @api.onchange('shop_id')
    def onchange_shop_id(self):
        if self.shop_id:
            getattr(self, '_onchange_shop_id_{}'.format(self.platform_id.platform))()
    
    #@api.onchange()
    #def load_demo_value(self):
    #    self.ensure_one()
    #    preset = self.product_tmpl_id and self.product_tmpl_id.mapped('{}_product_preset_id'.format(self.platform_id.platform))
    #    if not preset: return
    #    
    #    return {'value':{
    #        'name': self.ecomm_product_preset_id.name,
    #        'description': self.ecomm_product_preset_id.description,
    #        'ecomm_product_image_ids': [(5, _,_)] + [(0, _,{
    #            'res_model': 'ecommerce.product.template',
    #            'image_url': i.image_url,
    #        }) for i in preset.ecomm_product_image_ids]
    #    }}

    @api.depends('product_tmpl_id', 'platform_id')
    def compute_has_preset(self):
        for i in self:
            if i.product_tmpl_id and i.platform_id and i.product_tmpl_id.mapped('{}_product_preset_id'.format(i.platform_id.platform)): 
                i.has_preset = True
            else:
                i.has_preset = False

    def load_preset(self):
        self.ensure_one()
        getattr(self, '_load_preset_{}'.format(self.platform_id.platform))()

    def make_preset(self):
        for p in self:
            if p.platform_id: 
                getattr(p,'_make_preset_{}'.format(p.platform_id.platform))()

    def calculate_stock(self, default=1000):
        for p in self:
            if not p.product_tmpl_id or not p.product_product_id:
                continue
            elif (p.product_product_id.type == 'product' or p.product_product_id.pack_ok == True and 'product' in p.product_product_id.mapped('pack_line_ids.product_id.type')) and p.product_product_id.inventory_availability not in [False, 'never']:
                p.stock = p.product_product_id.virtual_available > 0 and p.product_product_id.virtual_available or 0
            else:
                p.stock = default

    @api.onchange('attribute_line_ids')
    def update_variant_ids(self):
        Product = self.env['ecommerce.product.product']
        variants_to_create = []
        variants_to_unlink = self.ecomm_product_product_ids
        
        value_list = [line.line_value_ids for line in self.attribute_line_ids]
        if any(value_list):
            combinations = itertools.product(*[line.line_value_ids for line in self.attribute_line_ids])
            exist_variants = {v.attr_line_value_ids: v for v in self.ecomm_product_product_ids}

            for comb_tuple in combinations:
                comb = self.env['ecommerce.product.template.attribute.line.value'].concat(*comb_tuple)
                if comb in exist_variants:
                    variants_to_unlink -= exist_variants[comb]
                else:
                    variants_to_create.append({
                        'attr_line_value_ids': [(6, 0, comb.ids)],
                        'name': ', '.join(comb.mapped('name'))
                    })
        if len(variants_to_create) > 1000:
            raise UserError(_('The number of variants to generate is too high. '))
        triplets = []
        if variants_to_unlink:
            triplets += [(2, v.id, 0) for v in variants_to_unlink]
        if variants_to_create:
            triplets += [(0, 0, vals) for vals in variants_to_create]
        if triplets:
            return {'value': {
                'ecomm_product_product_ids': triplets
            }}
        else: 
            return {}


    @api.depends('attribute_line_ids.line_value_ids.ecomm_product_image_ids')
    def compute_variant_image_ids(self):
        for rec in self:
            rec.ecomm_variant_image_ids = rec.mapped('attribute_line_ids.line_value_ids.ecomm_product_image_ids')

    def inverse_variant_image_ids(self):
        return

class eCommerceProductProduct(models.Model):
    _name = 'ecommerce.product.product'
    _description = "eCommerce Product Variant"
    _order = 'ecomm_product_tmpl_id, index'

    name = fields.Char()
    platform_variant_idn = fields.Char(index=True, readonly=True)
    product_product_id = fields.Many2one('product.product')
    ecomm_product_tmpl_id = fields.Many2one('ecommerce.product.template', ondelete='cascade', required=True)
    sku = fields.Char()
    price = fields.Float()
    stock = fields.Integer(readonly=True)
    attr_line_value_ids = fields.Many2many('ecommerce.product.template.attribute.line.value', relation='ecomm_product_ecomm_tmpl_attr_line_value_rel')
    index = fields.Char(compute='compute_index', store=True)


    @api.depends('attr_line_value_ids')
    def compute_index(self):
        for p in self:
            p.index = '[{}]'.format(', '.join(p.attr_line_value_ids.mapped(lambda r: '{:02d}'.format(r.sequence))))


    @api.onchange('product_product_id')
    def onchange_product_product_id(self):
        if not self.platform_variant_idn: self.sku = self.product_product_id.default_code
        if not self.price: self.price = self.product_product_id.lst_price

    def calculate_stock(self, default=1000):
        for v in self:
            if not v.product_product_id:
                continue
            elif (v.product_product_id.type == 'product' or v.product_product_id.pack_ok == True and 'product' in v.product_product_id.mapped('pack_line_ids.product_id.type')) and v.product_product_id.inventory_availability not in [False, 'never']:
                v.stock = v.product_product_id.virtual_available > 0 and v.product_product_id.virtual_available or 0
            else: 
                v.stock = default


class eCommerceProductImage(models.Model):
    _name = 'ecommerce.product.image'
    _description = 'eCommerce Product Image'
    _order = 'sequence'

    sequence = fields.Integer()
    name = fields.Char('Name')
    image_id = fields.Many2one('ir.attachment','Image Attachment')
    image_url = fields.Char('Image Url', compute='compute_image_url', inverse='inverse_image_url', store=True, copy=True)
    image_url_view = fields.Char('Image', related='image_url')
    image = fields.Binary('Image', attachment=True)
    res_id = fields.Integer()
    res_model = fields.Char()
    res_field = fields.Char()
    #src_id = fields.Integer()
    #src_model = fields.Char()

    #ecomm_product_tmpl_id = fields.Many2one('ecommerce.product.template','Related Product')


    @api.depends('image')
    def compute_image_url(self):
        for i in self:
            img = self.env['ir.attachment'].sudo().search([
                ('res_model','=','ecommerce.product.image'),
                ('res_id','=',i.id),
                ('res_field', '=', 'image')
                ])[:1]
            if img:
                img.public=True
                i.image_url = self.env['ir.config_parameter'].get_param('web.base.url') + img.local_url

    def inverse_image_url(self):
        for i in self:
            i.image = False
        return

    def refresh(self):
        return

#    @api.onchange('image_url')
#    def onchange_image_url(self):
#        self.image_url_view = self.image_url
