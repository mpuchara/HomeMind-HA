# HomeMind 0.14.211: automatyczne odzyskiwanie miejsca i priorytet decyzji

Trzy eksporty z 17.10.2026, 13:35:24–13:37:47, potwierdzają konfigurację
`storage_auto_use_percent: 60`. Baza zajmuje około 10,73 GB użytych stron,
przy limicie 11,92 GB. Dotychczasowy próg pauzy 90% wypada przy 10,7288 GB;
zajętość oscyluje zaledwie kilkadziesiąt kB wokół niego. Na dysku pozostaje
około 10,14 GB wolnego miejsca, powyżej rezerwy 8,37 GB.

Dotychczasowa retencja działa: usunęła ponad 588 tys. wierszy. Obejmuje
jednak głównie wiek 14 dni i nie utrzymuje zapasu pod bieżące nagrania.
Sama zmiana procentowego przydziału nie ogranicza tempa przyrostu danych.

## Odzyskiwanie w tle

- Worker rozpoczyna wcześniejsze odzyskiwanie przy 85% użytych stron bazy
  i dąży do 80%. Dotychczasowy limit i próg pauzy 90% pozostają w mocy.
  Histereza ogranicza ciągłe zatrzymywanie i wznawianie zapisu.
- Zwykłe, zbędne dane diagnostyczne mają domyślnie 24 godziny retencji.
  Pod presją miejsca można zebrać starsze niż godzinę. Konfiguracja:
  `storage_diagnostic_retention_hours: 24` i
  `storage_pressure_retention_hours: 1`, zakres 1–336 godzin.
- Zbierane są niepowiązane zwykłe decyzje SHADOW/REJECTED, pomocnicze
  zdarzenia Shadow, stare logi info/debug i niepotrzebny dziennik cech.
  Decyzje Teach, eksperymenty, epizody, komendy i doświadczenia pozostają
  chronione. Logi warning/error zachowują dotychczasową retencję.
- Przyszły termin ochrony zdarzenia cechy obowiązuje, gdy istnieje pasujące
  aktywne okno. Osierocony termin po usuniętym oknie nie blokuje sprzątania.
  Indeks czasu okien eliminuje przeglądanie wszystkich świeżych okien dla
  każdego starego zdarzenia. Indeks powstaje addytywnie w tle.
- Historia odtwarzania w `entity_history`, modele, preferencje oraz dowody
  kwalifikacji i kalibracji zachowują dotychczasową politykę. Okres treningu
  i surowej historii pozostaje 14-dniowy, zgodnie z istniejącą konfiguracją.
- Każda transakcja wybiera najwyżej 128 kandydatów przed sprawdzeniem
  powiązań. Usuwanie dzieci okna ma dodatkowy limit 4096 wierszy. Worker
  nie czeka na zajęty główny lock Store i przekazuje sterowanie między
  transakcjami. Budżet 80 ms jest kooperacyjny: powolne I/O lub pierwsze
  utworzenie indeksu może wydłużyć pojedynczą operację. Co najmniej jedna
  mała partia ma możliwość postępu także po powolnym odczycie metadanych.
- Presja WAL czeka na istniejący checkpoint, zamiast powodować dodatkowe
  kasowanie. Nieznane miejsce na dysku nadal blokuje zapisy bezpiecznie.

Zwolnione strony są ponownie używane przez SQLite. Plik bazy może zmniejszać
się wolniej, przez istniejące stopniowe odzyskiwanie stron. Postęp w budżecie
jest mierzony zajętymi stronami, a nie wyłącznie długością pliku.
Gdy pozostają wyłącznie chronione dane, worker raportuje to i zachowuje
ochronę pojemności. Sprzątanie nie może zagwarantować pomieszczenia
dowolnej ilości niezbędnej historii w dowolnie małym limicie.

## Szybsza obsługa zdarzeń

Autorytatywna decyzja i Executor zachowują pierwszeństwo. Opcjonalny Sensor
Tournament pomija nowe obliczenia podczas pauzy zapisu. Dotychczasowe,
zaakceptowane obserwacje pozostają w ograniczonych buforach i czekają na
dokładny zapis. Przejście przez lukę czyści konteksty przyszłych etykiet,
lagów i porównań, zachowując wyuczone modele i zaakceptowane przykłady.
Wznowienie korzysta z nowej obserwacji; nie odtwarza decyzji z pominiętej luki.
Ta wersja obejmuje także mechanizm przygotowany wcześniej w pracach 0.14.209.

W zdrowym trybie opcjonalny obserwator ustępuje także drugiemu wątkowi,
który właśnie liczy autorytatywną decyzję. Zdarzenia zmiany urządzenia
zachowują pierwszeństwo, a istniejące maksymalne odroczenie chroni przed
zagłodzeniem obserwacji. Zmienia się dobór opcjonalnych kontekstów przy
obciążeniu; późniejsze uczenie kandydatów może więc otrzymać mniej takich
kontekstów. Reguły decyzji Live, uczenia na zaakceptowanych etykietach,
promocji oraz zabezpieczenia rzeczywistych akcji pozostają zachowane.

Runtime debug pokazuje rzeczywisty przydział, postęp sprzątania, liczbę
usuniętych wierszy i zajęte strony. Po odzyskaniu miejsca pozostaje informacja
o ukończonym sprzątaniu zamiast komunikatu sugerującego zwiększenie procentu.

## Walidacja i instalacja

Dowody: `docs/benchmarks/STORAGE_RECOVERY_0_14_211_*.json`.
Testy obejmują prawdziwe limity stron SQLite, samoczynny start workera,
aktywne i osierocone okna, referencje do komend i uczenia, rollback,
postęp za chronioną partią, powolne metadane oraz priorytet dwóch wątków.
Osobna próba kontroluje zachowanie 128 zapisanych wcześniej obserwacji
Candidate po rzeczywistym odzyskaniu stron, bez zmiany kwoty i powtarzania
inferencji. Porównanie toru Control używa prawdziwego Engine i Executor,
z kontrolowaną predykcją Root oraz atrapą sieci HA.

Bazą jest 0.14.210 i PR #326. Zapisane ustawienie 60% jest zachowane.
Zmiana nie wymaga Full Rebuild ani resetowania wyuczonych modeli.
Eksporty nadal nie zawierają fizycznych wykonań HA i pasujących ACK.
Wyniki hosta testowego nie są pomiarem szybkości Raspberry Pi ani lampy.

Pełny zestaw: 2551 testów na Linuksie, wszystkie zaliczone, w tym 32 dodane
regresje. Końcowa optymalizacja indeksu okien ma osobne 22 zaliczone testy
z planem zapytania SQLite. W małej kontrolowanej bazie zużyte strony spadły
z 4 870 144 do 3 817 472 bajtów przy niezmienionej kwocie; wszystkie 128
wcześniej zatrzymanych obserwacji Candidate zapisano dokładnie, bez ponawiania
polityki. Równoległe GC i prawdziwy Engine/Executor obsłużyły 64 zdarzenia
i jedno dozwolone light.turn_on do atrapy HA, z medianą 6.0 ms i p95 7.7 ms
do Executor na hoście testowym. Krótka próba porównawcza przy domyślnej
kadencji nie wykazała wiarygodnego przyspieszenia czasu reakcji (mediana
przyspieszenia 0,906x). Pomijanie zbędnej pracy obserwatora nie jest samo
w sobie dowodem przyspieszenia całej instalacji. Wymaga to kolejnych pomiarów
z działającej instalacji po aktualizacji.
