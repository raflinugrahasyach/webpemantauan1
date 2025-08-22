import cv2
import easyocr
import re
from datetime import datetime
import os
import time
import threading

# =============================
# Inisialisasi
# =============================

pembaca_ocr = easyocr.Reader(['en'])
detektor_wajah = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

# Folder output
folder_output = "etle_output"
folder_output_plat = os.path.join(folder_output, "plat")
folder_output_wajah = os.path.join(folder_output, "wajah")

os.makedirs(folder_output_plat, exist_ok=True)
os.makedirs(folder_output_wajah, exist_ok=True)

print(f"📂 Folder plat: {os.path.abspath(folder_output_plat)}")
print(f"📂 Folder wajah: {os.path.abspath(folder_output_wajah)}")

# =============================
# Fungsi Watermark Waktu
# =============================

def tambahkan_waktu(frame):
    waktu = datetime.now().strftime("Waktu: %Y-%m-%d %H:%M:%S")
    cv2.putText(frame, waktu, (10, frame.shape[0] - 10), 
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
    return frame


# =============================
# Fungsi Deteksi Plat
# =============================

def deteksi_plat():
    kamera_plat = cv2.VideoCapture(2)
    waktu_terakhir_plat = 0
    jeda_plat = 2

    if not kamera_plat.isOpened():
        print("❌ Kamera plat gagal dibuka.")
        return

    print("🟢 Kamera PLAT aktif! Tekan ESC di window untuk keluar.")

    while True:
        ret, frame = kamera_plat.read()
        if not ret:
            break

        frame = tambahkan_waktu(frame.copy())

        abu = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.bilateralFilter(abu, 11, 17, 17)
        tepi = cv2.Canny(blur, 30, 200)

        kontur, _ = cv2.findContours(tepi.copy(), cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        kontur = sorted(kontur, key=cv2.contourArea, reverse=True)[:10]

        waktu_sekarang = time.time()

        for c in kontur:
            keliling = cv2.arcLength(c, True)
            aproks = cv2.approxPolyDP(c, 0.018 * keliling, True)

            if len(aproks) == 4:
                x, y, w, h = cv2.boundingRect(aproks)
                area_plat = frame[y:y+h, x:x+w]
                hasil_ocr = pembaca_ocr.readtext(area_plat)

                if hasil_ocr:
                    for (_, teks, conf) in hasil_ocr:
                        teks_bersih = teks.upper().replace(" ", "").strip()
                        cocok = re.search(r"[A-Z]{1,2}\d{1,4}[A-Z]{0,3}", teks_bersih)

                        if cocok:
                            plat_nomor = cocok.group()
                            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                            cv2.putText(frame, plat_nomor, (x, y - 10),
                                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

                            if waktu_sekarang - waktu_terakhir_plat >= jeda_plat:
                                nama_file = f"plat_{plat_nomor}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                                path_simpan = os.path.join(folder_output_plat, nama_file)
                                cv2.imwrite(path_simpan, frame)
                                print(f"✅ Plat terdeteksi: {plat_nomor} | Disimpan di {path_simpan}")
                                waktu_terakhir_plat = waktu_sekarang
                            break

        cv2.imshow("Kamera Plat", frame)

        if cv2.waitKey(1) & 0xFF == 27:
            break

    kamera_plat.release()
    cv2.destroyAllWindows()
    print("⛔ Kamera Plat ditutup.")


# =============================
# Fungsi Deteksi Wajah
# =============================

def deteksi_wajah():
    kamera_wajah = cv2.VideoCapture(1)
    waktu_terakhir_wajah = 0
    jeda_wajah = 5

    if not kamera_wajah.isOpened():
        print("❌ Kamera wajah gagal dibuka.")
        return

    print("🟢 Kamera WAJAH aktif! Tekan ESC di window untuk keluar.")

    while True:
        ret, frame = kamera_wajah.read()
        if not ret:
            break

        frame = tambahkan_waktu(frame.copy())

        abu = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        wajah_terdeteksi = detektor_wajah.detectMultiScale(abu, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
        waktu_sekarang = time.time()

        for (xw, yw, ww, hw) in wajah_terdeteksi:
            cv2.rectangle(frame, (xw, yw), (xw + ww, yw + hw), (255, 0, 0), 2)
            cv2.putText(frame, "Wajah", (xw, yw - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 0, 0), 2)

            if waktu_sekarang - waktu_terakhir_wajah >= jeda_wajah:
                nama_file_wajah = f"wajah_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
                path_wajah = os.path.join(folder_output_wajah, nama_file_wajah)
                cv2.imwrite(path_wajah, frame)
                print(f"🧑 Wajah disimpan: {path_wajah}")
                waktu_terakhir_wajah = waktu_sekarang

        cv2.imshow("Kamera Wajah", frame)

        if cv2.waitKey(1) & 0xFF == 27:
            break

    kamera_wajah.release()
    cv2.destroyAllWindows()
    print("⛔ Kamera Wajah ditutup.")


# =============================
# Jalankan paralel
# =============================

t1 = threading.Thread(target=deteksi_plat)
t2 = threading.Thread(target=deteksi_wajah)

t1.start()
t2.start()

t1.join()
t2.join()

print("✅ Semua proses selesai.")
