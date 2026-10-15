# HomeMind-HA 0.14.201

Wersja ogranicza powtarzane odczyty SQL w Sensor Tournament oraz skraca idealny
interwał oddawania czasu pomiędzy wątkami Pythona w głównym procesie runtime.
Aktualizacja wymaga restartu dodatku. Modele i historia zachowują format;
Full Rebuild nie jest potrzebny.

## Ustalenia z eksportów 09:44

Oba eksporty pochodzą z 0.14.200. Cache SQLite działa: 10536 trafień przy
5 otwarciach, brak bypassów, 128 MiB przydzielonych celów cache z dostępnego
budżetu 256 MiB. RSS wynosi 212–224 MiB. Storage ma stan normal i zero
zablokowanych zapisów. Poprzednia zmiana usunęła ponowne otwieranie połączeń,
ale nie rozwiązała opóźnienia całego pipeline.

Średni `wrapped_inference` nadal wynosi około 331 ms, natomiast
`context_shadow_observation` około 229 ms. Ostatni trace zawiera 37 obserwacji,
148 odczytów historii schematu i 259 odczytów stanu promocji, również dla
kandydatów niespełniających podstawowych warunków awansu. Odczyty te występują
w tej samej ścieżce, która liczy i sprawdza czterech kandydatów.

Wielomilisekundowe czasy krótkich odczytów i natywnego SHA są zgodne zarówno
z oczekiwaniem na storage, jak i na ponowne wykonanie wątku. Dotychczasowy
eksport nie rozdzielał tych przyczyn. Test obciążenia w tle potwierdza znaczenie
współdzielenia interpretera; nie dowodzi, jaka część opóźnienia na urządzeniu
wynikała konkretnie z GIL.

## Zmiany

- `runtime_thread_switch_ms`: domyślnie 1 ms, zakres 0–10. Opcja 0 pozostawia
  ustawienie interpretera bez zmiany, a 5 odtwarza interwał użyty przez bazowy
  CPython w benchmarku. Ustawienie stosuje wyłącznie główny entrypoint runtime
  przed uruchomieniem workerów; izolowane procesy treningu zachowują swój profil.
- Kandydat bez wymaganych próbek, dni, zysku lub wygranych okien kończy próbę
  promocji na bramkach RAM. Kandydat spełniający te warunki nadal odczytuje
  świeży cooldown SQLite przed jakąkolwiek zmianą schematu. Status/UI nadal
  czyta aktualne dane.
- Probation i requalification współdzielą jeden odczyt historii przed obserwacją
  i osobny, świeży odczyt po niej. Zapis historii albo zmiana jej statusu przez
  service unieważnia kopie po udanym commit, także pomiędzy wątkami. Wyjątek
  usuwa kontekst obserwacji. Zwykłe odczyty poza nim pozostają świeże.
- Zweryfikowany, rozgrzany kandydat nie kopiuje całej mapy stanów i registry
  przy każdym użyciu. Kopie nadal powstają podczas konstrukcji i odtwarzania
  polityki. Każde użycie modelu nadal wykonuje pełne sprawdzenie SHA.
- Diagnostyka zawiera `runtime_threads.actual_ms`, liczniki pominiętych odczytów
  cooldownu, `context_persistence.schema_history_reads` oraz `thread_cpu_ms`
  i `off_cpu_ms` w zakończonych spanach tego samego wątku. `off_cpu_ms` obejmuje
  łącznie I/O, blokady, planowanie OS i oczekiwanie na interpreter; nie jest
  osobnym pomiarem czasu GIL ani karty SD. Eksport pozostaje RAM-only.

Według [dokumentacji CPython](https://docs.python.org/3.12/library/sys.html#sys.setswitchinterval)
interwał przełączania określa idealny czas odcinka pracy wątku. Nie stanowi
deadline'u, nie skraca długiej funkcji natywnej i nie wybiera następnego wątku
za system operacyjny. Niceness procesu, native busy timeout, Executor,
kolejność predict/score/learn i wszystkie limity storage pozostają zachowane.

## Pomiary i ograniczenia

Pełna regresja Linux/Python 3.12.3: **2396 testów OK**, w tym 16 nowych.
Compileall, składnia JavaScript diagnostyki i rzeczywisty entrypoint: OK.

Raport `docs/benchmarks/RUNTIME_HOT_PATH_0_14_201_VALIDATION.json` zawiera
surowe czasy, hashe eksportów oraz porównanie dwóch niezależnych checkoutów
0.14.200 i 0.14.201. Fixture używa czterech rzeczywistych kandydatów, 24 wierszy
historii na sensor tak jak Engine, RAM-first bufora, pełnego SHA, rzeczywistych
wektorów, etykiet, metadanych i predykcji. Wszystkie wyniki są identyczne.

Przy dodatkowym wątku CPU mediana przyspieszenia całej obserwacji fixture
wynosi 4,23× w trzech parach po 24 obserwacje. Mediany pojedynczych obserwacji
w parach wynoszą 61–78 ms przed zmianą i 15–28 ms po zmianie.
W tym teście zwiększa się udział CPU dostępny dla szybko kończącej się pracy
foreground: tempo syntetycznej pracy tła jest niższe. Raport zachowuje również
ten wynik, bez przedstawiania go jako darmowego wzrostu całkowitej przepustowości.

Bez dodatkowego wątku CPU efekt jest bliski neutralnemu: mediana 0,95×,
czyli około 5% dłuższy łączny czas w tym krótkim teście hosta. Sama ekstrakcja
cech nie otrzymuje deklaracji przyspieszenia. Benchmark nie zawiera HA ingress,
rzeczywistej karty SD ani równoległego checkpointu. 4,23× nie jest prognozą
przyspieszenia całej instalacji. Wpływ na urządzenie wymaga nowego eksportu
po aktualizacji, zwłaszcza `event_to_decision`, `wrapped_inference`, długości
kolejek i nowych pól CPU/off-CPU.
