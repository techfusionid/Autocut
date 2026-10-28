# autocut-2

HTTP API untuk crop poster dari gambar (deteksi area konten di dalam frame navy).
Sumber gambar berupa **URL** (mis. CDN Instagram) atau **upload file** (PNG/JPEG/WebP).

Stateless — tanpa database, tanpa cache, tanpa file yang disimpan di server.

## Endpoint

| Method | Path | Keterangan |
|---|---|---|
| POST | `/v1/crop` | crop poster, sumber `url` atau `file` |
| GET | `/healthz` | health check `{"status":"ok"}` |

Field form:

| Field | Default | Keterangan |
|---|---|---|
| `url` | — | URL gambar (http/https) |
| `file` | — | file gambar (multipart) |
| `border_color` | `0,0,63` | warna frame, format `r,g,b` |
| `tol` | `18` | toleransi pencocokan warna frame |
| `min_ratio` | `0.8` | rasio piksel konten minimum per baris/kolom |
| `pad` | `0` | padding dikurangi dari hasil crop |
| `format` | `png` | `png` atau `jpeg` |
| `meta` | `false` | `true` → balas JSON, bukan binary |

Error: `413` gambar > 20 MB, `415` bukan gambar, `422` area poster tidak ditemukan / parameter
tidak valid / sumber gambar tidak ada, `502` fetch upstream gagal, `504` upstream timeout.

### Contoh

```bash
# dari URL
curl -X POST http://127.0.0.1:8000/v1/crop -F "url=https://cdn.example.com/poster.webp" -o poster.png

# dari file
curl -X POST http://127.0.0.1:8000/v1/crop -F "file=@poster.webp" -o poster.png

# JSON + base64
curl -X POST http://127.0.0.1:8000/v1/crop -F "file=@poster.webp" -F "meta=true"
```

## Development

```bash
uv sync --dev
uv run autocut-2              # http://127.0.0.1:8000 dengan reload
uv run pytest -s              # test memakai file lokal di tests/ dan URL di tests/url.txt
uv run python scripts/render_outputs.py   # render hasil crop ke outputs/
```

## Production

```bash
uv sync --frozen --no-dev
uv run uvicorn autocut_2.api:app --host 0.0.0.0 --port 8000 \
  --workers "$WEB_CONCURRENCY" --proxy-headers --no-access-log
```

Docker:

```bash
# image standalone (port dari env PORT, default 8000)
docker build -t autocut-2 .
docker run --rm -p 5000:8000 -e WEB_CONCURRENCY=4 autocut-2

# atau lewat compose (host port default 5000, override: HOST_PORT=xxxx)
docker compose up -d --build
docker compose ps        # tunggu status "healthy"
curl -s http://127.0.0.1:5000/healthz
```

Image berbasis `python:3.14-slim` (sesuai `.python-version`), berjalan sebagai user non-root
`app` (uid 10001). Aplikasi di-bake ke image — jangan bind-mount project ke `/app`
(lihat komentar di `docker-compose.yml`).
