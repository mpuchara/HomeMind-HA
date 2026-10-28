# HomeMind-HA 0.14.231: inferencja wielu agentów

Baza: `d460e0e10f18bcfebcd4565dcfdf54bf8782157b`, HEAD 0.14.230 z PR #346.
Gałąź: `codex/inference-capture-0.14.231`. [PR #347](https://github.com/mpuchara/HomeMind-HA/pull/347) celuje w `codex/performance-capture-20261027`.

## Zmiana i uzasadnienie pomiarami

`Engine.run()` potrzebował tylko sprawdzenia obecności stanów, ale kopiował całą mapę przed `process()`, które i tak wykonywało nowy atomowy snapshot. Pętla przekazuje teraz `None`; jedyna autorytatywna kopia stanów nadal powstaje w `process()` razem z rewizjami encji, kontekstu i zegarami odbioru. Event, timer i startup zachowują tę samą ścieżkę. Kolejność retry/FIFO, rewalidacja Executor i zapisy Control pozostają bez zmian.

Profil bazowy 65 przechwyceń przy 780 encjach: `_capture` 0,490 s łącznie, cache historii 0,230 s, cache map 0,122 s, grupowanie współdzielonych rekordów 0,048 s (cProfile, pomiar diagnostyczny). Istniejące cache już unikają serializacji niezmienionych rekordów. W cache map usunięto dodatkowe przebiegi sprawdzania przynależności i sumowania rozmiarów stałych kontenerów. Flaga kwalifikacji jest zbierana w istniejącym przebiegu; rozmiary są liczone zachowawczo. Pozostają te same limity bajtów/liczby rekordów, porównania zagnieżdżonych wartości, silne świadki tożsamości i zasady zwalniania danych.

Nie dodano kolejnego cache modelu. Stała tożsamość championa nie dowodzi niezmienności wag przy lazy decay i uczeniu. Nadal serializowany jest dokładny stan polityki pod jej blokadą; zakres algorytmów, cech, rewardów, Correct, Candidate, Shadow i trwałości danych pozostaje bez zmian.

Pięć naprzemiennych par samego `run()` + `process()`, 5000 przebiegów, zwykły produkcyjny `dict`, 780 encji i cztery grupy targetów: mediana ilorazów czasu po/przed **0.912**, czyli **8.8% krócej**. Testy behawioralne osobno potwierdzają zmianę liczby kopii z dwóch na jedną. Ten benchmark nie wykonuje inferencji.

Trzy pary pełnego `_capture`, 80 ciepłych przechwyceń na serię, 780 encji: mediana ilorazów czasu **0.958**, czyli **4.2% krócej**. Każda klatka przyczynowa ma identyczny hash przed i po, łącznie z historią, stanami, registry, polityką, prognozą, rewizją i generacją.

## Pełny złożony runtime: przed → po

Host Windows/WSL Ubuntu, Python 3.12, NumPy 2.3.3. Shipped `RuntimeCompositionRoot`, rzeczywiste `Engine.run` i ingress, normalne chronologicznie dopasowane i kalibrowane `MultiHorizonPolicy`, tryb Shadow, 780 encji, tracing wyłączony. Bez wymuszania confidence, wyniku ani otwierania gate. Sieć HA, timerowe maintenance i zewnętrzny trening nie należą do tego izolowanego workloadu zdarzeń.

Macierz potwierdzająca: 1/4/5/10/20 agentów, wspólne i niezależne sensory, trzy naprzemienne pary na konfigurację, 24 zdarzenia rozgrzewki + 300 mierzonych, pauza 20 ms po zakończeniu przebiegu. Całe skończone okno próbek jest zachowane w narzędziu benchmarkowym; produkcyjny ring 512 pozostaje bez zmian. Procentyle łączą serie i agentów, więc są opisowe i skorelowane. Nie stanowią sprzętowej/statystycznej akceptacji p99.

| Agenci | Sensory | p50 ms | p95 ms | p99 ms | Mediana ilorazów p95 par |
|---:|---|---:|---:|---:|---:|
| 1 | wspólne | 2.611 → 2.536 | 3.525 → 3.312 | 4.261 → 4.061 | 1.018 |
| 1 | niezależne | 2.540 → 2.576 | 3.333 → 3.362 | 4.136 → 4.076 | 0.967 |
| 4 | wspólne | 10.509 → 11.046 | 16.921 → 18.471 | 20.975 → 23.424 | 1.059 |
| 4 | niezależne | 10.858 → 11.020 | 18.277 → 18.547 | 23.001 → 23.764 | 1.006 |
| 5 | wspólne | 13.969 → 14.074 | 24.946 → 27.928 | 37.393 → 42.068 | 1.021 |
| 5 | niezależne | 14.144 → 14.205 | 27.858 → 25.675 | 40.466 → 34.077 | 0.898 |
| 10 | wspólne | 23.777 → 23.206 | 48.894 → 45.412 | 72.185 → 57.955 | 0.961 |
| 10 | niezależne | 25.489 → 27.481 | 52.106 → 60.170 | 68.232 → 71.512 | 1.080 |
| 20 | wspólne | 59.731 → 64.695 | 123.263 → 122.578 | 142.393 → 139.887 | 1.001 |
| 20 | niezależne | 55.192 → 58.173 | 127.234 → 123.158 | 153.867 → 141.287 | 1.050 |

CPU to czas procesu podzielony przez liczbę decyzji; obejmuje pracę skomponowanego runtime i końcowe opróżnienie, bez przygotowania modeli. RSS i peak RSS dotyczą całego procesu benchmarkowego, w tym wcześniej dopasowanych modeli. Capture jest mierzone w rzeczywistym torze Tournament, z zachowaniem istniejącego próbkowania; liczby przechwyceń są w JSON.

| Agenci | Sensory | CPU ms / decyzję | RSS MiB | Peak RSS MiB | Capture p50 ms | Capture p95 ms |
|---:|---|---:|---:|---:|---:|---:|
| 1 | wspólne | 4.088 → 4.107 | 64.73 → 64.66 | 64.31 → 64.45 | 1.098 → 0.944 | 1.303 → 1.213 |
| 1 | niezależne | 4.044 → 4.115 | 64.76 → 64.56 | 64.38 → 64.44 | 1.160 → 1.017 | 1.384 → 1.263 |
| 4 | wspólne | 5.493 → 5.833 | 75.40 → 72.84 | 75.91 → 76.38 | 1.370 → 1.182 | 1.721 → 1.621 |
| 4 | niezależne | 5.866 → 5.938 | 79.12 → 79.00 | 79.87 → 79.03 | 1.801 → 1.710 | 2.169 → 2.293 |
| 5 | wspólne | 6.074 → 6.419 | 79.52 → 79.48 | 79.12 → 79.32 | 1.426 → 1.268 | 2.107 → 2.421 |
| 5 | niezależne | 6.450 → 6.312 | 85.12 → 84.93 | 85.08 → 84.70 | 2.045 → 1.889 | 3.535 → 2.492 |
| 10 | wspólne | 6.010 → 5.918 | 96.53 → 97.64 | 95.96 → 97.31 | 1.362 → 1.193 | 2.189 → 1.640 |
| 10 | niezależne | 6.537 → 6.325 | 103.73 → 93.05 | 106.68 → 108.91 | 2.659 → 2.521 | 4.198 → 3.484 |
| 20 | wspólne | 8.175 → 8.361 | 130.23 → 115.39 | 133.28 → 128.43 | 1.287 → 0.918 | 1.931 → 1.716 |
| 20 | niezależne | 7.700 → 8.610 | 117.98 → 125.87 | 157.00 → 157.05 | 1.533 → 1.059 | 7.131 → 6.304 |

Wszystkie 30 par macierzy potwierdzającej i 30 par pilota zachowują identyczne hashe decyzji Root i zero wywołań HA. Zgodność pełnych klatek Tournament sprawdzono osobnym benchmarkiem capture; sama zgodność decyzji Root jej nie zastępuje.

## Pilot i badanie zmienności

Zachowano również wszystkie wyniki pierwszej macierzy (12 warmup + 120 zdarzeń, trzy pary), z historycznym ringiem 512 dla event→decision. Pilot pokazał mieszane wyniki: m.in. jeden agent +6–9% p95, pięć agentów około 13–16% poprawy. Dla 20 wspólnych agentów p95 event→decision z końca okna rosło, mimo prawie identycznego p95 całego pipeline. To było powodem powtórzenia całej macierzy z pełnym oknem.

Dodatkowo wykonano trzy pary po 1000 zdarzeń dla jednego agenta w obu topologiach: wspólne sensory p95 3,900 → 4,201 ms; niezależne 4,783 → 4,459 ms. Wyniki pokazują zmienność hosta i nie uzasadniają obietnicy jednakowego przyspieszenia każdej konfiguracji. Pierwsze wyniki nie zostały usunięte ani zastąpione wyłącznie korzystnymi seriami. Szczegółowe ilorazy i wszystkie serie są w JSON.

Kontrola identycznego kodu: pięć par dla 4 wspólnych agentów dało ilorazy p95 0,916–0,977 i CPU 0,955–1,014; trzy pary dla 20 niezależnych agentów dały p95 0,728–0,893 i CPU 0,722–1,002. Ponieważ etykiety oznaczają identyczne źródła, jest to obserwowana zmienność workloadu/hosta, a nie efekt wersji. W macierzy mediana ilorazów p95 par wynosi od 0,898 do 1,080. Zagregowane p95 nadal rośnie m.in. o 12,0% dla 5 wspólnych i o 15,5% dla 10 niezależnych agentów; mediana CPU rośnie o 11,8% dla 20 niezależnych. Liczby przechwyconych/przetworzonych opcjonalnych klatek i ich nakład różnią się z czasem wykonania. W pilocie kierunki części tych różnic były odwrotne, a identyczny kod wykazał większy rozrzut. Nie zidentyfikowano odtwarzalnej dużej regresji przypisanej zmianie źródeł, ale nie wykazano też jednolitego przyspieszenia całego runtime. Potwierdzony zysk dotyczy schedulera i capture. To kandydat testowy do dalszej akceptacji na urządzeniu; p99 i uniwersalna przewaga runtime pozostają bez deklaracji.

Diagnostyka wątków wykorzystuje istniejący, ograniczony `RuntimeDebugTrace` z czasem CPU wątku/procesu. Dodane próby podpinania profilerów do wątków wycofano po błędach na Python 3.12; żadna z nich nie jest dowodem akceptacji. Ograniczenie równoległych instancji cProfile opisuje [zgłoszenie CPython](https://github.com/python/cpython/issues/110770). Diagnostyczne przebiegi z tracingiem nie należą do porównań wydajności bez tracingu.

## Testy i CI

Pełne końcowe CI: Python 3.11 - 2822 testy OK / 158,431 s; Python 3.13 - 2822 testy OK / 266,084 s. Wszystkie dodatkowe kroki benchmarków, składni, entrypoint i job image zakończyły się sukcesem. Końcowy push candidate_packages również zakończył się sukcesem, budując oba layouty istniejącym skryptem. Lokalnie pięć nowych regresji: OK. Nie powtarzano lokalnie pełnych suit po zielonych pełnych testach CI.

[Pełne CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/40665140650) sprawdza Python 3.11/3.13, istniejące benchmarki regresyjne, źródłowy entrypoint, składnię Python/JS, budowę/start obrazu Docker i trener w obrazie. Pięć nowych regresji obejmuje pojedynczy snapshot, odłączenie stanów/rewizji, zamkniętą bramkę, timer bez zegara eventu oraz limity i zagnieżdżone mutacje cache. Dwie starsze asercje tekstowe zaktualizowano do nowego wywołania `process(None, ...)`, zachowując ich pierwotne wymagania kolejności i zegarów.

## Paczki i instalacja

[Repository ZIP](https://github.com/mpuchara/HomeMind-HA/releases/download/v0.14.231-rc.1/HomeMind-HA-0.14.231-repository.zip) · [Addon ZIP](https://github.com/mpuchara/HomeMind-HA/releases/download/v0.14.231-rc.1/HomeMind-HA-0.14.231-addon.zip).

Obie paczki powstają istniejącym `python tools/package_install_release.py` z dokładnych bajtów zatwierdzonego HEAD. Zawierają `SOURCE_COMMIT.txt` i `MANIFEST.sha256`; skrypt sprawdza CRC, każdą zawartość, wersję, layout i LF w run.sh. Osobne pliki `.zip.sha256` są w wydaniu. Publikacja jest oznaczona jako kandydat testowy 0.14.231; nie wykonano merge do main.

Przed instalacją zachowaj kopię danych i poprzednią wersję. Repository ZIP zawiera całe repozytorium z katalogiem adaptive_ai; addon ZIP zawiera katalog główny dodatku. Zastąp źródła, przebuduj obraz dodatku i uruchom ponownie, zachowując dane. Sprawdź wersję 0.14.231 i SOURCE_COMMIT. Nie jest wymagane przebudowanie modeli. Rollback polega na przywróceniu poprzednich źródeł/obrazu z zachowaniem kopii danych.

## Dowody i granice

- `docs/benchmarks/PERFORMANCE_0_14_231_SCALING.json`: podsumowania, serie, CPU/RAM/capture, pilot i dodatkowy test jednego agenta.
- `docs/benchmarks/PERFORMANCE_0_14_231_RAW.json.gz`: wszystkie surowe próbki par runtime.
- `docs/benchmarks/PERFORMANCE_0_14_231_PROFILE.json`: profile diagnostyczne, oddzielne od pomiarów wydajności.
- `docs/benchmarks/PERFORMANCE_0_14_231_QA.json` i `PERFORMANCE_0_14_231_SOURCE_HASHES.json`: testy, CI i tożsamość źródeł.

To są pomiary hosta. Nie wykonano instalacji na Pi, testu rzeczywistego dispatch/state-ACK HA ani 24-godzinnego soak. Akceptacja tych ścieżek pozostaje oddzielna; nie jest zastępowana benchmarkiem Shadow ani CI.
