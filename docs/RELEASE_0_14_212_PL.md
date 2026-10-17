# HomeMind 0.14.212: szybsze obliczenia Shadow

Zmiana obejmuje Candidate Shadow i Sensor Tournament. Główny agent w trybie
Shadow nadal przechodzi przez dotychczasową ścieżkę decyzji i walidację Shadow
Executor. Kandydaci pozostają obserwatorami bez prawa wysyłania usług HA.

## Reakcja pasywnego Candidate Shadow

Zdarzenie HA budzi istniejący worker kandydatów. Jeśli jednak worker obudził
się przed końcem 350-ms scalania zdarzeń lub 500-ms wyciszenia po świeżym
zdarzeniu, wcześniej wracał do domyślnego, 3-sekundowego oczekiwania.
Pojedyncza zmiana mogła więc czekać około 3 sekund pomimo wybudzenia workera.

Worker wylicza teraz najbliższy termin z kolejki RAM i czeka do niego.
Pozostają dotychczasowe okresy scalania i wyciszenia, pierwszeństwo zdarzeń,
deduplikacja względem decyzji głównego agenta, heartbeat 30 s i ochrona przed
zagłodzeniem przy ciągłym ruchu HA. Pusta kolejka zachowuje oszczędny polling.
Sygnał następnej partii pracy ustawiony podczas maintenance pozostaje aktywny.

## Cechy kandydatów i screening

- Cztery generacje kandydata mają osobne modele, predykcje i wyniki. Mogą
  współdzielić rekonstrukcję tych samych sensorów z jednego zdarzenia.
  Cache jest ograniczony do 256 deskryptorów, lokalny dla wątku i pojedynczej
  obserwacji. Klucz uwzględnia semantykę celu, wersję cech, lagi i wykluczenia.
  Metadane zwracane do modeli mają niezależne kopie.
- Kontekst odroczonej decyzji nadal używa czasu i zamrożonego Home Context
  głównego agenta. Kolejne zdarzenie otrzymuje nowy cache. Nakładki Explore
  oraz Change decision przekazują ten sam kontekst do predykcji.
- Screening Sensor Tournament liczy wspólne momenty kompletnych etykiet
  raz w danym wywołaniu. Braki i niepoprawne wartości zachowują obliczenia
  parami. Kolejność sumowania, wyniki korelacji, wynik screeningu i uczenie
  po każdej nowej etykiecie pozostają zgodne z wcześniejszą implementacją.

## Weryfikacja

`tools/benchmark_candidate_shadow_reaction.py` porównuje niezależne źródła
0.14.211 i 0.14.212. Tryb `--worker` uruchamia prawdziwą pętlę workera i
deterministyczną politykę testową, aby oddzielić oczekiwanie od obliczeń.
Tryb zwykły uruchamia cztery rzeczywiste polityki, porównując dokładne
wektory, etykiety, metadane oraz predykcje. Oba sprawdzają brak wywołań HA.

`tools/benchmark_screening_reaction.py` dodatkowo sprawdza rzeczywiste
uczenie czterech kandydatów po nowych etykietach, 96 sensorów i po 96
przykładów w puli. Porównywane są pełne wyniki, zawartość puli SQLite i
wyuczone głowice. Sześć nowych regresji obejmuje także sygnały wybudzenia,
wyciszenie po zdarzeniu, odczyty wyłącznie z RAM i statystyki z brakami.

Wyniki końcowe i warunki pomiaru znajdują się w
`docs/benchmarks/SHADOW_REACTION_0_14_212_*.json`.
Pełny zestaw: **2557 testów OK**, bez pominięć, Python 3.12.3 i NumPy 2.3.3.
Porównanie trzech naprzemiennych par niezależnych źródeł:

| Zakres pomiaru | Wynik na hoście |
| --- | --- |
| Zdarzenie do pasywnego kandydata, prawdziwa pętla workera, polityka testowa | około 3004 ms do 503 ms, 5,98x |
| Pakiet czterech rzeczywistych kandydatów | 1,27x, około 21% krótszy czas, 4x mniej rekonstrukcji sensorów |
| Screening sensorów | 1,25x, identyczny wynik statystyczny |
| Cały observer Tournament z uczeniem i zapisem | 1,016x; niewielki wynik tego scenariusza, bez deklaracji istotnego przyspieszenia całej instalacji |

Test głównego Shadow dostarczył wszystkie 32 zdarzenia do rzeczywistego
Shadow Executor bez usług HA. Kontrola Control z równoległym GC obsłużyła
64 zdarzenia i dokładnie jedną usługę do atrapy HA. Test startu potwierdził
gotowość wersji 0.14.212; kompilacja źródeł, składnia 21 skryptów JS i
zgodność 585 znormalizowanych plików kodu przeszły kontrolę.

Pomiary hosta nie są wynikiem Raspberry Pi ani pomiarem fizycznego ACK.
Przyspieszenie kandydatów nie stanowi dowodu skrócenia reakcji głównego
agenta lub urządzenia o ten sam współczynnik.

Instalacja wymaga aktualizacji dodatku i restartu. Full Rebuild nie jest
wymagany: format modeli i zapisanej historii pozostaje zgodny. Zawiera też
automatyczne odzyskiwanie miejsca z 0.14.211, przy zachowanych limitach
bazy, WAL, wolnego miejsca, pamięci oraz konfiguracji 14-dniowej historii.
