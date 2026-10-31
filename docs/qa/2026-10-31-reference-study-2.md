# Studi referensi 2: repo sejenis Argus, sumber gratis, media (2026-10-31)

> [!NOTE]
> Lanjutan [studi 2026-10-04](2026-10-04-reference-study.md). Studi kode, bukan ulasan README:
> 14 repo di-clone (shallow) dan dibaca 4 agen paralel per area, tidak ada kode referensi yang
> dijalankan. Setiap klaim punya bukti `file:line` di laporan agen. Keterjangkauan sumber diukur
> dari server produksi lewat `batch_read` Argus sendiri. Ide yang ditolak di studi 10-04 tidak
> diusulkan ulang tanpa bukti baru.

## Daftar Isi

- [Repo yang dipelajari](#repo-yang-dipelajari)
- [Gambaran besar](#gambaran-besar)
- [Dikerjakan hari ini (Batch 1)](#dikerjakan-hari-ini-batch-1)
- [Batch 2 (selesai di 0.4.40)](#batch-2-selesai-di-0440)
- [Ukur dulu, baru putuskan](#ukur-dulu-baru-putuskan)
- [Hasil pengukuran (0.4.41)](#hasil-pengukuran-0441)
- [Ditolak](#ditolak)
- [Klaim README yang tidak terbukti di kode](#klaim-readme-yang-tidak-terbukti-di-kode)

## Repo yang dipelajari

| Area | Repo | Lisensi |
|---|---|---|
| Search dan research | `KnockOutEZ/wigolo` (5.5k), `karust/openserp`, `fatwang2/search2ai`, `LearningCircuit/local-deep-research`, `dondai44423/donsetch` | wigolo dan donsetch AGPL (ide saja), sisanya MIT |
| Fetch, anti-bot, ekstraksi | `0xMassi/webclaw`, `donsetch`, `fastcrw/crw` (yang baru sejak 10-04), `any4ai/AnyCrawl`, `vibheksoni/stealth-browser-mcp` | webclaw, donsetch, crw AGPL (ide saja) |
| Media dan dokumen | `microsoft/markitdown`, `SylphxAI/anymd`, `AeternaLabsHQ/pullmd`, `runesleo/x-reader` | pullmd AGPL, sisanya MIT |
| Sumber platform | `mvanhorn/last30days-skill`, `x-reader`, `wigolo`, `local-deep-research` | MIT kecuali wigolo |

Referensi publik tambahan: WCXB (2.008 halaman, 7 tipe) dan `rs-trafilatura`.

## Gambaran besar

- **Tidak ada teknik gratis baru yang menembus blokir IP.** Semua jalan pintas anti-bot di repo
  ini adalah lapisan fingerprint (sudah diukur dan ditolak 10-04) atau layanan berbayar (Bright
  Data, 2captcha, API hosted). Kesimpulan 10-04 tetap berlaku.
- **Celah terbesar Argus sekarang ada di jenis konten, bukan di fetch.** `read` tidak bercabang
  per content-type (`static.py:288-293` men-decode semua body sebagai teks), jadi docx, xlsx,
  pptx, EPUB, CSV dan feed RSS pulang rusak atau `empty_content`.
- **Ada sinyal ranking gratis yang dibuang**: SearXNG mengirim daftar `engines` per hasil,
  `_map_result` membuangnya. Tiga repo independen memakai kesepakatan antar indeks.
- **Browser dipakai di tempat yang tidak bisa menolong**: halaman tipis tanpa script dan body
  terlalu besar tetap dirender (crw: sekitar 20 detik per 404 yang dirender, 59 dari 59 eskalasi
  oversize gagal).

Diukur dari server produksi (2026-10-31):

| Sumber | Hasil | Catatan |
|---|---|---|
| HN Algolia API | OK | JSON, tanpa key |
| GDELT doc API | OK | berita global bertanggal |
| Google News RSS (`hl=id`) | OK | berita Indonesia hari ini, **bertanggal**; menutup celah "SearXNG tidak memberi tanggal" |
| Polymarket Gamma API | OK | odds makro dan pemilu, tanpa key |
| Wikipedia REST, arXiv API, PubMed E-utilities, Lobsters JSON | OK | |
| Bluesky public API | 403 | ditolak |
| YouTube watch page | halaman terbuka, tanpa transkrip | transkrip diblokir dari IP datacenter (laporan publik 2026) |
| DuckDuckGo lite | CAPTCHA yang lolos sebagai konten | diperbaiki di Batch 1 |

## Dikerjakan hari ini (Batch 1)

Semua kecil, terverifikasi di kode, TDD; suite offline 1229 lulus, SSRF 100%, ruff bersih.

| # | Perubahan | Bukti | File |
|---|---|---|---|
| 1 | Halaman tipis tanpa `<script>` dan tanpa meta refresh tidak lagi dirender | crw `detector.rs:266-302`, webclaw `cloud.rs:499-543`; render Argus hanya mengambil frame utama | `fetch/core.py` |
| 2 | Body terlalu besar atau content-encoding tak dikenal langsung ke Wayback, tanpa stealth browser | crw `lib.rs:733-744` (59/59 gagal) | `fetch/core.py` |
| 3 | `read` tidak meng-cache status 4xx/5xx | webclaw `client.rs:657-668`; sebelumnya 404 di-cache 1 jam | `server.py` |
| 4 | Marker "complete the following challenge" (CAPTCHA DuckDuckGo) | diukur live hari ini | `fetch/static.py` |
| 5 | `engines` per hasil search dipertahankan (data untuk T6, belum mengubah ranking) | `search.py:163` membuangnya | `search.py` |

Ops hari yang sama: DNS host primary server diganti ke 1.1.1.1 dan 8.8.8.8 (resolver provider
membelokkan domain blokir Komdigi; provider konfirmasi tidak ada blokir IP).

## Batch 2 (selesai di 0.4.40)

Semua butir di bawah dikerjakan TDD, direview dua reviewer independen (keamanan dan
kebenaran), lalu divalidasi dengan fixture markitdown dan stress test live.

| # | Ide | Sumber | Rancangan minimal | Usaha |
|---|---|---|---|---|
| D1 | **`read` multi-format**: docx, pptx, xlsx, EPUB, CSV, JSON, XML | markitdown converter classes, anymd `ooxml.rs` (zip caps), pullmd `limits.py` (proses terisolasi) | cabang content-type + magic bytes di `read`, modul `extract/docs.py`; docx/pptx lewat **kelas** converter markitdown (bukan `MarkItDown()` default, yang fetch URL sendiri tanpa SSRF dan membuka `file:`), xlsx lewat openpyxl read-only (tanpa pandas), EPUB lewat PyMuPDF yang sudah ada, CSV/JSON/XML stdlib; guard zip (entri <= 10k, total deklarasi <= 200 MB) dan proses anak dengan timeout | M |
| D2 | **Feed RSS/Atom dirender jadi daftar item** (judul, link, tanggal) dan Google News RSS sebagai sumber `news` bertanggal | ukur live hari ini | parser stdlib `xml.etree` dengan batas ukuran; link `news.google.com/rss/articles/...` perlu di-resolve ke URL asli | S-M |
| D3 | **Hacker News**: adapter `read` untuk `news.ycombinator.com/item?id=` via Algolia `items/{id}` (seluruh pohon komentar satu call) | last30days `hackernews.py:107-128`, wigolo `hn-algolia.ts` | ikuti pola adapter reddit; cek dulu engine `hackernews` di `/config` SearXNG untuk sisi search | S |
| D4 | **Polymarket**: adapter `polymarket.com/event/<slug>` via Gamma `/events?slug=` | last30days `polymarket.py:429-440` | adapter di `fetch/adapters.py`; terjangkau dari server (diukur) | S |
| D5 | Tanggal dari path URL (`/2026/10/`) sebagai metadata `published` + `date_source` | wigolo `freshness.ts:14-35` | metadata saja; jangan masuk `is_fresh` sebelum diukur | S |
| D6 | Buang kandidat yang berupa halaman hasil pencarian (SERP) sebelum fetch di `research` | wigolo `source-validation.ts:87-150` | pola URL `/search?q=`, `?s=` dsb; menghemat slot fetch | S |
| D7 | **Transkrip YouTube** lewat player API Android + timedtext, tanpa dependency | diukur live: langsung 5/5, lewat WARP 0/5 (429) | adapter di `fetch/adapters.py`, selalu direct | M |

## Ukur dulu, baru putuskan

| # | Ide | Sumber | Cara mengukur | Kriteria adopsi |
|---|---|---|---|---|
| M1 | **Kesepakatan antar indeks** (google + google cse satu indeks, bing + duckduckgo web satu indeks) | donsetch `rank.rs:215-310`, wigolo `consensus-boost.ts`, openserp | arm baru di `semantic_ab.py` (data `engines` sudah tersedia sejak Batch 1) | nDCG@5 +0.02 tanpa rugi di subset konseptual |
| M2 | **rs-trafilatura** (klasifikasi tipe halaman + skor kualitas ekstraksi) | WCXB: F1 0.859 vs Argus 0.817 (0.4.29, dev set) | jalankan biner `extract_stdin` di `run_wcxb.py`; cek lisensi dan ongkos build Rust di image | F1 naik >= 0.02 dan latensi per halaman tidak lebih buruk |
| M3 | Penambang JSON data-island (`__NEXT_DATA__`, `__NUXT__`, `self.__next_f.push`) saat DOM < 500 kata | webclaw `data_island.rs`, donsetch `jsdata.rs` (dua implementasi independen) | URL yang kena `fetch.thin_escalate` dari log | kata naik pada >= 30% halaman SPA tanpa sampah |
| M4 | **B1**: benchmark fetch 1000 URL | dataset `firecrawl/scrape-content-dataset-v1` (cek lisensi dataset), skor recall baris seperti crw `diagnose_3way.py` (tulis ulang ~20 baris) + 78 kasus webclaw | dari server produksi, concurrency 5 | jadi angka dasar semua ide fetch |
| M5 | 429 tanpa body tantangan: lewati browser, hormati `Retry-After` | wigolo `router.ts:1248-1273` | hitung `fallback_stealth_ok` setelah 429 di log 14 hari | tidak ada pemulihan oleh browser |
| M6 | Captcha manusia di body static (hCaptcha, reCAPTCHA, Arkose): lewati stealth, langsung Wayback | donsetch `walls.rs:585-627` | sama, dari log | idem |
| M7 | Memori per host "browser menolong atau tidak" (A2) | webclaw `render.rs:2205-2220` | log render per host | hemat render nyata |
| M8 | ~~Transkrip YouTube~~ | diukur 2026-10-31 | langsung 5/5, WARP 0/5 | **diadopsi sebagai D7** |
| M9 | GDELT sebagai sumber berita (terjangkau) | diukur live | relevansi untuk query bisnis Indonesia | menambah sumber yang tidak ada di SearXNG news |
| M10 | Reddit search lewat arctic-shift; StockTwits untuk sentimen | last30days `reddit_arctic.py`, `stocktwits.py` | keterjangkauan dari server | jawaban stabil dari IP datacenter |
| M11 | Recency yang meluruh dengan umur, penalti istilah majemuk dan versi | wigolo `recency-boost.ts`, `rare-terms.ts`; donsetch `coverage.rs` | arm `semantic_ab.py` | nDCG naik pada query bertanggal atau berversi |
| M12 | Tesseract OCR untuk gambar dan PDF hasil scan | anymd `image.rs` (cap piksel, timeout) | 20 sampel nyata | akurasi layak, CPU wajar |
| M13 | Solve Cloudflare managed challenge lalu pakai ulang `cf_clearance` (T9) | webclaw `render.rs`, benchmark cloudflare 2026-10-15 (bukan dari IP datacenter) | 10 URL managed dari server | ada yang lolos |

## Hasil pengukuran (0.4.41)

Semua item "ukur dulu" diukur pada hari yang sama; keputusan mengikuti kriteria yang ditulis
sebelum pengukuran.

| # | Item | Cara | Hasil | Keputusan |
|---|---|---|---|---|
| M1 | Kesepakatan antar indeks | 40 query compare, pool 20 kandidat mentah dari SearXNG produksi (engine per hasil), 800 kandidat dinilai 4 juri buta (urutan diacak, engine disembunyikan) | rerank produksi nDCG@5 0.816 (urutan mentah 0.751); bonus per keluarga indeks terbaik +0.019 (w=10, menang/kalah 21/14), w=3 +0.015 (19/10); tidak monoton | **Ditolak** (kriteria +0.02 tidak tercapai, pola noise); data `engines` tetap disimpan |
| M11 | Penalti istilah majemuk/versi | arm yang sama | hanya 5 query relevan, delta 0.000-0.001 | **Ditolak** |
| M2 | rs-trafilatura (MIT/Apache-2.0, biner Rust 5.6 MB, model tertanam) | WCXB dev 1497 + test 511 halaman, scorer yang sama | teks rs: dev 0.847 vs Argus 0.817 (+0.030, CI 95% +0.020..+0.041), test 0.891 vs 0.858 (+0.033); unggul di service/forum/collection/listing, setara di artikel/dokumentasi; markdown rs lebih buruk (dev 0.780); 48 ms vs 360 ms per halaman; confidence < 0.6 berarti F1 rata-rata 0.33 | **Diadopsi sebagai hybrid**: tipe halaman non-artikel menurut klasifikasi rs sendiri memakai teks rs (dev 0.846, test 0.888), artikel/dokumentasi tetap Argus; confidence rs jadi `metadata.extraction_quality` |
| M3 | Penambang JSON island | 55 halaman B1 recall rendah, HTML diambil ulang dari server | teks truth hanya ada di JSON island pada 2/55 halaman | **Ditolak** (kriteria >= 30%) |
| M4 | **B1: 1000 URL Firecrawl** (MIT, ground truth manusia, 2025-10) dari IP produksi | `read` lewat MCP loopback, konkurensi 5, skor recall baris truth (metode crw) + kebocoran baris noise | read berhasil 91.2%; recall truth rata-rata 0.334, halaman ditemukan (recall >= 0.3) 46.4%; kebocoran noise 0.065; p50 2.0 s, p90 6.9 s, p99 27.5 s; static 611, browser 205, Wayback 94 | angka dasar. Diagnosis 55 halaman recall rendah: 38 konten sudah berubah sejak dataset dibuat, 15 ekstraksi kehilangan teks yang ada di HTML, 2 hanya di JSON island |
| M5 | 429 lewati browser | B1: 16 URL yang probe static-nya 429 | browser memulihkan 7, static (sementara) 5, Wayback 3, gagal 1 | **Ditolak** (browser memang menolong) |
| M6 | Captcha lewati stealth | B1: 7 URL dinding 2xx | browser 5, Wayback 2 | **Ditolak** |
| M7 | Memori per host | B1 hampir semua host unik | tidak terukur di data ini; stealth ok 94 / gagal 153 total | **Ditunda** sampai ada log trafik host berulang |
| M8 | Transkrip YouTube | 5 video dari server | langsung 5/5, WARP 0/5 (429) | **Diadopsi** (D7) |
| M9 | GDELT | 2 probe dari server | 1 OK, 1 timeout | **Ditolak** (tidak stabil; Google News RSS bertanggal sudah terbaca lewat `read`) |
| M10 | arctic-shift, StockTwits, Marginalia | probe dari server | arctic-shift wajib author/subreddit (bukan search global); StockTwits OK dengan label Bullish/Bearish; Marginalia perlu header key | **StockTwits diadopsi** (adapter), lainnya ditolak |
| M12 | OCR tesseract | 7 halaman PDF dirender 150 dpi vs lapisan teksnya, kontainer Ubuntu 24.04 di server | recall kata 0.988 (0.971-1.0), 1.5-6 s per halaman di 2 CPU, paket sekitar 20 MB | **Diadopsi** (gambar dan halaman PDF hasil scan) |
| M13 | Solve Cloudflare + `cf_clearance` | 12 URL managed/non-interactive dari benchmark webclaw, Chromium sabar + klik checkbox, dari IP produksi | `read` saat ini: 0 live, 5 lewat Wayback; eksperimen solve: 1/12 lolos (tidak konsisten), sisanya tetap "Just a moment" walau cookie didapat | **Ditolak** (blokir di reputasi IP); judul baru "Performing security verification" ditambah ke marker |

Temuan ikutan yang langsung diperbaiki: `batch_read` belum punya `max_chars` (respons API besar
membanjiri pemanggil) dan error dokumen di `batch_read` jatuh sebagai error generik.

## Ditolak

| Ide | Alasan |
|---|---|
| openserp (SERP lewat browser) | anti-blokirnya fingerprint + 2captcha berbayar; blokir terjadi di IP sebelum JS (terukur 10-04); satu Chromium per query |
| Camofox, invisible_playwright, stealth-browser-mcp, impersonasi TLS crw 0.39 | lapisan fingerprint, sudah diukur dan ditolak |
| Audio dan podcast lokal, caption gambar lokal | ~30 menit CPU per jam audio; VLM lokal 81-230 detik per halaman dan 4-5 GB RAM |
| `MarkItDown()` default, ZipConverter, converter YouTube/Audio markitdown | fetch sendiri tanpa SSRF, `file:` URI, zip tanpa batas, audio dikirim ke endpoint Google |
| Bluesky | 403 dari server, search perlu app password |
| Fallback provider (search2ai), query rewrite leksikon (wigolo) | penyedia berbayar; leksikon buatan tangan = bias yang ditolak 10-04 |
| Truth Social, TikTok/Instagram/LinkedIn | token dari dev tools atau API berbayar |
| Strategi riset local-deep-research | semua yang dibuka memanggil LLM; Argus tools-not-brain |

## Klaim README yang tidak terbukti di kode

- **last30days**: "transkrip YouTube penuh gratis" padahal kodenya sendiri menulis IP datacenter
  mendapat 0 hasil; "upvote Reddit tanpa key" padahal skor tidak bisa diambil tanpa key.
- **wigolo**: "fully keyless" padahal dua engine Brave butuh `BRAVE_API_KEY`; ekstraktor YouTube
  mengembalikan `transcript: []`.
- **local-deep-research**: "fully-local ~95% SimpleQA" diukur dengan search cloud berbayar
  (Serper).
- **donsetch**: "Cloudflare / DataDome 200 OK" tanpa daftar situs; tier dinding kerasnya Bright
  Data berbayar.
- **webclaw**: klaim didukung benchmark, tapi fallback "bot protection" memanggil API hosted
  berbayar.

---

Copyright 2026 PT Surya Inovasi Prioritas (SURIOTA).
