# Autocut

Autocut memotong gambar JPEG/PNG dan langsung menghasilkan PNG. Tidak ada database, antrean, atau worker.

## Potong lewat terminal

Jalankan satu perintah dari folder proyek:

```sh
sh run-local.sh
```

Tempel atau seret path gambar ke terminal lalu tekan Enter. Hasil tersimpan di `downloads/`. Ketik `q` untuk keluar. Mode ini mencoba deteksi batas otomatis; jika batasnya tidak jelas, gambar tidak dipotong.

## Jalankan API dengan Docker

```sh
docker compose up --build -d
```

API tersedia di `http://127.0.0.1:8000`. Untuk berhenti, jalankan `docker compose down`. Tidak perlu token atau volume database.

Compose membaca batas upload dan piksel dari `.env`; salin `.env.example` menjadi `.env` bila ingin mengubah batas bawaan.

## Sambungkan ke n8n

Untuk n8n dalam container di mesin yang sama, sambungkan containernya satu kali ke jaringan Autocut:

```sh
docker network connect autocut <nama-container-n8n>
```

Di node **HTTP Request**, gunakan `POST http://api:8000/crop`. Pilih body **Form-Data**, kirim gambar dari binary property bernama `image`, dan tambahkan `mode=auto`. Atur format respons menjadi **File** agar n8n menerima PNG hasilnya. Tidak perlu autentikasi.

Untuk potong manual, gunakan `mode=manual` dan field opsional `crop_left`, `crop_top`, `crop_right`, `crop_bottom` dalam piksel. Gambar harus JPEG/PNG statis; batas bawaan 15 MiB dan 25 juta piksel. Untuk menu lokal, atur `MAX_UPLOAD_BYTES` atau `MAX_IMAGE_PIXELS` di environment sebelum menjalankan skrip.

Jika n8n berjalan di luar Docker pada mesin yang sama, gunakan `http://127.0.0.1:8000/crop`. Jangan gunakan `localhost` dari dalam container n8n; itu menunjuk ke container n8n sendiri. Untuk server lain gunakan alamat yang dapat dijangkau dan HTTPS sebelum membuka API ke jaringan publik.
