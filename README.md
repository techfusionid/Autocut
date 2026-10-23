# Autocut

Autocut menerima gambar JPEG/PNG, memotong tepi gambar, lalu menyediakan hasil PNG melalui API. API dan worker berjalan sebagai proses terpisah. Hasil otomatis yang batas potongnya meragukan berstatus `needs_review` dan tidak menghasilkan PNG.

## Menjalankan di komputer sendiri

Pastikan virtual environment dan dependensi proyek sudah tersedia, lalu dari folder Autocut jalankan:

```sh
sh run-local.sh
```

Menu terminal akan memandu upload dan penyimpanan hasil. Pilih `1`, masukkan atau seret file gambar ke terminal, lalu tekan Enter. Pilih `2` untuk menyimpan ulang hasil yang sudah selesai. Pilih `q` untuk berhenti. Mode lokal hanya membuka API di komputer sendiri dan menonaktifkan bearer token.

## Menjalankan dengan Docker

Salin `.env.example` menjadi `.env`, lalu ganti nilai `API_TOKEN` dengan token pribadi. Jalankan Docker Desktop, kemudian:

```sh
docker compose up --build -d
```

API tersedia di `http://127.0.0.1:8000`; worker akan memproses antrean secara terpisah. Data pekerjaan dan gambar disimpan di volume Docker `autocut_data`, sehingga tetap ada setelah container dihentikan. Untuk menghentikan layanan:

```sh
docker compose down
```

## Menyambungkan n8n

Jika n8n berjalan di container pada mesin Docker yang sama, sambungkan container n8n ke jaringan Autocut satu kali:

```sh
docker network connect autocut <nama-container-n8n>
```

Di node **HTTP Request** n8n, gunakan alamat dasar `http://api:8000` dan autentikasi **Bearer** dengan token yang sama dari `.env`. Jangan gunakan `localhost` dari dalam container n8n, karena itu menunjuk ke container n8n sendiri.

Alur request:

1. **Upload:** `POST /jobs`, autentikasi Bearer, body **Form-Data**. Kirim file sebagai field binary bernama `image`; sertakan `mode=auto`. Untuk potong manual, gunakan `mode=manual` dan field opsional `crop_left`, `crop_top`, `crop_right`, `crop_bottom` dalam piksel.
2. **Cek status:** polling URL relatif `status_url` dari respons upload, digabungkan dengan alamat dasar `http://api:8000`. Status akhir dapat berupa `succeeded`, `failed`, atau `needs_review`.
3. **Ambil hasil:** jika status `succeeded`, unduh URL relatif `result_url` sebagai file/binary PNG. Status `failed` atau `needs_review` tidak memiliki hasil untuk diunduh.

Catatan: API memerlukan `API_TOKEN` saat berjalan di Docker. Jika n8n ada di mesin lain, gunakan alamat server yang dapat dijangkau; gunakan HTTPS sebelum membuka API ke jaringan publik. Proyek ini menyediakan API, bukan workflow n8n siap impor.
