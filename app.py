# ============================================================
# Portal de Gestión CFDI - DIF Municipal La Paz BCS
# Módulos: Compras · Pagos · Comprobaciones · Proveedores
# ============================================================
from flask import (Flask, render_template, redirect, url_for, flash,
                   request, jsonify, abort, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user,
                         logout_user, login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from functools import wraps
import os, uuid as uuid_lib, xml.etree.ElementTree as ET, requests as req_lib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ─────────────────────── APP CONFIG ───────────────────────────
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'portal-cfdi-dev-' + uuid_lib.uuid4().hex)

db_url = os.environ.get('DATABASE_URL', 'sqlite:///portal.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024   # 10 MB

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Inicia sesión para continuar.'
login_manager.login_message_category = 'warning'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# ─────────────────────── CORREO ───────────────────────────────
MAIL_SERVER   = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
MAIL_PORT     = int(os.environ.get('MAIL_PORT', '587'))
MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
MAIL_FROM     = os.environ.get('MAIL_FROM', MAIL_USERNAME)
PORTAL_DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'web-production-8e3f9.up.railway.app')

def enviar_correo(destinatario, asunto, cuerpo_html):
    """Envía correo. Silencia errores para no bloquear el flujo."""
    if not MAIL_USERNAME or not MAIL_PASSWORD or not destinatario:
        return
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'[Portal CFDI DIF] {asunto}'
        msg['From']    = f'Portal CFDI DIF <{MAIL_FROM}>'
        msg['To']      = destinatario
        footer = ('<br><hr style="margin-top:30px">'
                  '<small style="color:#888">Portal CFDI — DIF Municipal La Paz BCS</small>')
        msg.attach(MIMEText(cuerpo_html + footer, 'html', 'utf-8'))
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=10) as s:
            s.starttls()
            s.login(MAIL_USERNAME, MAIL_PASSWORD)
            s.sendmail(MAIL_FROM, [destinatario], msg.as_string())
        print(f'✉ Correo enviado a {destinatario}: {asunto}')
    except Exception as e:
        print(f'⚠ Correo no enviado a {destinatario}: {e}')

# ─────────────────────── CATÁLOGOS ────────────────────────────
ROLES  = ['solicitante', 'supervisor', 'director', 'tesoreria', 'admin']
AREAS  = ['Dirección General', 'Administración', 'Trabajo Social',
          'Contabilidad', 'Jurídico', 'Casa de Día Santa Fe',
          'Casa de Día Amor y Esperanza', 'Programas DIF',
          'Comunicación Social', 'Tesorería', 'Recursos Humanos']
TIPO_COMPROBACION = ['fondo_revolvente', 'gasto_a_comprobar', 'viaticos']

ESTADO_BADGE = {
    'enviada':            'warning',
    'pendiente':          'warning',
    'aprobada_supervisor':'info',
    'aprobada_direccion': 'info',
    'autorizado_pago':    'success',
    'aprobada':           'success',
    'rechazada':          'danger',
}

# ─────────────────────── MODELOS ──────────────────────────────
class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuarios'
    id           = db.Column(db.Integer, primary_key=True)
    nombre       = db.Column(db.String(150), nullable=False)
    email        = db.Column(db.String(150), unique=True, nullable=False)
    password_hash= db.Column(db.String(256))
    rol          = db.Column(db.String(50), default='solicitante')
    area         = db.Column(db.String(100))
    activo       = db.Column(db.Boolean, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def puede(self, accion):
        permisos = {
            'admin':      ['todo'],
            'tesoreria':  ['autorizar_pago', 'autorizar_comprobacion', 'ver_todo',
                           'gestionar_proveedores'],
            'director':   ['autorizar_compra', 'autorizar_pago', 'ver_todo'],
            'supervisor': ['autorizar_compra', 'autorizar_comprobacion'],
            'solicitante':[]
        }
        p = permisos.get(self.rol, [])
        return 'todo' in p or accion in p


class Proveedor(db.Model):
    __tablename__ = 'proveedores'
    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(250), nullable=False)
    rfc        = db.Column(db.String(13), unique=True, nullable=False)
    banco      = db.Column(db.String(100))
    clabe      = db.Column(db.String(18))
    email      = db.Column(db.String(150))
    telefono   = db.Column(db.String(20))
    activo     = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('usuarios.id'))


class SolicitudCompra(db.Model):
    __tablename__ = 'solicitudes_compra'
    id                = db.Column(db.Integer, primary_key=True)
    folio             = db.Column(db.String(20), unique=True, nullable=False)
    fecha             = db.Column(db.DateTime, default=datetime.utcnow)
    solicitante_id    = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    area              = db.Column(db.String(100))
    descripcion       = db.Column(db.Text, nullable=False)
    justificacion     = db.Column(db.Text)
    monto_estimado    = db.Column(db.Float, default=0)
    proveedor_sugerido= db.Column(db.String(250))
    archivo_path      = db.Column(db.String(500))
    estado            = db.Column(db.String(50), default='enviada')
    notas             = db.Column(db.Text)

    solicitante   = db.relationship('Usuario', foreign_keys=[solicitante_id])
    autorizaciones= db.relationship(
        'Autorizacion',
        primaryjoin="and_(Autorizacion.tipo_solicitud=='compra', "
                    "foreign(Autorizacion.solicitud_id)==SolicitudCompra.id)",
        order_by='Autorizacion.fecha')


class SolicitudPago(db.Model):
    __tablename__ = 'solicitudes_pago'
    id             = db.Column(db.Integer, primary_key=True)
    folio          = db.Column(db.String(20), unique=True, nullable=False)
    fecha          = db.Column(db.DateTime, default=datetime.utcnow)
    solicitante_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    proveedor_id   = db.Column(db.Integer, db.ForeignKey('proveedores.id'), nullable=True)
    # CFDI
    cfdi_uuid      = db.Column(db.String(36))
    cfdi_xml_path  = db.Column(db.String(500))
    rfc_emisor     = db.Column(db.String(13))
    nombre_emisor  = db.Column(db.String(250))
    rfc_receptor   = db.Column(db.String(13))
    subtotal       = db.Column(db.Float, default=0)
    iva            = db.Column(db.Float, default=0)
    total          = db.Column(db.Float, default=0)
    forma_pago     = db.Column(db.String(10))
    metodo_pago    = db.Column(db.String(5))
    uso_cfdi       = db.Column(db.String(10))
    fecha_cfdi     = db.Column(db.Date)
    concepto       = db.Column(db.Text)
    # Validación
    estado_sat     = db.Column(db.String(50), default='Sin verificar')
    cfdi_duplicado = db.Column(db.Boolean, default=False)
    # Workflow
    estado              = db.Column(db.String(50), default='pendiente')
    notas_solicitante   = db.Column(db.Text)
    # Enlace con solicitud de compra origen
    compra_origen_id    = db.Column(db.Integer, db.ForeignKey('solicitudes_compra.id'), nullable=True)
    # XML pendiente (compromiso 3 días)
    xml_comprometido    = db.Column(db.Boolean, default=False)
    xml_fecha_limite    = db.Column(db.DateTime, nullable=True)
    # Comprobante de pago (PDF que sube tesorería)
    comprobante_pago_pdf= db.Column(db.String(500))
    fecha_pago          = db.Column(db.DateTime, nullable=True)

    solicitante    = db.relationship('Usuario', foreign_keys=[solicitante_id])
    proveedor      = db.relationship('Proveedor')
    compra_origen  = db.relationship('SolicitudCompra', foreign_keys=[compra_origen_id])
    autorizaciones= db.relationship(
        'Autorizacion',
        primaryjoin="and_(Autorizacion.tipo_solicitud=='pago', "
                    "foreign(Autorizacion.solicitud_id)==SolicitudPago.id)",
        order_by='Autorizacion.fecha')


class Comprobacion(db.Model):
    __tablename__ = 'comprobaciones'
    id             = db.Column(db.Integer, primary_key=True)
    folio          = db.Column(db.String(20), unique=True, nullable=False)
    fecha          = db.Column(db.DateTime, default=datetime.utcnow)
    solicitante_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    tipo           = db.Column(db.String(50), default='gasto_a_comprobar')
    # CFDI
    cfdi_uuid      = db.Column(db.String(36))
    cfdi_xml_path  = db.Column(db.String(500))
    rfc_emisor     = db.Column(db.String(13))
    nombre_emisor  = db.Column(db.String(250))
    rfc_receptor   = db.Column(db.String(13))
    subtotal       = db.Column(db.Float, default=0)
    iva            = db.Column(db.Float, default=0)
    total          = db.Column(db.Float, default=0)
    fecha_cfdi     = db.Column(db.Date)
    concepto       = db.Column(db.Text)
    # Validación
    estado_sat     = db.Column(db.String(50), default='Sin verificar')
    cfdi_duplicado = db.Column(db.Boolean, default=False)
    # Workflow
    estado = db.Column(db.String(50), default='pendiente')
    notas  = db.Column(db.Text)

    solicitante   = db.relationship('Usuario', foreign_keys=[solicitante_id])
    autorizaciones= db.relationship(
        'Autorizacion',
        primaryjoin="and_(Autorizacion.tipo_solicitud=='comprobacion', "
                    "foreign(Autorizacion.solicitud_id)==Comprobacion.id)",
        order_by='Autorizacion.fecha')


class Autorizacion(db.Model):
    __tablename__  = 'autorizaciones'
    id             = db.Column(db.Integer, primary_key=True)
    tipo_solicitud = db.Column(db.String(20))   # compra | pago | comprobacion
    solicitud_id   = db.Column(db.Integer)
    autorizador_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    decision       = db.Column(db.String(20))   # aprobado | rechazado
    comentario     = db.Column(db.Text)
    fecha          = db.Column(db.DateTime, default=datetime.utcnow)
    nivel          = db.Column(db.Integer)       # 1=supervisor 2=director 3=tesoreria

    autorizador = db.relationship('Usuario')


@login_manager.user_loader
def load_user(uid):
    return Usuario.query.get(int(uid))

# ─────────────────────── HELPERS ──────────────────────────────
def generar_folio(prefix, model):
    year  = datetime.now().year
    count = model.query.filter(
        db.extract('year', model.fecha) == year).count() + 1
    return f"{prefix}-{year}-{count:04d}"


def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.rol not in roles and current_user.rol != 'admin':
                flash('No tienes permiso para realizar esta acción.', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return wrapper
    return decorator


ALLOWED = {'xml', 'pdf', 'png', 'jpg', 'jpeg'}

def allowed_file(fn):
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED


def save_upload(file, sub=''):
    if file and file.filename and allowed_file(file.filename):
        fn     = secure_filename(file.filename)
        unique = f"{uuid_lib.uuid4().hex}_{fn}"
        folder = os.path.join(app.config['UPLOAD_FOLDER'], sub)
        os.makedirs(folder, exist_ok=True)
        path   = os.path.join(folder, unique)
        file.save(path)
        return path
    return None


# ─────────────────────── CFDI HELPERS ─────────────────────────
def validar_cfdi_sat(uuid, rfc_emisor, rfc_receptor, total):
    """Consulta el servicio del SAT y devuelve estado del CFDI."""
    try:
        tt    = f"{float(total):.6f}"
        expr  = f"?re={rfc_emisor}&rr={rfc_receptor}&tt={tt}&id={uuid}"
        body  = f"""<?xml version="1.0" encoding="utf-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:tem="http://tempuri.org/">
  <soapenv:Header/>
  <soapenv:Body>
    <tem:Consulta>
      <tem:expresionImpresa>{expr}</tem:expresionImpresa>
    </tem:Consulta>
  </soapenv:Body>
</soapenv:Envelope>"""
        resp = req_lib.post(
            'https://consultaqr.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc',
            data=body.encode('utf-8'),
            headers={
                'Content-Type': 'text/xml; charset=utf-8',
                'SOAPAction':
                    'http://tempuri.org/IConsultaCFDIService/Consulta'
            },
            timeout=12
        )
        root = ET.fromstring(resp.text)
        ns   = {'c': 'http://schemas.datacontract.org/2004/07/'
                     'Sat.Cfdi.Negocio.ConsultaCfdi.Modelo'}
        estado = root.find('.//c:Estado', ns)
        es_can = root.find('.//c:EsCancelable', ns)
        return {
            'estado':        estado.text if estado is not None else 'Error',
            'es_cancelable': es_can.text if es_can is not None else '',
            'valido':        estado is not None and estado.text == 'Vigente'
        }
    except Exception as e:
        return {'estado': 'Error de conexión', 'es_cancelable': '',
                'valido': False, 'error': str(e)}


def parsear_cfdi_xml(xml_path):
    """Extrae datos clave de un XML CFDI 3.3 ó 4.0."""
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        ns33 = 'http://www.sat.gob.mx/cfd/3'
        ns40 = 'http://www.sat.gob.mx/cfd/4'
        ns   = ns40 if ns40 in root.tag else ns33

        emisor   = root.find(f'{{{ns}}}Emisor')
        receptor = root.find(f'{{{ns}}}Receptor')
        conceptos= root.find(f'{{{ns}}}Conceptos')

        # UUID desde TimbreFiscalDigital
        ns_tfd = 'http://www.sat.gob.mx/TimbreFiscalDigital'
        comp   = root.find(f'{{{ns}}}Complemento')
        uuid   = ''
        if comp is not None:
            tfd = comp.find(f'{{{ns_tfd}}}TimbreFiscalDigital')
            if tfd is not None:
                uuid = tfd.get('UUID', '')

        # Conceptos (max 3)
        desc_list = []
        if conceptos:
            for c in conceptos.findall(f'{{{ns}}}Concepto'):
                d = c.get('Descripcion', '')
                if d:
                    desc_list.append(d)

        fecha_str = root.get('Fecha', '')
        fecha_cfdi = None
        if fecha_str:
            try:
                fecha_cfdi = datetime.fromisoformat(fecha_str[:10]).date()
            except Exception:
                pass

        subtotal = float(root.get('SubTotal', 0))
        total    = float(root.get('Total', 0))
        return {
            'uuid':          uuid,
            'rfc_emisor':    emisor.get('Rfc', '')    if emisor    else '',
            'nombre_emisor': emisor.get('Nombre', '') if emisor    else '',
            'rfc_receptor':  receptor.get('Rfc', '')  if receptor  else '',
            'subtotal':      subtotal,
            'total':         total,
            'iva':           round(total - subtotal, 2),
            'forma_pago':    root.get('FormaPago', ''),
            'metodo_pago':   root.get('MetodoPago', ''),
            'uso_cfdi':      receptor.get('UsoCFDI', '') if receptor else '',
            'fecha_cfdi':    fecha_cfdi,
            'concepto':      '; '.join(desc_list[:3])
        }
    except Exception:
        return None


def cfdi_ya_existe(uuid, excluir_pago_id=None, excluir_comp_id=None):
    """Devuelve la primera solicitud donde ya aparece este UUID, o None."""
    if not uuid:
        return None
    pq = SolicitudPago.query.filter_by(cfdi_uuid=uuid).filter(
             SolicitudPago.estado != 'rechazada')
    if excluir_pago_id:
        pq = pq.filter(SolicitudPago.id != excluir_pago_id)
    found = pq.first()
    if found:
        return f"Solicitud de pago {found.folio}"

    cq = Comprobacion.query.filter_by(cfdi_uuid=uuid).filter(
             Comprobacion.estado != 'rechazada')
    if excluir_comp_id:
        cq = cq.filter(Comprobacion.id != excluir_comp_id)
    found = cq.first()
    if found:
        return f"Comprobación {found.folio}"
    return None


def _autorizar(tipo, model, id_val, url_detalle):
    """Lógica compartida de autorización."""
    obj        = model.query.get_or_404(id_val)
    decision   = request.form.get('decision')
    comentario = request.form.get('comentario', '')

    nivel_map = {'supervisor': 1, 'director': 2, 'tesoreria': 3, 'admin': 3}
    nivel = nivel_map.get(current_user.rol, 0)

    if nivel == 0:
        flash('Sin permiso para autorizar.', 'danger')
        return redirect(url_detalle)

    auth = Autorizacion(
        tipo_solicitud=tipo,
        solicitud_id=id_val,
        autorizador_id=current_user.id,
        decision=decision,
        comentario=comentario,
        nivel=nivel
    )
    db.session.add(auth)

    if decision == 'rechazado':
        obj.estado = 'rechazada'
    elif decision == 'aprobado':
        if tipo == 'pago' and nivel >= 2:
            obj.estado = 'autorizado_pago'
            obj.fecha_pago = datetime.utcnow()
        elif nivel == 1:
            obj.estado = 'aprobada_supervisor'
        else:
            obj.estado = 'aprobada'

    db.session.commit()
    _notificar(tipo, obj, decision, nivel, comentario)

    flash(
        f'Solicitud {"autorizada para pago" if obj.estado == "autorizado_pago" else obj.estado}.',
        'success' if decision == 'aprobado' else 'danger'
    )
    return redirect(url_detalle)


def _notificar(tipo, obj, decision, nivel, comentario):
    """Envía correo de notificación al solicitante según evento."""
    try:
        url_base = f'https://{PORTAL_DOMAIN}'
        solicitante = obj.solicitante
        nota = f'<p><em>Comentario: {comentario}</em></p>' if comentario else ''

        if tipo == 'compra':
            url = f'{url_base}/compras/{obj.id}'
            btn = f'<a href="{url}" style="background:#0d6efd;color:#fff;padding:10px 20px;text-decoration:none;border-radius:5px;">Ver solicitud</a>'
            if decision == 'aprobado' and nivel >= 2:
                enviar_correo(solicitante.email,
                    f'✅ Compra {obj.folio} aprobada — ya puedes solicitar el pago',
                    f'<h2>Tu solicitud de compra fue aprobada</h2>'
                    f'<p>La solicitud <strong>{obj.folio}</strong> fue aprobada por Dirección.</p>'
                    f'<p><strong>Monto estimado:</strong> ${obj.monto_estimado:,.2f}</p>'
                    f'<p>Cuando cuentes con el CFDI del proveedor, registra la '
                    f'<strong>Solicitud de Pago</strong> desde el portal.</p>'
                    f'{nota}<br>{btn}')
            elif decision == 'rechazado':
                enviar_correo(solicitante.email,
                    f'❌ Compra {obj.folio} rechazada',
                    f'<h2>Tu solicitud de compra fue rechazada</h2>'
                    f'<p>La solicitud <strong>{obj.folio}</strong> no fue autorizada.</p>'
                    f'{nota}<br>{btn}')

        elif tipo == 'pago':
            url = f'{url_base}/pagos/{obj.id}'
            btn = f'<a href="{url}" style="background:#198754;color:#fff;padding:10px 20px;text-decoration:none;border-radius:5px;">Ver solicitud</a>'
            if decision == 'aprobado' and nivel >= 3:
                enviar_correo(solicitante.email,
                    f'💳 Pago autorizado — {obj.folio}',
                    f'<h2>Tu solicitud de pago fue autorizada</h2>'
                    f'<p>La solicitud <strong>{obj.folio}</strong> fue autorizada para pago por Tesorería.</p>'
                    f'<p><strong>Total:</strong> ${obj.total:,.2f} | <strong>Emisor:</strong> {obj.nombre_emisor or "—"}</p>'
                    f'<p>Recibirás el comprobante de pago cuando Tesorería lo adjunte en el sistema.</p>'
                    f'{nota}<br>{btn}')
            elif decision == 'rechazado':
                enviar_correo(solicitante.email,
                    f'❌ Pago {obj.folio} rechazado',
                    f'<h2>Tu solicitud de pago fue rechazada</h2>'
                    f'<p>La solicitud <strong>{obj.folio}</strong> no fue autorizada.</p>'
                    f'{nota}<br>{btn}')
    except Exception as e:
        print(f'⚠ Error en notificación: {e}')


# ─────────────────────── JINJA FILTERS ────────────────────────
@app.template_filter('mxn')
def fmt_mxn(v):
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return v or '-'

@app.template_filter('fmtdate')
def fmt_date(v):
    if not v:
        return '-'
    if isinstance(v, str):
        return v[:10]
    return v.strftime('%d/%m/%Y')

app.jinja_env.globals['ESTADO_BADGE'] = ESTADO_BADGE
app.jinja_env.globals['now'] = datetime.utcnow

# ─────────────────────── AUTH ─────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').lower().strip()
        pw    = request.form.get('password', '')
        user  = Usuario.query.filter_by(email=email, activo=True).first()
        if user and user.check_password(pw):
            login_user(user, remember=True)
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Correo o contraseña incorrectos.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ─────────────────────── DASHBOARD ────────────────────────────
@app.route('/')
@login_required
def dashboard():
    es_admin = current_user.rol in ['admin', 'tesoreria', 'director', 'supervisor']
    stats = {}
    if es_admin:
        stats['compras_pendientes']        = SolicitudCompra.query.filter(
            SolicitudCompra.estado.in_(['enviada', 'aprobada_supervisor'])).count()
        stats['pagos_pendientes']          = SolicitudPago.query.filter(
            SolicitudPago.estado.in_(['pendiente', 'aprobada_supervisor'])).count()
        stats['comprobaciones_pendientes'] = Comprobacion.query.filter(
            Comprobacion.estado.in_(['pendiente', 'aprobada_supervisor'])).count()
        stats['cfdi_duplicados']           = SolicitudPago.query.filter_by(
            cfdi_duplicado=True).count() + Comprobacion.query.filter_by(
            cfdi_duplicado=True).count()
    else:
        stats['mis_compras']         = SolicitudCompra.query.filter_by(
            solicitante_id=current_user.id).count()
        stats['mis_pagos']           = SolicitudPago.query.filter_by(
            solicitante_id=current_user.id).count()
        stats['mis_comprobaciones']  = Comprobacion.query.filter_by(
            solicitante_id=current_user.id).count()

    recientes_compras = SolicitudCompra.query.order_by(
        SolicitudCompra.fecha.desc()).limit(6).all()
    recientes_pagos   = SolicitudPago.query.order_by(
        SolicitudPago.fecha.desc()).limit(6).all()

    return render_template('dashboard.html',
        stats=stats, es_admin=es_admin,
        recientes_compras=recientes_compras,
        recientes_pagos=recientes_pagos)


# ─────────────────────── COMPRAS ──────────────────────────────
@app.route('/compras')
@login_required
def compras_index():
    q = SolicitudCompra.query
    if current_user.rol == 'solicitante':
        q = q.filter_by(solicitante_id=current_user.id)
    estado = request.args.get('estado', '')
    if estado:
        q = q.filter_by(estado=estado)
    solicitudes = q.order_by(SolicitudCompra.fecha.desc()).all()
    return render_template('compras/index.html',
        solicitudes=solicitudes, filtro_estado=estado)


@app.route('/compras/nueva', methods=['GET', 'POST'])
@login_required
def compras_nueva():
    if request.method == 'POST':
        archivo_path = save_upload(request.files.get('archivo'), 'compras')
        sc = SolicitudCompra(
            folio             = generar_folio('SC', SolicitudCompra),
            solicitante_id    = current_user.id,
            area              = request.form.get('area', current_user.area),
            descripcion       = request.form.get('descripcion', ''),
            justificacion     = request.form.get('justificacion', ''),
            monto_estimado    = float(request.form.get('monto_estimado') or 0),
            proveedor_sugerido= request.form.get('proveedor_sugerido', ''),
            archivo_path      = archivo_path,
            estado            = 'enviada'
        )
        db.session.add(sc)
        db.session.commit()
        flash(f'Solicitud de compra {sc.folio} enviada correctamente.', 'success')
        return redirect(url_for('compras_index'))
    return render_template('compras/nueva.html', areas=AREAS)


@app.route('/compras/<int:id>')
@login_required
def compras_detalle(id):
    sc = SolicitudCompra.query.get_or_404(id)
    return render_template('compras/detalle.html', sc=sc)


@app.route('/compras/<int:id>/autorizar', methods=['POST'])
@login_required
def compras_autorizar(id):
    return _autorizar('compra', SolicitudCompra, id,
                      url_for('compras_detalle', id=id))


# ─────────────────────── PAGOS ────────────────────────────────
@app.route('/pagos')
@login_required
def pagos_index():
    q = SolicitudPago.query
    if current_user.rol == 'solicitante':
        q = q.filter_by(solicitante_id=current_user.id)
    estado = request.args.get('estado', '')
    if estado:
        q = q.filter_by(estado=estado)
    solicitudes = q.order_by(SolicitudPago.fecha.desc()).all()
    return render_template('pagos/index.html',
        solicitudes=solicitudes, filtro_estado=estado)


@app.route('/pagos/nueva', methods=['GET', 'POST'])
@login_required
def pagos_nueva():
    proveedores  = Proveedor.query.filter_by(activo=True).order_by(Proveedor.nombre).all()
    compra_id    = request.args.get('compra_id', type=int)
    compra_origen = SolicitudCompra.query.get(compra_id) if compra_id else None

    if request.method == 'POST':
        xml_comprometido = 'xml_comprometido' in request.form
        xml_path   = None
        datos_cfdi = {}
        xml_file   = request.files.get('xml_cfdi')
        if xml_file and xml_file.filename:
            xml_path = save_upload(xml_file, 'pagos')
            if xml_path:
                datos_cfdi = parsear_cfdi_xml(xml_path) or {}

        # Si no hay XML y no marcó compromiso, rechazar
        if not xml_path and not xml_comprometido:
            flash('Debes adjuntar el XML del CFDI o marcar el compromiso de entregarlo en 3 días.', 'danger')
            return render_template('pagos/nueva.html', proveedores=proveedores,
                                   compra_origen=compra_origen)

        uuid       = datos_cfdi.get('uuid') or request.form.get('cfdi_uuid', '')
        total      = datos_cfdi.get('total') or float(request.form.get('total') or 0)
        rfc_emisor = datos_cfdi.get('rfc_emisor', '') or request.form.get('rfc_emisor', '')
        rfc_rec    = datos_cfdi.get('rfc_receptor', '') or request.form.get('rfc_receptor', '')

        duplicado  = bool(cfdi_ya_existe(uuid)) if uuid else False
        estado_sat = 'Sin verificar'
        if uuid and rfc_emisor and rfc_rec and total:
            res        = validar_cfdi_sat(uuid, rfc_emisor, rfc_rec, total)
            estado_sat = res.get('estado', 'Error')

        fecha_limite = datetime.utcnow() + timedelta(days=3) if xml_comprometido else None

        sp = SolicitudPago(
            folio             = generar_folio('SP', SolicitudPago),
            solicitante_id    = current_user.id,
            proveedor_id      = request.form.get('proveedor_id') or None,
            compra_origen_id  = request.form.get('compra_origen_id') or None,
            cfdi_uuid         = uuid,
            cfdi_xml_path     = xml_path,
            rfc_emisor        = rfc_emisor,
            nombre_emisor     = datos_cfdi.get('nombre_emisor', ''),
            rfc_receptor      = rfc_rec,
            subtotal          = datos_cfdi.get('subtotal', 0),
            iva               = datos_cfdi.get('iva', 0),
            total             = total,
            forma_pago        = datos_cfdi.get('forma_pago', ''),
            metodo_pago       = datos_cfdi.get('metodo_pago', ''),
            uso_cfdi          = datos_cfdi.get('uso_cfdi', ''),
            fecha_cfdi        = datos_cfdi.get('fecha_cfdi'),
            concepto          = datos_cfdi.get('concepto') or request.form.get('concepto', ''),
            estado_sat        = estado_sat,
            cfdi_duplicado    = duplicado,
            estado            = 'pendiente',
            notas_solicitante = request.form.get('notas', ''),
            xml_comprometido  = xml_comprometido,
            xml_fecha_limite  = fecha_limite
        )
        db.session.add(sp)
        db.session.commit()

        if xml_comprometido:
            flash(f'Solicitud {sp.folio} creada con compromiso de XML. '
                  f'Tienes hasta el {fecha_limite.strftime("%d/%m/%Y")} para subir el XML. '
                  f'Si no lo haces, deberás reintegrar el recurso.', 'warning')
        elif duplicado:
            flash(f'⚠️ ALERTA: El CFDI {uuid[:8]}… ya existe en el sistema '
                  f'(posible duplicado). La solicitud quedó en estado pendiente.', 'danger')
        elif estado_sat == 'Cancelado':
            flash(f'⚠️ ALERTA: El CFDI está CANCELADO ante el SAT. '
                  f'No procede el pago hasta aclaración.', 'warning')
        elif estado_sat == 'Vigente':
            flash(f'Solicitud {sp.folio} creada. CFDI Vigente ante el SAT. ✓', 'success')
        else:
            flash(f'Solicitud {sp.folio} creada. Estado SAT: {estado_sat}.', 'info')
        return redirect(url_for('pagos_detalle', id=sp.id))

    return render_template('pagos/nueva.html', proveedores=proveedores,
                           compra_origen=compra_origen)


@app.route('/pagos/<int:id>')
@login_required
def pagos_detalle(id):
    sp = SolicitudPago.query.get_or_404(id)
    return render_template('pagos/detalle.html', sp=sp)


@app.route('/pagos/<int:id>/autorizar', methods=['POST'])
@login_required
def pagos_autorizar(id):
    return _autorizar('pago', SolicitudPago, id,
                      url_for('pagos_detalle', id=id))


@app.route('/pagos/<int:id>/subir-xml', methods=['POST'])
@login_required
def pagos_subir_xml(id):
    sp = SolicitudPago.query.get_or_404(id)
    if sp.solicitante_id != current_user.id and current_user.rol != 'admin':
        flash('Solo el solicitante puede subir el XML.', 'danger')
        return redirect(url_for('pagos_detalle', id=id))
    xml_file = request.files.get('xml_cfdi')
    if xml_file and xml_file.filename:
        xml_path = save_upload(xml_file, 'pagos')
        if xml_path:
            datos = parsear_cfdi_xml(xml_path) or {}
            uuid = datos.get('uuid') or sp.cfdi_uuid
            duplicado = bool(cfdi_ya_existe(uuid, excluir_pago_id=id)) if uuid else False
            if uuid and datos.get('rfc_emisor') and datos.get('rfc_receptor') and datos.get('total'):
                res = validar_cfdi_sat(uuid, datos['rfc_emisor'], datos['rfc_receptor'], datos['total'])
                sp.estado_sat = res.get('estado', 'Error')
            sp.cfdi_xml_path = xml_path
            sp.cfdi_uuid     = uuid or sp.cfdi_uuid
            sp.rfc_emisor    = datos.get('rfc_emisor', sp.rfc_emisor)
            sp.nombre_emisor = datos.get('nombre_emisor', sp.nombre_emisor)
            sp.rfc_receptor  = datos.get('rfc_receptor', sp.rfc_receptor)
            sp.subtotal      = datos.get('subtotal', sp.subtotal)
            sp.iva           = datos.get('iva', sp.iva)
            sp.total         = datos.get('total', sp.total)
            sp.forma_pago    = datos.get('forma_pago', sp.forma_pago)
            sp.metodo_pago   = datos.get('metodo_pago', sp.metodo_pago)
            sp.uso_cfdi      = datos.get('uso_cfdi', sp.uso_cfdi)
            sp.fecha_cfdi    = datos.get('fecha_cfdi', sp.fecha_cfdi)
            sp.concepto      = datos.get('concepto', sp.concepto)
            sp.cfdi_duplicado= duplicado
            sp.xml_comprometido = False
            db.session.commit()
            flash('✅ XML subido y validado correctamente.', 'success')
    else:
        flash('No se seleccionó ningún archivo XML.', 'warning')
    return redirect(url_for('pagos_detalle', id=id))


@app.route('/pagos/<int:id>/subir-comprobante', methods=['POST'])
@login_required
@require_role('tesoreria', 'admin')
def pagos_subir_comprobante(id):
    sp = SolicitudPago.query.get_or_404(id)
    pdf_file = request.files.get('comprobante_pdf')
    if pdf_file and pdf_file.filename:
        pdf_path = save_upload(pdf_file, 'comprobantes')
        if pdf_path:
            sp.comprobante_pago_pdf = pdf_path
            db.session.commit()
            # Notificar al solicitante
            url = f'https://{PORTAL_DOMAIN}/pagos/{sp.id}'
            btn = f'<a href="{url}" style="background:#0d6efd;color:#fff;padding:10px 20px;text-decoration:none;border-radius:5px;">Ver comprobante</a>'
            enviar_correo(sp.solicitante.email,
                f'📄 Comprobante de pago disponible — {sp.folio}',
                f'<h2>El comprobante de pago está disponible</h2>'
                f'<p>Tesorería adjuntó el comprobante de pago para la solicitud <strong>{sp.folio}</strong>.</p>'
                f'<p><strong>Total pagado:</strong> ${sp.total:,.2f}</p>'
                f'<p><strong>Proveedor/Emisor:</strong> {sp.nombre_emisor or "—"}</p>'
                f'<br>{btn}')
            flash('✅ Comprobante de pago adjunto. Se notificó al solicitante.', 'success')
    else:
        flash('No se seleccionó ningún archivo.', 'warning')
    return redirect(url_for('pagos_detalle', id=id))


@app.route('/pagos/<int:id>/reverificar', methods=['POST'])
@login_required
def pagos_reverificar(id):
    sp = SolicitudPago.query.get_or_404(id)
    if sp.cfdi_uuid and sp.rfc_emisor and sp.rfc_receptor and sp.total:
        res = validar_cfdi_sat(sp.cfdi_uuid, sp.rfc_emisor, sp.rfc_receptor, sp.total)
        sp.estado_sat = res.get('estado', 'Error')
        db.session.commit()
        flash(f'Re-verificación SAT: {sp.estado_sat}', 'info')
    else:
        flash('Datos CFDI insuficientes para verificar.', 'warning')
    return redirect(url_for('pagos_detalle', id=id))


# ─────────────────────── COMPROBACIONES ───────────────────────
@app.route('/comprobaciones')
@login_required
def comprobaciones_index():
    q = Comprobacion.query
    if current_user.rol == 'solicitante':
        q = q.filter_by(solicitante_id=current_user.id)
    estado = request.args.get('estado', '')
    if estado:
        q = q.filter_by(estado=estado)
    comps = q.order_by(Comprobacion.fecha.desc()).all()
    return render_template('comprobaciones/index.html',
        comprobaciones=comps, filtro_estado=estado)


@app.route('/comprobaciones/nueva', methods=['GET', 'POST'])
@login_required
def comprobaciones_nueva():
    if request.method == 'POST':
        xml_path   = None
        datos_cfdi = {}
        xml_file   = request.files.get('xml_cfdi')
        if xml_file and xml_file.filename:
            xml_path = save_upload(xml_file, 'comprobaciones')
            if xml_path:
                datos_cfdi = parsear_cfdi_xml(xml_path) or {}

        uuid       = datos_cfdi.get('uuid') or request.form.get('cfdi_uuid', '')
        total      = datos_cfdi.get('total') or float(request.form.get('total') or 0)
        rfc_emisor = datos_cfdi.get('rfc_emisor', '')
        rfc_rec    = datos_cfdi.get('rfc_receptor', '')

        duplicado  = bool(cfdi_ya_existe(uuid)) if uuid else False
        estado_sat = 'Sin verificar'
        if uuid and rfc_emisor and rfc_rec and total:
            res        = validar_cfdi_sat(uuid, rfc_emisor, rfc_rec, total)
            estado_sat = res.get('estado', 'Error')

        comp = Comprobacion(
            folio          = generar_folio('CR', Comprobacion),
            solicitante_id = current_user.id,
            tipo           = request.form.get('tipo', 'gasto_a_comprobar'),
            cfdi_uuid      = uuid,
            cfdi_xml_path  = xml_path,
            rfc_emisor     = rfc_emisor,
            nombre_emisor  = datos_cfdi.get('nombre_emisor', ''),
            rfc_receptor   = rfc_rec,
            subtotal       = datos_cfdi.get('subtotal', 0),
            iva            = datos_cfdi.get('iva', 0),
            total          = total,
            fecha_cfdi     = datos_cfdi.get('fecha_cfdi'),
            concepto       = datos_cfdi.get('concepto') or request.form.get('concepto', ''),
            estado_sat     = estado_sat,
            cfdi_duplicado = duplicado,
            estado         = 'pendiente',
            notas          = request.form.get('notas', '')
        )
        db.session.add(comp)
        db.session.commit()

        if duplicado:
            flash(f'⚠️ CFDI DUPLICADO detectado: UUID {uuid[:12]}… ya existe en el sistema. '
                  f'Revise antes de proceder.', 'danger')
        elif estado_sat not in ['Vigente', 'Sin verificar']:
            flash(f'⚠️ CFDI con estado SAT: {estado_sat}. Verifique.', 'warning')
        else:
            flash(f'Comprobación {comp.folio} registrada. Estado SAT: {estado_sat}', 'success')
        return redirect(url_for('comprobaciones_detalle', id=comp.id))

    return render_template('comprobaciones/nueva.html',
        tipos=TIPO_COMPROBACION)


@app.route('/comprobaciones/<int:id>')
@login_required
def comprobaciones_detalle(id):
    comp = Comprobacion.query.get_or_404(id)
    return render_template('comprobaciones/detalle.html', comp=comp)


@app.route('/comprobaciones/<int:id>/autorizar', methods=['POST'])
@login_required
def comprobaciones_autorizar(id):
    return _autorizar('comprobacion', Comprobacion, id,
                      url_for('comprobaciones_detalle', id=id))


# ─────────────────────── PROVEEDORES ──────────────────────────
@app.route('/proveedores')
@login_required
def proveedores_index():
    proveedores = Proveedor.query.order_by(Proveedor.nombre).all()
    return render_template('proveedores/index.html', proveedores=proveedores)


@app.route('/proveedores/nuevo', methods=['GET', 'POST'])
@login_required
@require_role('admin', 'tesoreria')
def proveedores_nuevo():
    if request.method == 'POST':
        rfc = request.form.get('rfc', '').upper().strip()
        if Proveedor.query.filter_by(rfc=rfc).first():
            flash('Ya existe un proveedor con ese RFC.', 'danger')
            return render_template('proveedores/form.html', prov=None)
        prov = Proveedor(
            nombre    = request.form.get('nombre', ''),
            rfc       = rfc,
            banco     = request.form.get('banco', ''),
            clabe     = request.form.get('clabe', ''),
            email     = request.form.get('email', ''),
            telefono  = request.form.get('telefono', ''),
            created_by= current_user.id
        )
        db.session.add(prov)
        db.session.commit()
        flash(f'Proveedor {prov.nombre} agregado al catálogo.', 'success')
        return redirect(url_for('proveedores_index'))
    return render_template('proveedores/form.html', prov=None)


@app.route('/proveedores/<int:id>/editar', methods=['GET', 'POST'])
@login_required
@require_role('admin', 'tesoreria')
def proveedores_editar(id):
    prov = Proveedor.query.get_or_404(id)
    if request.method == 'POST':
        prov.nombre   = request.form.get('nombre', '')
        prov.rfc      = request.form.get('rfc', '').upper().strip()
        prov.banco    = request.form.get('banco', '')
        prov.clabe    = request.form.get('clabe', '')
        prov.email    = request.form.get('email', '')
        prov.telefono = request.form.get('telefono', '')
        prov.activo   = ('activo' in request.form)
        db.session.commit()
        flash('Proveedor actualizado.', 'success')
        return redirect(url_for('proveedores_index'))
    return render_template('proveedores/form.html', prov=prov)


# ─────────────────────── ADMIN USUARIOS ───────────────────────
@app.route('/admin/usuarios')
@login_required
@require_role('admin')
def admin_usuarios():
    usuarios = Usuario.query.order_by(Usuario.nombre).all()
    return render_template('admin/usuarios.html',
        usuarios=usuarios, roles=ROLES, areas=AREAS)


@app.route('/admin/usuarios/nuevo', methods=['POST'])
@login_required
@require_role('admin')
def admin_usuario_nuevo():
    email = request.form.get('email', '').lower().strip()
    if Usuario.query.filter_by(email=email).first():
        flash('Ya existe un usuario con ese correo.', 'danger')
        return redirect(url_for('admin_usuarios'))
    u = Usuario(
        nombre = request.form.get('nombre', ''),
        email  = email,
        rol    = request.form.get('rol', 'solicitante'),
        area   = request.form.get('area', '')
    )
    u.set_password(request.form.get('password', ''))
    db.session.add(u)
    db.session.commit()
    flash(f'Usuario {u.nombre} creado correctamente.', 'success')
    return redirect(url_for('admin_usuarios'))


@app.route('/admin/usuarios/<int:id>/toggle', methods=['POST'])
@login_required
@require_role('admin')
def admin_usuario_toggle(id):
    u = Usuario.query.get_or_404(id)
    if u.id == current_user.id:
        flash('No puedes desactivar tu propia cuenta.', 'warning')
    else:
        u.activo = not u.activo
        db.session.commit()
        flash(f'Usuario {"activado" if u.activo else "desactivado"}.', 'info')
    return redirect(url_for('admin_usuarios'))


@app.route('/admin/usuarios/<int:id>/reset', methods=['POST'])
@login_required
@require_role('admin')
def admin_usuario_reset(id):
    u  = Usuario.query.get_or_404(id)
    pw = request.form.get('nueva_password', '')
    if len(pw) < 6:
        flash('La contraseña debe tener al menos 6 caracteres.', 'danger')
    else:
        u.set_password(pw)
        db.session.commit()
        flash(f'Contraseña de {u.nombre} restablecida.', 'success')
    return redirect(url_for('admin_usuarios'))


# ─────────────────────── API ──────────────────────────────────
@app.route('/api/cfdi/verificar', methods=['POST'])
@login_required
def api_cfdi_verificar():
    d         = request.get_json() or {}
    uuid      = d.get('uuid', '')
    rfc_e     = d.get('rfc_emisor', '')
    rfc_r     = d.get('rfc_receptor', '')
    total     = d.get('total', 0)
    if not uuid:
        return jsonify({'error': 'UUID requerido'}), 400
    sat       = validar_cfdi_sat(uuid, rfc_e, rfc_r, total)
    duplicado = cfdi_ya_existe(uuid)
    return jsonify({'sat': sat, 'duplicado': bool(duplicado),
                    'duplicado_en': duplicado})


# ─────────────────────── SETUP ROUTE ─────────────────────────
@app.route('/setup-db')
def setup_db_route():
    """Ruta de emergencia para crear tablas. Visitar una sola vez."""
    try:
        db.create_all()
        admin_existe = False
        try:
            admin_existe = Usuario.query.first() is not None
        except Exception:
            pass

        if not admin_existe:
            admin = Usuario(
                nombre='Administrador',
                email ='admin@dif.gob.mx',
                rol   ='admin',
                area  ='Administración'
            )
            admin.set_password('Admin2025!')
            db.session.add(admin)
            db.session.commit()
            msg = '✅ Tablas creadas y usuario admin generado. Correo: admin@dif.gob.mx | Contraseña: Admin2025!'
        else:
            msg = '✅ Tablas ya existían. Base de datos lista.'

        db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', 'desconocida')
        db_tipo = 'PostgreSQL' if 'postgresql' in db_uri else 'SQLite'
        return f'<h2>{msg}</h2><p>Base de datos: <b>{db_tipo}</b></p><p><a href="/">Ir al portal</a></p>'
    except Exception as e:
        return f'<h2>❌ Error: {str(e)}</h2><p>DATABASE_URL: {app.config.get("SQLALCHEMY_DATABASE_URI","no definida")}</p>', 500


# ─────────────────────── INIT & RUN ───────────────────────────
def init_db():
    with app.app_context():
        db.create_all()
        if not Usuario.query.first():
            admin = Usuario(
                nombre='Administrador',
                email ='admin@dif.gob.mx',
                rol   ='admin',
                area  ='Administración'
            )
            admin.set_password('Admin2025!')
            db.session.add(admin)
            db.session.commit()
            print("✓ Admin creado → admin@dif.gob.mx  /  Admin2025!")


try:
    init_db()
except Exception as e:
    print(f"⚠ init_db() falló al arrancar: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

# ─────────────────────── MIGRACIÓN DB ────────────────────────
@app.route('/migrar-db')
def migrar_db():
    """Agrega columnas nuevas a tablas existentes sin borrar datos."""
    msgs = []
    try:
        conn = db.engine.connect()
        migraciones = [
            ("solicitudes_pago", "compra_origen_id",     "INTEGER"),
            ("solicitudes_pago", "xml_comprometido",     "BOOLEAN DEFAULT FALSE"),
            ("solicitudes_pago", "xml_fecha_limite",     "TIMESTAMP"),
            ("solicitudes_pago", "comprobante_pago_pdf", "VARCHAR(500)"),
            ("solicitudes_pago", "fecha_pago",           "TIMESTAMP"),
        ]
        from sqlalchemy import text
        for tabla, columna, tipo in migraciones:
            try:
                conn.execute(text(
                    f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS {columna} {tipo}"
                ))
                conn.commit()
                msgs.append(f"✅ {tabla}.{columna}")
            except Exception as e:
                msgs.append(f"⚠ {tabla}.{columna}: {e}")
        conn.close()
        resultado = "<br>".join(msgs)
        return (f"<h2>Migración completada</h2>{resultado}"
                f"<br><br><a href='/'>Ir al portal</a>")
    except Exception as e:
        return f"<h2>Error: {e}</h2>", 500