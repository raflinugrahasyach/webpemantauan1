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
import numpy as np

app = Flask(__name__)
app.secret_key = 'vtrack-secret-key-2024-final-fix'

DB_CONFIG = { 'host': 'localhost', 'database': 'etle_system', 'user': 'root', 'password': '' }
RUTE_KAMERA = {
    "Masjid": [1, 2],
    "Departemen IT PSP": [3, 4],
    "Pabrik": [5, 6]
}

BATAS_WAKTU_ANTAR_CHECKPOINT = 1 

KAMERA_SETUP = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 }
JEDA_DEMO_DETIK = 10

print("Memuat model AI...")
try:
    pembaca_ocr = easyocr.Reader(['en'], gpu=False)
    print("✅ Model EasyOCR berhasil dimuat.")
except Exception as e:
    print(f"❌ Gagal memuat EasyOCR: {e}")

folder_output_plat = os.path.join("static", "etle_output", "plat")
os.makedirs(folder_output_plat, exist_ok=True)

is_running = False
main_lock = threading.Lock()
camera_threads = {}
camera_captures = {}
camera_frames = {}
active_detection_camera_id = None
last_detections = {}

g_notifications = []
notification_checker_thread = None
stop_event = threading.Event()


def create_connection():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Error as e:
        print(f"Error connecting to MySQL: {e}")
        return None

def create_info_frame(message, size=(640, 480)):
    frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    (w, h), _ = cv2.getTextSize(message, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
    cv2.putText(frame, message, ((size[0] - w) // 2, (size[1] + h) // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    _, buffer = cv2.imencode('.jpg', frame)
    return buffer.tobytes()

def add_notification(message, status):
    with main_lock:
        g_notifications.append({'message': message, 'status': status, 'time': datetime.now().isoformat()})
    print(f"🔔 NOTIFIKASI [{status.upper()}]: {message}")


def perbarui_status_dan_kamera_aktif(delay=0):
    def task():
        if delay > 0:
            time.sleep(delay)

        global active_detection_camera_id
        connection = create_connection()
        if not connection: return
        db_cursor = connection.cursor(dictionary=True)
        
        try:
            db_cursor.execute("SELECT * FROM perjalanan WHERE status = 'Pending' ORDER BY waktu_mulai DESC LIMIT 1")
            perjalanan = db_cursor.fetchone()

            next_cam_to_detect = None
            if perjalanan:
                rute_wajib = RUTE_KAMERA.get(perjalanan['tujuan'], [])
                db_cursor.execute("SELECT kamera_id FROM deteksi WHERE perjalanan_id = %s", (perjalanan['id'],))
                kamera_terdeteksi = {row['kamera_id'] for row in db_cursor.fetchall()}
                
                next_cam_index = len(kamera_terdeteksi)
                if next_cam_index < len(rute_wajib):
                    next_cam_to_detect = rute_wajib[next_cam_index]

            if next_cam_to_detect and is_running:
                if active_detection_camera_id != next_cam_to_detect:
                    if active_detection_camera_id is not None:
                        stop_camera_thread(active_detection_camera_id, is_detection_cam=True)
                    start_camera_thread(next_cam_to_detect, is_detection_cam=True)
            else:
                if active_detection_camera_id is not None:
                    stop_camera_thread(active_detection_camera_id, is_detection_cam=True)
        finally:
            db_cursor.close()
            connection.close()

    threading.Thread(target=task).start()

def proses_deteksi(nomor_plat, path_foto, confidence, kamera_id):
    connection = create_connection()
    if not connection: return
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT * FROM perjalanan WHERE status = 'Pending' AND nomor_plat = %s", (nomor_plat,))
        perjalanan = db_cursor.fetchone()

        if not perjalanan:
            db_cursor.execute("INSERT INTO deteksi_anomali (nomor_plat, waktu_deteksi, path_foto, kamera_id) VALUES (%s, %s, %s, %s)",
                              (nomor_plat, datetime.now(), path_foto, kamera_id))
            connection.commit()
            add_notification(f"ANOMALI: Plat {nomor_plat} terdeteksi di CAM-{kamera_id} tanpa tujuan aktif.", 'Gagal')
            return

        perjalanan_id = perjalanan['id']
        rute_wajib = RUTE_KAMERA.get(perjalanan['tujuan'], [])
        db_cursor.execute("SELECT kamera_id FROM deteksi WHERE perjalanan_id = %s", (perjalanan_id,))
        kamera_terdeteksi = {row['kamera_id'] for row in db_cursor.fetchall()}
        
        next_cam_index = len(kamera_terdeteksi)
        
        if next_cam_index < len(rute_wajib) and rute_wajib[next_cam_index] == kamera_id:
            db_cursor.execute("INSERT INTO deteksi (perjalanan_id, nomor_plat, waktu_deteksi, path_foto, confidence, kamera_id) VALUES (%s, %s, %s, %s, %s, %s)",
                              (perjalanan_id, nomor_plat, datetime.now(), path_foto, confidence, kamera_id))
            
            if len(kamera_terdeteksi) + 1 == len(rute_wajib):
                db_cursor.execute("UPDATE perjalanan SET status = 'Sesuai', waktu_selesai = %s WHERE id = %s", (datetime.now(), perjalanan_id))
                add_notification(f"Plat {nomor_plat} telah sampai di tujuan {perjalanan['tujuan']}.", 'Sesuai')
                perbarui_status_dan_kamera_aktif()
            else:
                add_notification(f"Plat {nomor_plat} terdeteksi di CAM-{kamera_id}, melanjutkan.", 'Sesuai')
                perbarui_status_dan_kamera_aktif(delay=JEDA_DEMO_DETIK)
            
            connection.commit()
        
        elif kamera_id not in rute_wajib:
            db_cursor.execute("UPDATE perjalanan SET status = 'Gagal', waktu_selesai = %s WHERE id = %s", (datetime.now(), perjalanan_id))
            connection.commit()
            add_notification(f"Plat {nomor_plat} SALAH RUTE, terdeteksi di CAM-{kamera_id}.", 'Gagal')
            perbarui_status_dan_kamera_aktif()

    finally:
        db_cursor.close()
        connection.close()

def background_notification_checker():
    while not stop_event.is_set():
        try:
            connection = create_connection()
            if not connection:
                time.sleep(30)
                continue

            db_cursor = connection.cursor(dictionary=True)
            db_cursor.execute("SELECT * FROM perjalanan WHERE status = 'Pending'")
            pending_perjalanan = db_cursor.fetchall()

            for perjalanan in pending_perjalanan:
                db_cursor.execute("SELECT waktu_deteksi FROM deteksi WHERE perjalanan_id = %s ORDER BY waktu_deteksi DESC LIMIT 1", (perjalanan['id'],))
                last_detection = db_cursor.fetchone()
                
                waktu_referensi = perjalanan['waktu_mulai']
                if last_detection:
                    waktu_referensi = last_detection['waktu_deteksi']

                if datetime.now() > waktu_referensi + timedelta(minutes=BATAS_WAKTU_ANTAR_CHECKPOINT):
                    pesan = f"Kendaraan {perjalanan['nomor_plat']} tujuan {perjalanan['tujuan']} TERLAMBAT mencapai checkpoint berikutnya."
                    add_notification(pesan, 'Gagal')
                    db_cursor.execute("UPDATE perjalanan SET status = 'Gagal', waktu_selesai = %s WHERE id = %s", (datetime.now(), perjalanan['id']))
                    connection.commit()
                    perbarui_status_dan_kamera_aktif()
            
            db_cursor.close()
            connection.close()
        except Exception as e:
            print(f"Error di background checker: {e}")
        
        stop_event.wait(30)

def capture_task(kamera_id):
    global last_detections
    video_index = KAMERA_SETUP.get(kamera_id, 0)
    cap = cv2.VideoCapture(video_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"❌ Gagal membuka kamera {kamera_id} di indeks {video_index}")
        with main_lock:
            camera_frames[kamera_id] = create_info_frame(f"Gagal Buka Cam {kamera_id}")
        return

    with main_lock:
        camera_captures[kamera_id] = cap
    print(f"✅ Kamera {kamera_id} aktif.")
    
    waktu_terakhir_ocr = 0
    while True:
        with main_lock:
            if kamera_id not in camera_captures: break
        
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        
        with main_lock:
            is_detection_cam = (active_detection_camera_id == kamera_id)

        if is_detection_cam and time.time() - waktu_terakhir_ocr > 3:
            threading.Thread(target=run_ocr_and_save, args=(frame.copy(), kamera_id)).start()
            waktu_terakhir_ocr = time.time()

        with main_lock:
            if kamera_id in last_detections:
                last_detections[kamera_id] = [d for d in last_detections[kamera_id] if time.time() - d['time'] < 2]
                for det in last_detections[kamera_id]:
                    (tl, tr, br, bl) = det['bbox']
                    tl = (int(tl[0]), int(tl[1]))
                    br = (int(br[0]), int(br[1]))
                    cv2.rectangle(frame, tl, br, (0, 255, 0), 2)
                    cv2.putText(frame, det['text'], (tl[0], tl[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        _, buffer = cv2.imencode('.jpg', frame)
        with main_lock:
            camera_frames[kamera_id] = buffer.tobytes()
        time.sleep(0.05)

    if cap.isOpened(): cap.release()
    print(f"⛔ Kamera {kamera_id} ditutup.")

def run_ocr_and_save(frame, cam_id):
    global last_detections
    try:
        hasil_ocr = pembaca_ocr.readtext(frame)
        current_detections = []
        for (bbox, teks, conf) in hasil_ocr:
            teks_bersih = re.sub(r'[^A-Z0-9]', '', teks.upper())
            if 4 < len(teks_bersih) < 10:
                path_simpan = os.path.join(folder_output_plat, f"cam{cam_id}_{teks_bersih}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
                cv2.imwrite(path_simpan, frame)
                proses_deteksi(teks_bersih, path_simpan, conf, cam_id)
                current_detections.append({'bbox': bbox, 'text': teks_bersih, 'time': time.time()})
        
        if current_detections:
            with main_lock:
                last_detections[cam_id] = current_detections
    except Exception as e:
        print(f"Error saat OCR: {e}")

def generate_frames(kamera_id):
    start_camera_thread(kamera_id)
    try:
        while True:
            with main_lock:
                frame_to_yield = camera_frames.get(kamera_id, create_info_frame("Menunggu Kamera..."))
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_to_yield + b'\r\n')
            time.sleep(0.1)
    finally:
        stop_camera_thread(kamera_id)

def generate_dashboard_frame():
    while True:
        with main_lock:
            if not is_running:
                frame_to_yield = create_info_frame("Sistem Tidak Aktif")
            elif active_detection_camera_id is None:
                frame_to_yield = create_info_frame("Sistem Aktif: Menunggu Tujuan")
            else:
                frame_to_yield = camera_frames.get(active_detection_camera_id, create_info_frame(f"Memuat CAM-{active_detection_camera_id}..."))
        
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_to_yield + b'\r\n')
        time.sleep(0.1)

@app.route('/')
def index():
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

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('dashboard.html')

@app.route('/tambah_tujuan', methods=['GET', 'POST'])
def tambah_tujuan():
    if not session.get('logged_in'): return redirect(url_for('login'))
    if request.method == 'POST':
        if not is_running:
            flash('Sistem pemantauan belum aktif. Silakan mulai sistem terlebih dahulu.', 'warning')
            return render_template('tambah_tujuan.html')
        connection = create_connection()
        if connection:
            try:
                db_cursor = connection.cursor()
                db_cursor.execute("INSERT INTO perjalanan (nama_pengunjung, nomor_plat, tujuan, waktu_mulai, status) VALUES (%s, %s, %s, %s, %s)",
                                  (request.form['nama_pengunjung'], re.sub(r'[^A-Z0-9]', '', request.form['nomor_plat'].upper()), request.form['lokasi_tujuan'], datetime.now(), 'Pending'))
                connection.commit()
                flash('Sesi perjalanan baru berhasil ditambahkan!', 'success')
                perbarui_status_dan_kamera_aktif()
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

@app.route('/pemantauan/<int:perjalanan_id>')
def pemantauan(perjalanan_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('pemantauan.html', perjalanan_id=perjalanan_id)

@app.route('/verifikasi/<int:perjalanan_id>')
def verifikasi(perjalanan_id):
    if not session.get('logged_in'): return redirect(url_for('login'))
    return render_template('verifikasi.html', perjalanan_id=perjalanan_id)

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

@app.route('/api/riwayat')
def api_riwayat():
    if not session.get('logged_in'): return jsonify([])
    connection = create_connection()
    if not connection: return jsonify([])
    db_cursor = connection.cursor(dictionary=True)
    try:
        query = "SELECT p.*, d.path_foto FROM perjalanan p LEFT JOIN (SELECT perjalanan_id, path_foto FROM deteksi ORDER BY waktu_deteksi ASC) d ON p.id = d.perjalanan_id GROUP BY p.id ORDER BY p.waktu_mulai DESC"
        db_cursor.execute(query)
        semua_perjalanan = db_cursor.fetchall()
        for p in semua_perjalanan:
            p['waktu_mulai'] = p['waktu_mulai'].strftime('%Y-%m-%dT%H:%M:%S') if p.get('waktu_mulai') else None
        return jsonify(semua_perjalanan)
    finally:
        db_cursor.close()
        connection.close()

@app.route('/api/pemantauan_status/<int:perjalanan_id>')
def api_pemantauan_status(perjalanan_id):
    if not session.get('logged_in'): return jsonify({'error': 'Unauthorized'}), 401
    connection = create_connection()
    if not connection: return jsonify({'error': 'Database connection failed'}), 500
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT kamera_id FROM deteksi WHERE perjalanan_id = %s", (perjalanan_id,))
        kamera_terdeteksi = {d['kamera_id'] for d in db_cursor.fetchall()}
        return jsonify({'kamera_terdeteksi': list(kamera_terdeteksi)})
    finally:
        db_cursor.close()
        connection.close()
        
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
        db_cursor.execute("SELECT * FROM deteksi WHERE perjalanan_id = %s ORDER BY waktu_deteksi ASC", (perjalanan_id,))
        deteksi_list = db_cursor.fetchall()
        perjalanan['waktu_mulai'] = perjalanan['waktu_mulai'].strftime('%Y-%m-%d %H:%M:%S') if perjalanan.get('waktu_mulai') else None
        for deteksi in deteksi_list:
            deteksi['waktu_deteksi'] = deteksi['waktu_deteksi'].strftime('%Y-%m-%d %H:%M:%S') if deteksi.get('waktu_deteksi') else None
        return jsonify({'perjalanan': perjalanan, 'deteksi': deteksi_list})
    finally:
        db_cursor.close()
        connection.close()

@app.route('/api/stats')
def api_stats():
    if not session.get('logged_in'): return jsonify({'error': 'Unauthorized'}), 401
    connection = create_connection()
    if not connection: return jsonify({'total_deteksi': 0, 'deteksi_hari_ini': 0, 'deteksi_minggu_ini': 0, 'deteksi_bulan_ini': 0})
    db_cursor = connection.cursor(dictionary=True)
    try:
        db_cursor.execute("SELECT COUNT(*) as count FROM perjalanan")
        total = db_cursor.fetchone()['count']
        db_cursor.execute("SELECT COUNT(*) as count FROM perjalanan WHERE DATE(waktu_mulai) = CURDATE()")
        today = db_cursor.fetchone()['count']
        db_cursor.execute("SELECT COUNT(*) as count FROM perjalanan WHERE YEARWEEK(waktu_mulai, 1) = YEARWEEK(CURDATE(), 1)")
        this_week = db_cursor.fetchone()['count']
        db_cursor.execute("SELECT COUNT(*) as count FROM perjalanan WHERE YEAR(waktu_mulai) = YEAR(CURDATE()) AND MONTH(waktu_mulai) = MONTH(CURDATE())")
        this_month = db_cursor.fetchone()['count']
        return jsonify({'total_deteksi': total, 'deteksi_hari_ini': today, 'deteksi_minggu_ini': this_week, 'deteksi_bulan_ini': this_month})
    finally:
        db_cursor.close()
        connection.close()

@app.route('/video_feed/<int:kamera_id>')
def video_feed(kamera_id):
    if not session.get('logged_in'): return "Unauthorized", 401
    return Response(generate_frames(kamera_id), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/dashboard_video_feed')
def dashboard_video_feed():
    if not session.get('logged_in'): return "Unauthorized", 401
    return Response(generate_dashboard_frame(), mimetype='multipart/x-mixed-replace; boundary=frame')
    
@app.route('/api/status')
def api_status():
    with main_lock:
        return jsonify({'is_running': is_running, 'active_camera': active_detection_camera_id})

@app.route('/api/notifications')
def api_notifications():
    with main_lock:
        notifications_to_send = list(g_notifications)
        g_notifications.clear()
    return jsonify(notifications_to_send)

def start_camera_thread(kamera_id, is_detection_cam=False):
    global active_detection_camera_id
    with main_lock:
        if kamera_id in camera_threads and camera_threads[kamera_id].is_alive():
            if is_detection_cam: active_detection_camera_id = kamera_id
            return
        thread = threading.Thread(target=capture_task, args=(kamera_id,))
        thread.daemon = True
        thread.start()
        camera_threads[kamera_id] = thread
        if is_detection_cam: active_detection_camera_id = kamera_id

def stop_camera_thread(kamera_id, is_detection_cam=False):
    with main_lock:
        global active_detection_camera_id
        if is_detection_cam:
            if active_detection_camera_id == kamera_id: active_detection_camera_id = None
            return 
        if kamera_id in camera_captures:
            cap = camera_captures.pop(kamera_id, None)
            if cap: cap.release()
        camera_threads.pop(kamera_id, None)
        camera_frames.pop(kamera_id, None)

@app.route('/start_detection')
def start_detection():
    global is_running, notification_checker_thread, stop_event
    if is_running: return jsonify({'status': 'already_running'})
    with main_lock:
        is_running = True
        stop_event.clear()
        if notification_checker_thread is None or not notification_checker_thread.is_alive():
            notification_checker_thread = threading.Thread(target=background_notification_checker)
            notification_checker_thread.daemon = True
            notification_checker_thread.start()
    perbarui_status_dan_kamera_aktif()
    return jsonify({'status': 'started'})
    
@app.route('/stop_detection')
def stop_detection():
    global is_running, camera_threads, camera_captures, camera_frames, active_detection_camera_id
    if not is_running: return jsonify({'status': 'already_stopped'})
    with main_lock:
        is_running = False
        stop_event.set()
        
        cam_ids = list(camera_captures.keys())
        for cam_id in cam_ids:
            cap = camera_captures.pop(cam_id, None)
            if cap: cap.release()
        camera_threads.clear()
        camera_frames.clear()
        active_detection_camera_id = None
    return jsonify({'status': 'stopped'})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
