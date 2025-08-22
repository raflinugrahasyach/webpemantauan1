from flask import Flask, render_template, Response, jsonify, request, session, redirect, url_for, flash
import cv2
import easyocr
import re
from datetime import datetime, timedelta
import os
import time
import threading
import mysql.connector
from mysql.connector import Error
from collections import deque
import numpy as np

app = Flask(__name__)
app.secret_key = 'vtrack-secret-key-2024-final-fix'

# =============================
# Konfigurasi
# =============================
DB_CONFIG = { 'host': 'localhost', 'database': 'etle_system', 'user': 'root', 'password': '' }
RUTE_KAMERA = {
    "Masjid": [1, 2],
    "Departemen IT PSP": [3, 4],
    "Pabrik": [5, 6]
}
BATAS_WAKTU_PERJALANAN = 30
CONFIDENCE_THRESHOLD_MANUAL = 0.4
KAMERA_SETUP = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 }

# =============================
# Inisialisasi & Variabel Global
# =============================
pembaca_ocr = easyocr.Reader(['en'], gpu=False)
folder_output_plat = os.path.join("static", "etle_output", "plat")
os.makedirs(folder_output_plat, exist_ok=True)

is_running = False
camera_thread = None
active_camera_id = None
notifications = deque(maxlen=10)
recent_detections = {}
camera_frame = None
main_lock = threading.Lock()

# =============================
# Fungsi Database
# =============================
def create_connection():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
        return None

def init_database():
    connection = create_connection()
    if not connection: return
    cursor = connection.cursor()
    cursor.execute("CREATE DATABASE IF NOT EXISTS etle_system")
    cursor.execute("USE etle_system")
    # Skema tabel... (Tidak perlu diubah)
    create_perjalanan_table = """
    CREATE TABLE IF NOT EXISTS perjalanan (
        id INT AUTO_INCREMENT PRIMARY KEY, nama_pengunjung VARCHAR(255) NOT NULL,
        nomor_plat VARCHAR(20) NOT NULL, tujuan VARCHAR(100) NOT NULL,
        waktu_mulai DATETIME NOT NULL, waktu_selesai DATETIME,
        status VARCHAR(50) NOT NULL DEFAULT 'Pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    create_deteksi_table = """
    CREATE TABLE IF NOT EXISTS deteksi (
        id INT AUTO_INCREMENT PRIMARY KEY, perjalanan_id INT, nomor_plat VARCHAR(20) NOT NULL,
        waktu_deteksi DATETIME NOT NULL, path_foto VARCHAR(255) NOT NULL,
        confidence FLOAT DEFAULT 0.0, kamera_id INT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (perjalanan_id) REFERENCES perjalanan(id) ON DELETE SET NULL
    )"""
    cursor.execute(create_perjalanan_table)
    cursor.execute(create_deteksi_table)
    connection.commit()
    cursor.close()
    connection.close()
    print("✅ Database dan tabel berhasil diinisialisasi")

# =============================
# Logika Inti & Pemrosesan Status (PERBAIKAN UTAMA)
# =============================
def perbarui_status_perjalanan(perjalanan_id):
    connection = create_connection()
    if not connection: return
    db_cursor = connection.cursor(dictionary=True)

    db_cursor.execute("SELECT * FROM perjalanan WHERE id = %s", (perjalanan_id,))
    perjalanan = db_cursor.fetchone()
    if not perjalanan or perjalanan['status'] not in ['Pending', 'Perlu Cek Manual']:
        db_cursor.close()
        connection.close()
        return

    db_cursor.execute("SELECT * FROM deteksi WHERE perjalanan_id = %s", (perjalanan_id,))
    deteksi_list = db_cursor.fetchall()
    
    status_lama = perjalanan['status']
    rute_wajib = RUTE_KAMERA.get(perjalanan['tujuan'], [])
    kamera_terdeteksi = sorted(list({d['kamera_id'] for d in deteksi_list}))
    
    status_baru = 'Pending'
    # Cek apakah rute yang sudah dilewati sesuai urutan
    if kamera_terdeteksi != rute_wajib[:len(kamera_terdeteksi)]:
        status_baru = 'Gagal'
    # Cek apakah semua rute sudah selesai
    elif set(kamera_terdeteksi) == set(rute_wajib):
        status_baru = 'Sesuai'
    # Cek timeout
    elif datetime.now() > perjalanan['waktu_mulai'] + timedelta(minutes=BATAS_WAKTU_PERJALANAN):
        status_baru = 'Gagal'
    
    if status_baru != 'Gagal' and any(d['confidence'] < CONFIDENCE_THRESHOLD_MANUAL for d in deteksi_list):
        status_baru = 'Perlu Cek Manual'

    if status_baru != status_lama:
        waktu_selesai = datetime.now() if status_baru in ['Sesuai', 'Gagal'] else None
        db_cursor.execute("UPDATE perjalanan SET status = %s, waktu_selesai = %s WHERE id = %s", (status_baru, waktu_selesai, perjalanan_id))
        connection.commit()
        pesan = f"Status perjalanan {perjalanan['nomor_plat']} ke {perjalanan['tujuan']} menjadi {status_baru}."
        notifications.appendleft({'time': datetime.now().strftime('%H:%M:%S'), 'message': pesan, 'status': status_baru})
        print(f"✅ STATUS UPDATE: Perjalanan ID {perjalanan_id} -> {status_baru}")

    db_cursor.close()
    connection.close()
    
    # Otomatis aktifkan kamera berikutnya jika perjalanan masih berlangsung
    if status_baru == 'Pending':
        next_cam_index = len(kamera_terdeteksi)
        if next_cam_index < len(rute_wajib):
            next_camera_id = rute_wajib[next_cam_index]
            start_camera_thread(next_camera_id)

def proses_deteksi(nomor_plat, path_foto, confidence, kamera_id):
    connection = create_connection()
    if not connection: return
    db_cursor = connection.cursor(dictionary=True)
    
    query_perjalanan = "SELECT * FROM perjalanan WHERE nomor_plat = %s AND status IN ('Pending', 'Perlu Cek Manual') ORDER BY waktu_mulai DESC LIMIT 1"
    db_cursor.execute(query_perjalanan, (nomor_plat,))
    perjalanan = db_cursor.fetchone()
    
    perjalanan_id = perjalanan['id'] if perjalanan else None

    # Hanya simpan deteksi jika terkait perjalanan atau jika tidak ada perjalanan sama sekali (untuk anomali)
    if perjalanan:
        # Cek duplikat deteksi untuk kamera yang sama dalam satu perjalanan
        db_cursor.execute("SELECT id FROM deteksi WHERE perjalanan_id = %s AND kamera_id = %s", (perjalanan_id, kamera_id))
        if db_cursor.fetchone():
            db_cursor.close(); connection.close()
            return # Sudah ada deteksi, abaikan

    db_cursor.execute("INSERT INTO deteksi (perjalanan_id, nomor_plat, waktu_deteksi, path_foto, confidence, kamera_id) VALUES (%s, %s, %s, %s, %s, %s)",
                      (perjalanan_id, nomor_plat, datetime.now(), path_foto, confidence, kamera_id))
    connection.commit()

    if perjalanan:
        recent_detections[kamera_id] = {'time': time.time(), 'status': 'success'}
        perbarui_status_perjalanan(perjalanan_id) # Panggil fungsi update tanpa argumen cursor & connection
    else:
        recent_detections[kamera_id] = {'time': time.time(), 'status': 'fail'}
        pesan = f"Peringatan: {nomor_plat} terdeteksi di Kamera {kamera_id} tanpa sesi aktif."
        notifications.appendleft({'time': datetime.now().strftime('%H:%M:%S'), 'message': pesan, 'status': 'Gagal'})

    db_cursor.close()
    connection.close()

# =============================
# Fungsi Streaming & Deteksi Latar
# =============================
def create_info_frame(message):
    frame = np.full((480, 640, 3), (48, 59, 122), dtype=np.uint8)
    (w, h), _ = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
    cv2.putText(frame, message, ((640 - w) // 2, (480 + h) // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    _, buffer = cv2.imencode('.jpg', frame)
    return buffer.tobytes()

def capture_and_detect_task(kamera_id, video_index):
    global is_running, camera_frame, active_camera_id
    kamera = cv2.VideoCapture(video_index, cv2.CAP_DSHOW)
    if not kamera.isOpened():
        with main_lock: camera_frame = create_info_frame(f"Kamera {kamera_id} Gagal")
        return

    print(f"🟢 Memulai thread capture untuk Kamera {kamera_id}...")
    waktu_terakhir_ocr = 0
    while is_running and active_camera_id == kamera_id:
        ret, frame = kamera.read()
        if not ret: time.sleep(0.5); continue
        
        border_color = (100, 100, 100)
        detection_info = recent_detections.get(kamera_id)
        if detection_info and time.time() - detection_info['time'] < 5:
            border_color = (0, 255, 0) if detection_info['status'] == 'success' else (0, 0, 255)
        
        bordered_frame = cv2.copyMakeBorder(frame, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=border_color)
        _, buffer = cv2.imencode('.jpg', bordered_frame)
        with main_lock: camera_frame = buffer.tobytes()

        if time.time() - waktu_terakhir_ocr > 3:
            threading.Thread(target=run_ocr_detection, args=(frame.copy(), kamera_id)).start()
            waktu_terakhir_ocr = time.time()
        time.sleep(0.05)

    kamera.release()
    with main_lock:
        if is_running:
            camera_frame = create_info_frame("Sistem Aktif: Menunggu Tujuan")
    print(f"⛔ Thread capture untuk Kamera {kamera_id} dihentikan.")

def run_ocr_detection(frame, kamera_id):
    hasil_ocr = pembaca_ocr.readtext(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    for (_, teks, conf) in hasil_ocr:
        teks_bersih = re.sub(r'[^A-Z0-9]', '', teks.upper())
        if 4 < len(teks_bersih) < 10:
            path_simpan = os.path.join(folder_output_plat, f"cam{kamera_id}_{teks_bersih}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
            cv2.imwrite(path_simpan, frame)
            proses_deteksi(teks_bersih, path_simpan, conf, kamera_id)
            break

def generate_frames_from_buffer():
    while True:
        with main_lock: frame = camera_frame
        if frame is None: frame = create_info_frame("Menunggu Sistem...")
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.1)

# =============================
# Routes Flask
# =============================
@app.route('/')
def index_redirect():
    if not session.get('logged_in'): return redirect(url_for('login'))
    return redirect(url_for('index'))

@app.route('/index')
def index():
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('username') == 'admin' and request.form.get('password') == 'admin123':
        session['logged_in'] = True
        return redirect(url_for('index'))
    elif request.method == 'POST':
        return render_template('login.html', error='Username atau password salah!')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))
    
def start_camera_thread(kamera_id_to_start):
    global camera_thread, active_camera_id
    with main_lock:
        active_camera_id = kamera_id_to_start
        if camera_thread and camera_thread.is_alive():
            time.sleep(0.5)
        
        video_idx = KAMERA_SETUP.get(active_camera_id, 0)
        camera_thread = threading.Thread(target=capture_and_detect_task, args=(active_camera_id, video_idx))
        camera_thread.daemon = True
        camera_thread.start()
        flash(f'Kamera {active_camera_id} aktif untuk pemantauan.', 'info')

@app.route('/tambah_tujuan', methods=['GET', 'POST'])
def tambah_tujuan():
    if not session.get('logged_in'): return redirect(url_for('login'))
    if request.method == 'POST':
        lokasi_tujuan = request.form['lokasi_tujuan']
        nomor_plat_input = re.sub(r'[^A-Z0-9]', '', request.form['nomor_plat'].upper())
        
        if is_running:
            next_camera = RUTE_KAMERA.get(lokasi_tujuan, [None])[0]
            if next_camera:
                start_camera_thread(next_camera)
        else:
            flash('Sistem pemantauan belum aktif. Klik "Mulai Sistem" terlebih dahulu.', 'warning')
        
        connection = create_connection()
        if connection:
            db_cursor = connection.cursor()
            query = "INSERT INTO perjalanan (nama_pengunjung, nomor_plat, tujuan, waktu_mulai) VALUES (%s, %s, %s, %s)"
            db_cursor.execute(query, (request.form['nama_pengunjung'], nomor_plat_input, lokasi_tujuan, datetime.now()))
            connection.commit()
            db_cursor.close()
            connection.close()
            flash('Sesi perjalanan baru berhasil ditambahkan!', 'success')
        else:
            flash('Gagal terhubung ke database.', 'danger')
        return redirect(url_for('riwayat'))
        
    return render_template('tambah_tujuan.html')

@app.route('/riwayat')
def riwayat():
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('riwayat.html')

@app.route('/api/riwayat')
def api_riwayat():
    if not session.get('logged_in'): return jsonify([])
    connection = create_connection()
    if not connection: return jsonify([])
    db_cursor = connection.cursor(dictionary=True)
    
    db_cursor.execute("SELECT id FROM perjalanan WHERE status IN ('Pending', 'Perlu Cek Manual')")
    pending_ids = [item['id'] for item in db_cursor.fetchall()]
    db_cursor.close()
    connection.close()

    for pid in pending_ids:
        perbarui_status_perjalanan(pid)
    
    connection_new = create_connection()
    db_cursor_new = connection_new.cursor(dictionary=True)
    db_cursor_new.execute("SELECT * FROM perjalanan ORDER BY waktu_mulai DESC")
    semua_perjalanan = db_cursor_new.fetchall()
    db_cursor_new.close()
    connection_new.close()

    for p in semua_perjalanan:
        p['waktu_mulai'] = p['waktu_mulai'].strftime('%Y-%m-%d %H:%M:%S') if p.get('waktu_mulai') else None
        p['waktu_selesai'] = p['waktu_selesai'].strftime('%Y-%m-%d %H:%M:%S') if p.get('waktu_selesai') else None
    return jsonify(semua_perjalanan)

@app.route('/video_feed')
def video_feed():
    if not session.get('logged_in'): return "Unauthorized", 401
    return Response(generate_frames_from_buffer(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status')
def api_status():
    return jsonify({'is_running': is_running, 'active_camera': active_camera_id})

@app.route('/start_detection')
def start_detection():
    global is_running, camera_frame
    if is_running: return jsonify({'status': 'already_running'})
    with main_lock:
        is_running = True
        camera_frame = create_info_frame("Sistem Aktif: Menunggu Tujuan")
    return jsonify({'status': 'started'})

@app.route('/stop_detection')
def stop_detection():
    global is_running, camera_thread, active_camera_id, camera_frame
    if not is_running: return jsonify({'status': 'already_stopped'})
    with main_lock:
        is_running = False
        active_camera_id = None
        camera_frame = create_info_frame("Sistem Dihentikan")
    if camera_thread and camera_thread.is_alive():
        camera_thread.join(timeout=2.0)
    return jsonify({'status': 'stopped'})

if __name__ == '__main__':
    init_database()
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)