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

# PENTING: Konfigurasi ini sudah diatur untuk pengujian dengan SATU WEBCAM.
# Semua kamera akan menggunakan webcam di indeks 0.
KAMERA_SETUP = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 }

# UNTUK PENGGUNAAN NYATA DENGAN BANYAK WEBCAM, GUNAKAN KONFIGURASI INI:
# KAMERA_SETUP = { 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5 }


# =============================
# Inisialisasi & Variabel Global
# =============================
print("Memuat model AI...")
pembaca_ocr = easyocr.Reader(['en'], gpu=False)
detektor_wajah = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
print("✅ Model AI berhasil dimuat.")

folder_output_plat = os.path.join("static", "etle_output", "plat")
os.makedirs(folder_output_plat, exist_ok=True)

is_running = False
camera_thread = None
active_camera_id = None
camera_frame = None
main_lock = threading.Lock()

# =============================
# Fungsi Pembantu
# =============================
def create_info_frame(message, size=(640, 480)):
    frame = np.full((size[1], size[0], 3), (244, 247, 252), dtype=np.uint8)
    (w, h), _ = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
    cv2.putText(frame, message, ((size[0] - w) // 2, (size[1] + h) // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (59, 76, 166), 2)
    _, buffer = cv2.imencode('.jpg', frame)
    return buffer.tobytes()

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
# Logika Inti & Pemrosesan
# =============================
def perbarui_status_perjalanan(perjalanan_id):
    connection = create_connection()
    if not connection: return
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT * FROM perjalanan WHERE id = %s", (perjalanan_id,))
        perjalanan = db_cursor.fetchone()
        if not perjalanan or perjalanan['status'] not in ['Pending', 'Perlu Cek Manual']: return
        
        db_cursor.execute("SELECT * FROM deteksi WHERE perjalanan_id = %s ORDER BY waktu_deteksi ASC", (perjalanan_id,))
        deteksi_list = db_cursor.fetchall()
        status_lama = perjalanan['status']
        rute_wajib = RUTE_KAMERA.get(perjalanan['tujuan'], [])
        kamera_terdeteksi = sorted(list({d['kamera_id'] for d in deteksi_list}))
        status_baru = 'Pending'
        
        if kamera_terdeteksi != rute_wajib[:len(kamera_terdeteksi)]: status_baru = 'Gagal'
        elif set(kamera_terdeteksi) == set(rute_wajib): status_baru = 'Sesuai'
        elif datetime.now() > perjalanan['waktu_mulai'] + timedelta(minutes=BATAS_WAKTU_PERJALANAN): status_baru = 'Gagal'
        if status_baru != 'Gagal' and any(d['confidence'] < CONFIDENCE_THRESHOLD_MANUAL for d in deteksi_list): status_baru = 'Perlu Cek Manual'
        
        if status_baru != status_lama:
            waktu_selesai = datetime.now() if status_baru in ['Sesuai', 'Gagal'] else None
            db_cursor.execute("UPDATE perjalanan SET status = %s, waktu_selesai = %s WHERE id = %s", (status_baru, waktu_selesai, perjalanan_id))
            connection.commit()

        if status_baru in ['Pending', 'Perlu Cek Manual']:
            next_cam_index = len(kamera_terdeteksi)
            if next_cam_index < len(rute_wajib):
                start_camera_thread(rute_wajib[next_cam_index])
            else:
                stop_camera_thread()
        else:
            stop_camera_thread()
    finally:
        db_cursor.close()
        connection.close()

def proses_deteksi(nomor_plat, path_foto, confidence, kamera_id):
    connection = create_connection()
    if not connection: return
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT * FROM perjalanan WHERE nomor_plat = %s AND status IN ('Pending', 'Perlu Cek Manual') ORDER BY waktu_mulai DESC LIMIT 1", (nomor_plat,))
        perjalanan = db_cursor.fetchone()
        perjalanan_id = perjalanan['id'] if perjalanan else None
        if perjalanan:
            db_cursor.execute("SELECT id FROM deteksi WHERE perjalanan_id = %s AND kamera_id = %s", (perjalanan_id, kamera_id))
            if db_cursor.fetchone(): return
        db_cursor.execute("INSERT INTO deteksi (perjalanan_id, nomor_plat, waktu_deteksi, path_foto, confidence, kamera_id) VALUES (%s, %s, %s, %s, %s, %s)",
                          (perjalanan_id, nomor_plat, datetime.now(), path_foto, confidence, kamera_id))
        connection.commit()
        if perjalanan:
            perbarui_status_perjalanan(perjalanan_id)
    finally:
        db_cursor.close()
        connection.close()

def capture_and_detect_task(kamera_id, video_index):
    global camera_frame
    kamera = cv2.VideoCapture(video_index, cv2.CAP_DSHOW)
    if not kamera.isOpened():
        with main_lock: camera_frame = create_info_frame(f"Gagal Buka Kamera {kamera_id}")
        return
    
    print(f"✅ Kamera {kamera_id} aktif.")
    waktu_terakhir_ocr = 0
    while is_running and active_camera_id == kamera_id:
        ret, frame = kamera.read()
        if not ret:
            time.sleep(0.1)
            continue
        
        if time.time() - waktu_terakhir_ocr > 3:
            threading.Thread(target=run_ocr_detection, args=(frame.copy(), kamera_id)).start()
            waktu_terakhir_ocr = time.time()

        _, buffer = cv2.imencode('.jpg', frame)
        with main_lock:
            camera_frame = buffer.tobytes()
        time.sleep(0.05)

    if kamera.isOpened():
        kamera.release()
    with main_lock:
        if active_camera_id == kamera_id:
            camera_frame = create_info_frame("Sistem Aktif: Menunggu Tujuan")
    print(f"⛔ Kamera {kamera_id} ditutup.")

def run_ocr_detection(frame, cam_id):
    try:
        hasil_ocr = pembaca_ocr.readtext(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        for (_, teks, conf) in hasil_ocr:
            teks_bersih = re.sub(r'[^A-Z0-9]', '', teks.upper())
            if 4 < len(teks_bersih) < 10:
                path_simpan = os.path.join(folder_output_plat, f"cam{cam_id}_{teks_bersih}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
                cv2.imwrite(path_simpan, frame)
                proses_deteksi(teks_bersih, path_simpan, conf, cam_id)
                break
    except Exception as e:
        print(f"Error saat OCR: {e}")

def generate_frames_from_buffer():
    global camera_frame
    while True:
        with main_lock:
            if camera_frame is None:
                camera_frame = create_info_frame("Sistem Tidak Aktif")
            frame_to_yield = camera_frame
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_to_yield + b'\r\n')
        time.sleep(0.1)

# =============================
# Routes Flask
# =============================
@app.route('/')
def index_redirect():
    if not session.get('logged_in'): return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('dashboard.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and request.form.get('username') == 'admin' and request.form.get('password') == 'admin123':
        session['logged_in'] = True
        return redirect(url_for('dashboard'))
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
        if active_camera_id == kamera_id_to_start and camera_thread and camera_thread.is_alive():
            return
        active_camera_id = kamera_id_to_start
        video_idx = KAMERA_SETUP.get(active_camera_id, 0)
        camera_thread = threading.Thread(target=capture_and_detect_task, args=(active_camera_id, video_idx))
        camera_thread.daemon = True
        camera_thread.start()

def stop_camera_thread():
    global active_camera_id
    with main_lock:
        active_camera_id = None

@app.route('/tambah_tujuan', methods=['GET', 'POST'])
def tambah_tujuan():
    if not session.get('logged_in'): return redirect(url_for('login'))
    if request.method == 'POST':
        if not is_running:
            flash('Sistem pemantauan belum aktif.', 'warning')
            return render_template('tambah_tujuan.html')
        connection = create_connection()
        if connection:
            try:
                db_cursor = connection.cursor()
                db_cursor.execute("INSERT INTO perjalanan (nama_pengunjung, nomor_plat, tujuan, waktu_mulai, status) VALUES (%s, %s, %s, %s, %s)",
                                  (request.form['nama_pengunjung'], re.sub(r'[^A-Z0-9]', '', request.form['nomor_plat'].upper()), request.form['lokasi_tujuan'], datetime.now(), 'Pending'))
                connection.commit()
                
                new_perjalanan_id = db_cursor.lastrowid
                perbarui_status_perjalanan(new_perjalanan_id)

                flash('Sesi perjalanan baru berhasil ditambahkan!', 'success')
            except Error as e:
                flash(f'Gagal menambahkan perjalanan: {e}', 'danger')
            finally:
                db_cursor.close()
                connection.close()
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
    try:
        db_cursor.execute("SELECT id FROM perjalanan WHERE status IN ('Pending', 'Perlu Cek Manual')")
        pending_ids = [item['id'] for item in db_cursor.fetchall()]
        
        for pid in pending_ids:
            perbarui_status_perjalanan(pid)
        
        db_cursor.execute("SELECT * FROM perjalanan ORDER BY waktu_mulai DESC")
        semua_perjalanan = db_cursor.fetchall()

        for p in semua_perjalanan:
            p['waktu_mulai'] = p['waktu_mulai'].strftime('%Y-%m-%d %H:%M:%S') if p.get('waktu_mulai') else None
            p['waktu_selesai'] = p['waktu_selesai'].strftime('%Y-%m-%d %H:%M:%S') if p.get('waktu_selesai') else None
        return jsonify(semua_perjalanan)
    finally:
        db_cursor.close()
        connection.close()
        
@app.route('/verifikasi/<int:perjalanan_id>')
def verifikasi(perjalanan_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('verifikasi.html')

@app.route('/api/perjalanan/<int:perjalanan_id>')
def api_perjalanan_detail(perjalanan_id):
    if not session.get('logged_in'): return jsonify({'error': 'Unauthorized'}), 401
    connection = create_connection()
    if not connection: return jsonify({'error': 'Database connection failed'}), 500
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT * FROM perjalanan WHERE id = %s", (perjalanan_id,))
        perjalanan = db_cursor.fetchone()
        if not perjalanan: return jsonify({'error': 'Perjalanan tidak ditemukan'}), 404
        db_cursor.execute("SELECT * FROM deteksi WHERE perjalanan_id = %s", (perjalanan_id,))
        deteksi_list = db_cursor.fetchall()
        perjalanan['waktu_mulai'] = perjalanan['waktu_mulai'].strftime('%Y-%m-%d %H:%M:%S') if perjalanan.get('waktu_mulai') else None
        return jsonify({'perjalanan': perjalanan, 'deteksi': deteksi_list})
    finally:
        db_cursor.close()
        connection.close()

@app.route('/update_verifikasi', methods=['POST'])
def update_verifikasi():
    if not session.get('logged_in'): return redirect(url_for('login'))
    perjalanan_id = request.form['perjalanan_id']
    status_baru = request.form['status']
    nomor_plat_koreksi = re.sub(r'[^A-Z0-9]', '', request.form['nomor_plat_koreksi'].upper())
    connection = create_connection()
    if not connection:
        flash("Gagal terhubung ke database.", "danger")
        return redirect(url_for('riwayat'))
    db_cursor = connection.cursor()
    try:
        db_cursor.execute("UPDATE perjalanan SET nomor_plat = %s, status = %s, waktu_selesai = %s WHERE id = %s",
                          (nomor_plat_koreksi, status_baru, datetime.now() if status_baru != 'Pending' else None, perjalanan_id))
        if status_baru == 'Sesuai':
             db_cursor.execute("UPDATE deteksi SET nomor_plat = %s WHERE perjalanan_id = %s", (nomor_plat_koreksi, perjalanan_id))
        connection.commit()
        flash("Verifikasi berhasil diperbarui.", "success")
    except Error as e:
        flash(f"Gagal memperbarui verifikasi: {e}", "danger")
    finally:
        db_cursor.close()
        connection.close()
    return redirect(url_for('riwayat'))

@app.route('/api/stats')
def api_stats():
    if not session.get('logged_in'): return jsonify({'error': 'Unauthorized'}), 401
    connection = create_connection()
    if not connection: return jsonify({'total': 0, 'today': 0, 'this_week': 0, 'this_month': 0})
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT COUNT(*) as count FROM deteksi")
        total = db_cursor.fetchone()['count']
        db_cursor.execute("SELECT COUNT(*) as count FROM deteksi WHERE DATE(waktu_deteksi) = CURDATE()")
        today = db_cursor.fetchone()['count']
        db_cursor.execute("SELECT COUNT(*) as count FROM deteksi WHERE YEARWEEK(waktu_deteksi, 1) = YEARWEEK(CURDATE(), 1)")
        this_week = db_cursor.fetchone()['count']
        db_cursor.execute("SELECT COUNT(*) as count FROM deteksi WHERE YEAR(waktu_deteksi) = YEAR(CURDATE()) AND MONTH(waktu_deteksi) = MONTH(CURDATE())")
        this_month = db_cursor.fetchone()['count']
        return jsonify({'total': total, 'today': today, 'this_week': this_week, 'this_month': this_month})
    finally:
        db_cursor.close()
        connection.close()

@app.route('/video_feed')
def video_feed():
    if not session.get('logged_in'): return "Unauthorized", 401
    return Response(generate_frames_from_buffer(), mimetype='multipart/x-mixed-replace; boundary=frame')
    
@app.route('/api/status')
def api_status():
    return jsonify({'is_running': is_running, 'active_camera': active_camera_id})

@app.route('/start_detection')
def start_detection():
    global is_running
    if is_running: return jsonify({'status': 'already_running'})
    is_running = True
    return jsonify({'status': 'started'})
    
@app.route('/stop_detection')
def stop_detection():
    global is_running
    if not is_running: return jsonify({'status': 'already_stopped'})
    is_running = False
    stop_camera_thread()
    return jsonify({'status': 'stopped'})

if __name__ == '__main__':
    init_database()
    app.run(debug=False, host='0.0.0.0', port=5000)