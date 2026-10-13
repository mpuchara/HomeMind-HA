# 0.14.191: ograniczony RAM zamiast powtarzanych zapisów statystyk

Eksport .190 z 13 października pokazuje brak aktywnego treningu. RAM procesu wynosił około 136 MiB. Średnia obsługa całej decyzji wynosiła 278 ms, w tym kontekstowy Shadow 193 ms. Weryfikacja modelu kandydata zajmowała średnio 13 ms i powtarzała się 15 758 razy. Statystyki sensorów zapisano w 1443 operacjach; checkpoint WAL wykonał się 882 razy, średnio w 758 ms. Dane wskazują na koszt powtarzanych odczytów, przetwarzania i zapisów podczas normalnej pracy.

## Zmiany

Statystyki puli sensorów i ich jakości pozostają w RAM jako kompletne narastające stany. Zastąpienie starszej kopii nowszą zachowuje wszystkie zliczone okazje i utrwalone w niej próbki. Model kandydata, ocena etykiety, metody uczenia, marginesy Correct i kolejność decyzji pozostają na dotychczasowych ścieżkach.

Opcje dodatku `context_row_flush_seconds` (5–60, domyślnie 30) i `context_row_ram_mb` (1–64, domyślnie 16) określają odstęp oraz limit RAM na każdy bufor statystyk. Dwa obecne bufory mają więc domyślnie łączny limit 32 MiB i po 4096 wierszy. Limit obejmuje również wiersze aktualnie zapisywane. Zbliżenie do limitu przyspiesza zapis; pełny bufor wymusza zapis przez istniejącą ścieżkę SQLite. Nie powstaje żaden plik cache na karcie. Błąd zapisu zachowuje najnowsze przyjęte kopie do ponowienia i nie omija ochrony wolnego miejsca/WAL.

Writer sprawdza termin co pięć sekund, więc normalny odstęp zapisu przy domyślnych ustawieniach wynosi około 30–35 sekund, z możliwością dalszego oczekiwania na I/O lub ponowienie błędu. Jawne opróżnienie i poprawne zamknięcie zapisują statystyki niezależnie od terminu. Przy nagłym wyłączeniu mogą zniknąć nieutrwalone ostatnie statystyki puli/jakości. Zapis modeli i punktów Correct nie dostaje tego dodatkowego opóźnienia.

Liczba sensorów w puli jest wspólnie odczytywana w ramach jednej obserwacji. Cache jest lokalny dla wątku i usuwany także po błędzie. Następne zdarzenie ponownie czyta aktualne dane, a schemat jest świeży przy każdym odczycie. Ten cache dotyczy wyłącznie liczby sensorów, nie cech, predykcji ani bramek promocji.

Cache kodowania modelu sprawdza memo jednym przebiegiem, zamiast kopiować wszystkie obiekty i skanować je wielokrotnie. Przy każdym użyciu nadal powstaje świeży dowód pełnej zawartości i liczony jest SHA kanonicznego JSON. Zmiana wag, próbki, schematu lub oczekiwanej sumy jest wykrywana bez zmiany rewizji modelu.

Diagnostyka eksportuje zajętość/limity RAM, wiersze w trakcie zapisu, wymuszone opróżnienia i trafienia cache metadanych jednej obserwacji.

## Weryfikacja

Regresje sprawdzają scalanie 300 głosów, termin niezależny od nowych wpisów, limit RAM/wierszy, odmowę zbyt dużego wpisu, zachowanie nowszej kopii po błędzie SQLite, zapis modeli podczas odkładania statystyk i końcową barierę zapisu. Sześć odczytów liczby sensorów w jednej obserwacji wykonuje jedno zapytanie; nowy schemat i następna obserwacja pozostają świeże, również po wyjątku. Dotychczasowe regresje modelu zachowują pełny SHA i wykrywanie uszkodzeń.

Benchmark porównuje .190 i .191 w osobnych procesach na tym samym wejściu: 96 sensorów i 600 aktualizacji przez symulowane 60 sekund. Wynik końcowy liczników i etykiet ma identyczny SHA. Czas komponentów na komputerze testowym nie jest prognozą opóźnienia Raspberry Pi.

Pełny zestaw 2279/2279 testów przeszedł w Linuksie/WSL bez pominięć, wraz z siedmioma nowymi regresjami. Kompilacja modułów przeszła. W trzech porównaniach liczba zapisanych wierszy spadła z 1152 do 192, z identycznym wynikiem końcowym. Mediana przyspieszenia weryfikacji wyniosła 1.08×; to wynik komponentu na tym hoście. [Raport weryfikacji](benchmarks/RUNTIME_RAM_0_14_191_VALIDATION.json).

## Instalacja

Zainstaluj .191 i zrestartuj dodatek. Nie jest wymagany nowy trening ani Full Rebuild. Zacznij od domyślnych 16 MiB na bufor i 30 sekund. Kolejny eksport runtime pozwoli porównać `wrapped_inference`, `context_shadow_observation`, `context_candidate_validation`, `context_rows_persist`, checkpointy WAL i nowe liczniki RAM.
