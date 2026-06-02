from flask import Flask, render_template, jsonify, request, redirect
from flask_apscheduler import APScheduler
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import os
import logging

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///bina_sehat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('bina_sehat')

# extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


@login_manager.unauthorized_handler
def handle_unauthorized():
    # Return JSON 401 for API/AJAX requests, otherwise redirect to login page
    try:
        if request.path.startswith('/api/') or request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'sukses': False, 'msg': 'Harus login admin'}), 401
    except Exception:
        pass
    return redirect('/login')

scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# ====================================
# KONFIGURASI POLI
# ====================================
DATA_ANTREAN = {
    'Poli Umum': {
        'kode': 'A',
        'kuota_max': 20,
        'waktu_per_pasien': 10
    },
    'Poli Anak': {
        'kode': 'B',
        'kuota_max': 20,
        'waktu_per_pasien': 12
    },
    'Poli Lansia': {
        'kode': 'C',
        'kuota_max': 20,
        'waktu_per_pasien': 15
    },
    'IGD (DARURAT)': {
        'kode': 'IGD-D',
        'kuota_max': 5,
        'waktu_per_pasien': 0
    },
    'IGD (GEJALA BERAT)': {
        'kode': 'IGD-B',
        'kuota_max': 5,
        'waktu_per_pasien': 8
    }
}

# ====================================
# KUOTA JALUR PENJAMINAN
# ====================================
# Legacy konfigurasi jalur. Pembatasan pendaftaran sekarang dihitung berdasar kuota poli,
# sehingga nilai ini tidak lagi dipakai untuk menolak pendaftaran.
KUOTA_JALUR = {
    'BPJS': 5,
    'UM': 20,
    'ASURANSI': 10
}

# ====================================
# MODEL DATABASE
# ====================================
class QueueItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nomor_antrian = db.Column(db.String(20), unique=True, nullable=False)
    nama = db.Column(db.String(120), nullable=False)
    umur = db.Column(db.Integer, nullable=False)
    jalur = db.Column(db.String(20), nullable=False)
    poli = db.Column(db.String(50), nullable=False)
    keluhan = db.Column(db.String(500), nullable=False)
    is_darurat = db.Column(db.Boolean, default=False, nullable=False)
    waktu_daftar = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='waiting')
    tanggal = db.Column(db.String(10), nullable=False)

    def to_dict(self):
        return {
            'nomor_antrian': self.nomor_antrian,
            'nama': self.nama,
            'umur': self.umur,
            'jalur': self.jalur,
            'poli': self.poli,
            'keluhan': self.keluhan,
            'is_darurat': self.is_darurat,
            'waktu_daftar': self.waktu_daftar,
            'status': self.status
        }

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='admin')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class PoliConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poli_name = db.Column(db.String(80), unique=True, nullable=False)
    kode = db.Column(db.String(20), nullable=False)
    kuota_max = db.Column(db.Integer, nullable=False)
    waktu_per_pasien = db.Column(db.Integer, nullable=False)


class JalurConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    jalur = db.Column(db.String(80), unique=True, nullable=False)
    kuota_max = db.Column(db.Integer, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def get_all_poli_configs():
    return {conf.poli_name: {
        'kode': conf.kode,
        'kuota_max': conf.kuota_max,
        'waktu_per_pasien': conf.waktu_per_pasien
    } for conf in PoliConfig.query.all()}


def get_all_jalur_configs():
    return {conf.jalur: {'kuota_max': conf.kuota_max} for conf in JalurConfig.query.all()}


def get_poli_config(poli_name):
    return PoliConfig.query.filter_by(poli_name=poli_name).first()


def get_jalur_config(jalur_name):
    return JalurConfig.query.filter_by(jalur=jalur_name).first()


def save_default_configs():
    for poli_name, config in DATA_ANTREAN.items():
        existing = PoliConfig.query.filter_by(poli_name=poli_name).first()
        if not existing:
            existing = PoliConfig(
                poli_name=poli_name,
                kode=config['kode'],
                kuota_max=config['kuota_max'],
                waktu_per_pasien=config['waktu_per_pasien']
            )
            db.session.add(existing)
        else:
            # Ensure existing config matches defaults (update if changed)
            changed = False
            if existing.kode != config['kode']:
                existing.kode = config['kode']
                changed = True
            if existing.kuota_max != config['kuota_max']:
                existing.kuota_max = config['kuota_max']
                changed = True
            if existing.waktu_per_pasien != config['waktu_per_pasien']:
                existing.waktu_per_pasien = config['waktu_per_pasien']
                changed = True
            if changed:
                logger.info(f"Updated PoliConfig for {poli_name} to defaults")
    for jalur, kuota_max in KUOTA_JALUR.items():
        existing = JalurConfig.query.filter_by(jalur=jalur).first()
        if not existing:
            db.session.add(JalurConfig(jalur=jalur, kuota_max=kuota_max))
        else:
            if existing.kuota_max != kuota_max:
                existing.kuota_max = kuota_max
                logger.info(f"Updated JalurConfig for {jalur} to {kuota_max}")
    db.session.commit()


with app.app_context():
    db.create_all()
    save_default_configs()
    if not User.query.filter_by(username='dokter').first():
        u = User(username='dokter')
        u.set_password('admin123')
        db.session.add(u)
        db.session.commit()
        logger.info('Default admin user created (dokter)')

# ====================================
# HELPER
# ====================================

def hari_ini():
    return date.today().strftime('%Y-%m-%d')


def today_items():
    return QueueItem.query.filter_by(tanggal=hari_ini())


def hitung_kuota():
    total = today_items().filter(QueueItem.status != 'selesai').count()
    return {
        'total': total,
        'umum': today_items().filter(QueueItem.poli == 'Poli Umum', QueueItem.status != 'selesai').count(),
        'anak': today_items().filter(QueueItem.poli == 'Poli Anak', QueueItem.status != 'selesai').count(),
        'lansia': today_items().filter(QueueItem.poli == 'Poli Lansia', QueueItem.status != 'selesai').count(),
        'igd': today_items().filter(QueueItem.poli.in_(['IGD (DARURAT)', 'IGD (GEJALA BERAT)']), QueueItem.status != 'selesai').count()
    }


def get_poli_today_total(poli):
    return today_items().filter_by(poli=poli).count()


def get_poli_current_nomor(poli):
    called = today_items().filter_by(poli=poli, status='called').order_by(QueueItem.id).all()
    return called[-1].nomor_antrian if called else '-'


def get_poli_active_waiting(poli):
    return today_items().filter(QueueItem.poli == poli, QueueItem.status != 'selesai').count()


def get_igd_dirawat_count():
    return today_items().filter(QueueItem.poli.in_(['IGD (DARURAT)', 'IGD (GEJALA BERAT)']), QueueItem.status == 'dirawat').count()


def load_poli_keys():
    return [config.poli_name for config in PoliConfig.query.order_by(PoliConfig.id).all()]


def config_as_dict():
    return {
        'poli': get_all_poli_configs(),
        'jalur': get_all_jalur_configs()
    }


def hitung_kuota_jalur():
    result = {}
    for jalur, config in get_all_jalur_configs().items():
        terpakai = today_items().filter(QueueItem.jalur == jalur, QueueItem.status != 'selesai').count()
        sisa = max(0, config['kuota_max'] - terpakai)
        percent = int((sisa / config['kuota_max']) * 100) if config['kuota_max'] > 0 else 0
        result[jalur] = {
            'max': config['kuota_max'],
            'used': terpakai,
            'remaining': sisa,
            'percent': percent
        }
    return result


def hitung_kuota_poli():
    result = {}
    for poli_name, config in get_all_poli_configs().items():
        # Count total registrations for the poli today (include semua status)
        terpakai = today_items().filter_by(poli=poli_name).count()
        sisa = max(0, config['kuota_max'] - terpakai)
        percent = int((sisa / config['kuota_max']) * 100) if config['kuota_max'] > 0 else 0
        result[poli_name] = {
            'kuota_max': config['kuota_max'],
            'terpakai': terpakai,
            'sisa': sisa,
            'percent': percent,
            'penuh': sisa <= 0
        }
    return result

# ====================================
# RESET ANTREAN HARIAN
# ====================================
@scheduler.task('cron', id='reset_antrean_harian', hour=0, minute=0)
def reset_antrean_harian():
    today = hari_ini()
    QueueItem.query.filter(QueueItem.tanggal != today).delete()
    db.session.commit()

# ====================================
# HALAMAN PASIEN
# ====================================
@app.route('/')
def index():
    status = {
        'umum': get_poli_active_waiting('Poli Umum'),
        'anak': get_poli_active_waiting('Poli Anak'),
        'lansia': get_poli_active_waiting('Poli Lansia')
    }
    igd_active = get_poli_active_waiting('IGD (DARURAT)') + get_poli_active_waiting('IGD (GEJALA BERAT)')
    igd_max = 5
    igd_remaining = max(0, igd_max - igd_active)
    kuota_poli = hitung_kuota_poli()
    return render_template(
        'index_tailwind.html',
        status=status,
        igd_active=igd_active,
        igd_remaining=igd_remaining,
        igd_max=igd_max,
        kuota_poli=kuota_poli
    )

# ====================================
# LOGIN ADMIN
# ====================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect('/admin')
        return render_template('login.html', error='Username atau password salah')
    return render_template('login.html')

# ====================================
# LOGOUT
# ====================================
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/login')

# ====================================
# DASHBOARD ADMIN
# ====================================
@app.route('/admin')
@login_required
def admin():
    if getattr(current_user, 'role', 'admin') != 'admin':
        return redirect('/login')
    kuota = hitung_kuota()
    antrean = [item.to_dict() for item in today_items().filter(QueueItem.status != 'selesai').order_by(QueueItem.id).all()]
    return render_template('admin_dashboard.html', kuota=kuota, antrean=antrean)

@app.route('/api/admin/queue', methods=['GET'])
@login_required
def admin_queue():
    if getattr(current_user, 'role', 'admin') != 'admin':
        return jsonify({'sukses': False, 'msg': 'Harus login admin'})
    kuota = hitung_kuota()
    antrean = [item.to_dict() for item in today_items().filter(QueueItem.status != 'selesai').order_by(QueueItem.id).all()]
    return jsonify({'sukses': True, 'kuota': kuota, 'antrean': antrean})


@app.route('/api/admin/history', methods=['GET'])
@login_required
def admin_history():
    if getattr(current_user, 'role', 'admin') != 'admin':
        return jsonify({'sukses': False, 'msg': 'Harus login admin'}), 401
    # return last 30 completed for today
    items = today_items().filter_by(status='selesai').order_by(QueueItem.id.desc()).limit(30).all()
    history = [it.to_dict() for it in items]
    return jsonify({'sukses': True, 'history': history})

# ====================================
# API STATUS ANTREAN
# ====================================
@app.route('/api/status_all', methods=['GET'])
def get_status_all():
    res = {}
    for poli_name, target in get_all_poli_configs().items():
        sekarang_nomor = get_poli_current_nomor(poli_name)
        total_registered = get_poli_today_total(poli_name)
        active_waiting = get_poli_active_waiting(poli_name)
        res[poli_name] = {
            'sekarang': sekarang_nomor,
            'waiting': active_waiting,
            'max': target['kuota_max'],
            'sisa': max(0, target['kuota_max'] - total_registered),
            'registered': total_registered
        }
    return jsonify(res)

# ====================================
# API STATUS KUOTA JALUR
# ====================================
@app.route('/api/kuota_status', methods=['GET'])
def kuota_status():
    # Return kuota per POLI (not per jalur)
    res = hitung_kuota_poli()
    return jsonify(res)

# ====================================
# PENDAFTARAN PASIEN
# ====================================
@app.route('/api/daftar', methods=['POST'])
def proses_pendaftaran():
    req = request.json or {}
    nama = (req.get('nama') or '').strip()
    try:
        umur = int(req.get('umur', 0))
    except (TypeError, ValueError):
        umur = 0
    jalur = req.get('jalur', 'BPJS')
    is_darurat = bool(req.get('is_darurat', False))

    keluhan_list = req.get('keluhan', [])
    keluhan_str = ', '.join(keluhan_list) if keluhan_list else 'Tidak ada keluhan'

    if not nama or not umur or umur <= 0:
        return jsonify({'sukses': False, 'msg': 'Nama dan Umur wajib diisi angka valid.'})

    if is_darurat:
        poli_tujuan = 'IGD (DARURAT)'
        estimasi_tunggu = '0 Menit (LANGSUNG MASUK RUANG IGD)'
        pesan_khusus = 'KONDISI DARURAT: langsung menuju ruang IGD.'
    else:
        if umur >= 60:
            poli_tujuan = 'Poli Lansia'
        elif umur <= 12:
            poli_tujuan = 'Poli Anak'
        else:
            poli_tujuan = 'Poli Umum'

    target = DATA_ANTREAN[poli_tujuan]
    total_registered = get_poli_today_total(poli_tujuan)

    if is_darurat:
        dirawat_count = get_igd_dirawat_count()
        if dirawat_count >= 5:
            return jsonify({'sukses': False, 'msg': 'IGD Penuh Sementara\nSaat ini semua ranjang IGD Puskesmas Bina Sehat sedang terisi penuh.\nUntuk kondisi darurat/gawat darurat, silakan langsung datang ke IGD RSUD terdekat 24 jam atau hubungi 112/119.'})
        status = 'dirawat'
    else:
        status = 'waiting'
        if total_registered >= target['kuota_max']:
            if poli_tujuan == 'Poli Umum':
                return jsonify({'sukses': False, 'msg': 'Kuota Poli Umum hari ini sudah penuh. Silakan daftar besok.'})
            elif poli_tujuan == 'Poli Gigi':
                return jsonify({'sukses': False, 'msg': 'Kuota Poli Gigi hari ini sudah penuh. Silakan daftar besok.'})
            else:
                return jsonify({'sukses': False, 'msg': f'Kuota {poli_tujuan} hari ini sudah penuh. Silakan daftar besok.'})

    nomor_baru = f"{target['kode']}-{total_registered + 1:03d}"

    if not is_darurat:
        current_called = today_items().filter_by(poli=poli_tujuan, status='called').count()
        pasien_di_depan = max(0, total_registered - current_called)
        estimasi_tunggu = f'± {pasien_di_depan * target["waktu_per_pasien"]} Menit'
        pesan_khusus = 'Silakan tunggu hingga dipanggil.'

    waktu_daftar = datetime.now().strftime('%d-%m-%Y %H:%M:%S')

    antrean_record = QueueItem(
        nomor_antrian=nomor_baru,
        nama=nama,
        umur=umur,
        jalur=jalur,
        poli=poli_tujuan,
        keluhan=keluhan_str,
        is_darurat=is_darurat,
        waktu_daftar=waktu_daftar,
        status='waiting',
        tanggal=hari_ini()
    )
    db.session.add(antrean_record)
    db.session.commit()

    return jsonify({
        'sukses': True,
        'nomor_antrian': nomor_baru,
        'nama': nama,
        'umur': umur,
        'waktu_daftar': waktu_daftar,
        'poli': poli_tujuan,
        'jalur': jalur,
        'keluhan': keluhan_str,
        'is_darurat': is_darurat,
        'estimasi': estimasi_tunggu,
        'pesan_khusus': pesan_khusus,
        'qr_url': f'https://api.qrserver.com/v1/create-qr-code/?size=150x150&data={nomor_baru}'
    })

# ====================================
# CEK POSISI ANTREAN
# ====================================
@app.route('/api/cek_posisi/<nomor>')
def cek_posisi(nomor):
    try:
        prefix, angka_str = nomor.split('-')
        angka = int(angka_str)
        for poli, data in DATA_ANTREAN.items():
            if data['kode'] == prefix:
                current_nomor = get_poli_current_nomor(poli)
                current_index = 0
                if current_nomor != '-':
                    _, current_str = current_nomor.split('-')
                    current_index = int(current_str)
                if angka < current_index:
                    status = 'Sudah selesai'
                    sisa = 0
                elif angka == current_index:
                    status = 'Sedang dipanggil'
                    sisa = 0
                else:
                    status = 'Menunggu antrean'
                    sisa = angka - current_index
                return jsonify({'sukses': True, 'status': status, 'sisa_orang': sisa})
    except Exception:
        pass
    return jsonify({'sukses': False, 'msg': 'Nomor tidak valid'})

# ====================================
# ADMIN PANGGIL ANTREAN
# ====================================
@app.route('/api/admin/panggil', methods=['POST'])
@login_required
def panggil_antrean():
    if getattr(current_user, 'role', 'admin') != 'admin':
        return jsonify({'sukses': False, 'msg': 'Harus login admin'})
    req = request.json or {}
    nomor = req.get('nomor_antrian')
    if not nomor:
        return jsonify({'sukses': False, 'msg': 'Nomor antrean tidak dikirim'})
    record = QueueItem.query.filter_by(nomor_antrian=nomor, tanggal=hari_ini()).first()
    if not record:
        return jsonify({'sukses': False, 'msg': 'Antrean tidak ditemukan'})
    if record.status == 'selesai':
        return jsonify({'sukses': False, 'msg': 'Pasien sudah selesai'})
    if record.status == 'waiting':
        record.status = 'called'
        db.session.commit()
    return jsonify({'sukses': True, 'nomor_antrian': record.nomor_antrian, 'poli': record.poli, 'nama': record.nama})

@app.route('/api/cek_dokter', methods=['POST'])
@login_required
def cek_dokter():
    if getattr(current_user, 'role', 'admin') != 'admin':
        return jsonify({'sukses': False, 'msg': 'Harus login admin'})
    req = request.json or {}
    nomor = req.get('nomor_antrian')
    if not nomor:
        return jsonify({'sukses': False, 'msg': 'Nomor antrean tidak dikirim'})
    record = QueueItem.query.filter_by(nomor_antrian=nomor, tanggal=hari_ini()).first()
    if not record:
        return jsonify({'sukses': False, 'msg': 'Antrean tidak ditemukan'})
    record.status = 'selesai'
    db.session.commit()
    return jsonify({'sukses': True})

# ====================================
# JALANKAN SERVER
# ====================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)