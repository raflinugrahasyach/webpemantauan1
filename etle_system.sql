-- Hapus tabel jika sudah ada untuk memastikan skema yang bersih
DROP TABLE IF EXISTS deteksi;
DROP TABLE IF EXISTS deteksi_wajah;
DROP TABLE IF EXISTS perjalanan;

-- Tabel untuk mencatat setiap sesi perjalanan kendaraan
CREATE TABLE perjalanan (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nama_pengunjung VARCHAR(255) NOT NULL,
    nomor_plat VARCHAR(20) NOT NULL,
    tujuan VARCHAR(100) NOT NULL,
    waktu_mulai DATETIME NOT NULL,
    waktu_selesai DATETIME,
    -- Status bisa: 'Pending', 'Sesuai', 'Gagal', 'Perlu Cek Manual'
    status VARCHAR(50) NOT NULL DEFAULT 'Pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabel untuk mencatat setiap deteksi plat nomor oleh kamera
CREATE TABLE deteksi (
    id INT AUTO_INCREMENT PRIMARY KEY,
    -- Menghubungkan deteksi ke sesi perjalanan tertentu
    perjalanan_id INT,
    nomor_plat VARCHAR(20) NOT NULL,
    waktu_deteksi DATETIME NOT NULL,
    path_foto VARCHAR(255) NOT NULL,
    confidence FLOAT DEFAULT 0.0,
    -- Menandai kamera mana yang melakukan deteksi
    kamera_id INT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (perjalanan_id) REFERENCES perjalanan(id) ON DELETE SET NULL
);

-- Tabel untuk deteksi wajah (tidak ada perubahan fungsionalitas inti)
CREATE TABLE deteksi_wajah (
    id INT AUTO_INCREMENT PRIMARY KEY,
    waktu_deteksi DATETIME,
    path_foto VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);