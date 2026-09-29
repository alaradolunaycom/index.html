# job-agent — iş arama pipeline'ı (insan onaylı)

Bu ajan iş ilanlarını buluyor, CV'ne göre eliyor, Claude ile uygunluk puanı veriyor, her ilana özel başvuru ve outreach metinlerini yazıyor ve follow-up'ları takip ediyor.
**Başvuruyu ve mesajı sen gönderiyorsun.** Bu bilerek böyle tasarlandı:

- LinkedIn Kullanıcı Sözleşmesi bot, scraper ve otomatik mesaj/başvuruyu yasaklıyor. Hesap kısıtlanırsa bu iş aramasındaki en değerli varlığını, yani ağını kaybedersin.
- Toplu ve kişiselleştirilmemiş başvuru, senin seviyende (5+ yıl, growth/GTM) mülakata nadiren dönüşüyor. Az sayıda ama isabetli başvuru daha çok sonuç veriyor.

## Kurulum

```bash
cd job-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # screening + taslaklar için; yoksa sadece kural bazlı skor çalışır
```

## Günlük rutin (~90 dk)

| Adım | Komut | Süre |
|---|---|---|
| 1. Tara | `python -m job_agent scan` → `data/digest-YYYY-MM-DD.md` | 2 dk |
| 2. LinkedIn taraması | `python -m job_agent linkedin` → son 24 saatte açılan remote ilanları aç | 20 dk |
| 3. Beğendiğini ekle | `pbpaste \| python -m job_agent add <URL> --title "..." --company "..." --location "..."` | ilan başına 1 dk |
| 4. Listeyi gözden geçir | `python -m job_agent list` (fit ≥ 70 ve verdict = apply olanlar) | 5 dk |
| 5. Taslak oluştur | `python -m job_agent draft <id>` → `data/drafts/` | ilan başına 2 dk |
| 6. Başvur ve mesaj gönder | kendin; sonra `mark <id> applied` / `mark <id> outreach` | ilan başına 10 dk |
| 7. Follow-up | `python -m job_agent followups` | 10 dk |

Diğer komutlar: `show <id>`, `mark <id> interview|rejected|offer --note "..."`, `export` (tüm pipeline'ı CSV olarak verir).

Günlük limit `profile.yaml → limits` altında: 10 başvuru ve 15 outreach. Limit aşılınca ajan uyarıyor.

## Kaynaklar

- **Public feed'ler:** Remotive, RemoteOK, We Work Remotely.
- **Hedef şirketler:** Greenhouse, Lever ve Ashby'nin herkese açık board API'leri. Şirketin kariyer URL'sindeki slug'ı `profile.yaml → companies` altına ekle. Bu, en yüksek getirili kaldıraç.
- **LinkedIn:** ajan sadece arama URL'si ve kişi arama URL'si üretiyor. Giriş yapmıyor, scraping yapmıyor.

## Ajanın yazdığı metinlerle ilgili kurallar

- Profilde olmayan hiçbir bilgiyi uydurmuyor. Bir rakam gerektiğinde metne `[ADD METRIC]` koyuyor; o boşluğu gerçek bir sayıyla sen dolduruyorsun.
- LinkedIn bağlantı notu 300 karakterde kesiliyor.
- Screening sonucu "skip" olan bir ilan için taslak üretmek istersen `--force` gerekiyor.

## Testler

```bash
python -m pytest -q
```

`data/` klasörü (veritabanı, taslaklar, digest'ler) git'e girmiyor; kişisel pipeline'ın bilgisayarında kalıyor.
