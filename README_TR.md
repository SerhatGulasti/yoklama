[EN](README.md) | **TR**

# Yoklama

Arch tabanlı sistemlerde hangi pacman paketinin en son ne zaman kullanıldığını
kaydeden küçük bir daemon. Paket dosyalarının açılışını izler, uzun süredir hiç
görülmeyen paketleri raporlar.

Yoklama hiçbir paketi kaldırmaz ve öneride bulunmaz. Yalnızca liste verir; listeyle
ne yapılacağı size kalmıştır.

## Gereksinimler

- pacman kullanan bir Linux sistemi, systemd
- Python 3.10 veya üstü (yalnız standart kütüphane)
- `/usr`, `/etc` ve `/opt` dizinlerinin kök bağlama noktasında olması
  (bkz. [Sınırlar](#sınırlar))

## Kurulum

```sh
sudo ./install.sh
sudo systemctl enable --now yoklama
```

`install.sh` dosyaları root sahipliğinde `/usr/local/lib/yoklama` altına kopyalar,
servis ve pacman hook'unu yerine koyar. Servisi kendisi etkinleştirmez veya başlatmaz.
Servis root olarak çalıştığı için kodu kullanıcının yazabildiği bir yerden çalıştırmaz.

Kurulumu root olmadan denemek için `DESTDIR=/bir/dizin ./install.sh` kullanılabilir.

## Kullanım

```sh
yoklama report                 # 60 gündür görülmeyen paketler
yoklama report --days 90       # eşiği değiştir
yoklama report --all           # bağımlılıklar dahil bütün paketler
yoklama report --json          # betikler için JSON çıktı
```

Rapor root istemez, salt okunurdur ve servisin o anda çalışmasını gerektirmez.
Varsayılan kapsam, açıkça kurulmuş ve başka hiçbir paketin bağımlı olmadığı
paketlerdir (`pacman -Qet`).

```text
Observing since 2026-11-05. Scope: explicit packages nothing depends on.
WARNING: Observed for only 0.2 days, under the 30 this report needs to mean much.
WARNING: Nothing can reach 60 idle days yet, so an empty list says nothing.

Not seen for 60+ days (0)
  (none)

Protected, do not touch (0)
  (none)

Not measurable by use (2)
  PACKAGE          LAST SEEN  LAST PROCESS  INSTALLED      SIZE
  man-pages        never      -             2026-10-28  5.7 MiB
  vlc-plugins-all  never      -             2026-10-28      0 B
```

Bir paket yalnızca bir bölümde görünür:

| Bölüm | İçerik |
| --- | --- |
| Not seen for N+ days | Eşiği aşan paketler, en eskisi başta. Asıl aday listesi budur. |
| Protected, do not touch | `protected.txt` ile eşleşen ve eşiği aşan paketler. |
| Not measurable by use | Eşikten bağımsız olarak, izlenebilir dosyası yalnız lisans, belge veya kılavuz olan ya da hiç olmayan paketler. Görülmemeleri kullanılmadıkları anlamına gelmez. |

Hiç görülmemiş bir paketin süresi, kurulum tarihi ile gözlemin başlangıcından hangisi
daha yeniyse ondan başlar. Kurulum tarihi `/var/log/pacman.log`'daki `installed`
satırlarından okunur, çünkü pacman'ın `%INSTALLDATE%` alanı her güncellemede değişir.

Gözlem 30 günden kısaysa rapor uyarı verir. Süre eşikten de kısaysa boş liste
hiçbir şey ifade etmez, çünkü henüz hiçbir paket eşiğe ulaşamaz.

## Nasıl çalışır

Daemon, kök bağlama noktasına bir fanotify işareti koyup dosya açılışlarını dinler.
Bir olay ancak şu elemelerden geçerse paketin kullanımı sayılır:

1. Olayı Yoklama'nın kendisi üretmemiş olmalı.
2. Pacman penceresinin dışında olmalı. Pencere, pacman `db.lck` kilidini tutarken
   ve durduktan sonraki 10 saniye boyunca açıktır. Pacman süreci olmayan bayat kilit
   pencereyi açmaz, yalnızca günlüğe uyarı yazar.
3. Dosya `/usr`, `/etc` veya `/opt` altında olmalı.
4. Dosya bir pakete ait olmalı ve `ignore_paths.txt`'teki bir dizinde olmamalı.
5. Dosyayı açan süreç `ignore_procs.txt`'te olmamalı.

Geçen olaylar bellekte toplanır, 60 saniyede bir ve kapanışta SQLite'a yazılır
(`/var/lib/yoklama/yoklama.db`). Paket başına ilk ve son görülme zamanı, açılış sayısı
ve son süreç adı tutulur. Dosya-paket eşlemesi pacman'ın yerel veritabanından kurulur;
pacman hook'u her işlemden sonra servisi yeniden yükleyerek bu eşlemeyi tazeler.

| Modül | Görevi |
| --- | --- |
| `yoklama/watcher.py` | ctypes üzerinden fanotify |
| `yoklama/catalog.py` | Dosya-paket eşlemesi |
| `yoklama/daemon.py` | Olay filtresi, pacman penceresi, ana döngü |
| `yoklama/store.py` | SQLite kaydı |
| `yoklama/report.py` | `report` komutu |

Fanotify doğrudan kullanılıyor, çünkü FID işaretleri btrfs alt birimlerinde `EXDEV`
hatası veriyor ve fatrace bu yüzden onlardan olay göremiyor.

## Yapılandırma

Dosyalar repoda durur, ama servis ve rapor `/usr/local/lib/yoklama` altındaki
kopyaları okur. Düzenledikten sonra:

```sh
sudo ./install.sh
sudo systemctl reload yoklama
```

| Dosya | İçerik | Kullanan |
| --- | --- | --- |
| `ignore_procs.txt` | Açtığı dosyalar kullanım sayılmayan süreçler. İlk 15 karakter eşleştirilir, çünkü çekirdek süreç adlarını orada keser. | daemon |
| `ignore_paths.txt` | Toplu taranan dizinler. Altındaki dosyaların açılışı kullanım sayılmaz. | daemon, rapor |
| `protected.txt` | Raporda ayrı tutulan paketler. Satır başına bir glob. | rapor |

Kod değiştiyse `reload` yetmez, `sudo systemctl restart yoklama` gerekir.

`yoklama run` seçenekleri: `--db`, `--config-dir`, `--flush-interval` (saniye) ve her
yazımda kaynak kullanımını günlüğe yazan `--stats`. Günlükler için
`journalctl -u yoklama`.

## Sınırlar

Rapor kesin hüküm değil, ipucudur. Paketi kaldırmadan önce kendiniz kontrol edin.

- **Görülmemek, kullanılmamak demek değildir.** Açılışta servisten önce okunan ve
  çekirdeğin kendisinin yüklediği dosyalar kaçabilir. `amd-ucode`, `sof-firmware` ve
  `bluez-hid2hci` gibi paketler bu yüzden aday listesine düşebilir. Dosyalarını servis
  başlamadan açmış ve bir daha açmayan uzun ömürlü süreçler de görünmez.
- **Görülmek, kullanılmak demek değildir.** Herhangi bir süreç dosyayı açtığında
  sayılır. Dosya tarayan bir uygulama `ignore_procs.txt`'te değilse, taradığı paketler
  kullanılmış görünür. Bunu anlamak için `yoklama report --all --days 0` çıktısındaki
  `LAST PROCESS` sütununa bakılabilir.
- **Yalnız kök bağlama noktası izlenir.** `/usr`, `/etc` veya `/opt` ayrı bir bağlama
  noktasındaysa oradaki açılışlar görünmez. Aynı nedenle servise `ProtectSystem` veya
  `PrivateTmp` gibi kendi mount namespace'ini açan ayarlar eklenmemelidir
  (bkz. `systemd/yoklama.service`).
- **Servis kapalıyken gözlem yapılmaz.** Kapanma süresi gözlem süresinden düşülmez.
- **Az da olsa veri kaybı olabilir.** Süreç öldürülürse son yazımdan sonraki kayıtlar
  gider. Fanotify kuyruğu taşarsa o olaylar kaybolur ve günlüğe uyarı düşer.

## Geliştirme

```sh
python3 -B -m unittest discover -s tests
```

Testler root ve çalışan servis gerektirmez.

## Kaldırma

```sh
sudo systemctl disable --now yoklama
sudo rm -r /usr/local/lib/yoklama
sudo rm /usr/local/bin/yoklama /etc/systemd/system/yoklama.service /etc/pacman.d/hooks/yoklama.hook
sudo systemctl daemon-reload
```

Toplanan veriler `/var/lib/yoklama` altında kalır; istemiyorsanız onu da silin.
