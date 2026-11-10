# HomeMind 0.15.0 — pomiar treningu na Raspberry Pi 5

Kandydat do testów. Rozpakuj archiwum dodatku w osobnym katalogu pod
`/addons/` tak, aby `config.yaml` był na pierwszym poziomie tego katalogu.
Odśwież sklep dodatków i sprawdź wersję przed uruchomieniem. Do porównania użyj
dwóch odizolowanych instalacji z tego samego stanu wejściowego, tą samą
konfiguracją agenta i tym samym zakresem historii. Zapisz pokrycie Recorder.

Uruchom poniższy skrypt 0.15.0 w przestrzeni procesu dodatku dla obu wersji.
W 0.14.240 skopiuj sam skrypt pomiarowy do katalogu tymczasowego i uruchom
go stamtąd. Skrypt wymaga dostępu do lokalnego portu 8099 i `/proc`.

```sh
python /app/pi_training_profile.py --scenario training --duration 18000 --interval 5 --output /tmp/training.json
```

Uruchom pomiar przed zleceniem jednego treningu i pozostaw działający do czasu
co najmniej jednego odczytu statusu po zakończeniu zadania. Czas pomiaru ustaw
tak, by objąć całe zadanie. `finished_training` zapisuje stan zadania, czasy
Recorder i workera, agregat sesji replay, ostatni fragment, budżet oraz
dostępne liczniki CPU, RSS i I/O. Timery poszczególnych faz mogą być zagnieżdżone.
Brak pola oznacza brak pomiaru. Czas od zlecenia do faktycznego startu zapisz
osobno na podstawie znaczników czasu z kolejki.

Zbierz co najmniej 100 rzeczywistych zdarzeń z decyzją w każdym scenariuszu
bez treningu, podczas replay oraz podczas serii zdarzeń. Zapisz p95, p99,
maksimum i liczbę **zdarzeń** w metryce event → decision. Kolejne odczyty
tego samego okna przez profiler nie są kolejnymi zdarzeniami.

Jeżeli wystąpią błędy Recorder, zachowaj czasy i kody HTTP oraz odpowiadający
im fragment logu HA Core. Usuń identyfikatory encji i dane prywatne przed
udostępnieniem logu. Porównuj czas treningu przy równoważnym pokryciu historii.
