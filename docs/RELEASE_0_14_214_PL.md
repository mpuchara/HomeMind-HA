# 0.14.214: szybsza obsługa kolejnych zdarzeń Control i Shadow

W pięciu dostarczonych eksportach 0.14.213 magazyn działa prawidłowo. To nie zatrzymanie przez limit dysku. Ostatni eksport pokazuje p95 event → decision 7952 ms, p95 event → target start 7752 ms i p95 samego inference 166 ms. Są to osobne rozkłady etapów, których percentyli nie należy dodawać.

Obserwator Sensor Tournament działał po pierwszej decyzji, ale nadal zajmował pracownika `device-control` i blokował obsługę następnego zdarzenia dla tego urządzenia. Jego ostatnie p95 wynosiło 10173 ms. Ślady pokazują między innymi wybór puli, uczenie klasyfikatora i synchroniczny flush bufora do SQLite. Zwiększenie samego cache SQLite nie usuwało tego oczekiwania.

## Zmiana

Control i Root Shadow po swojej decyzji przekazują obserwację do osobnego pracownika Tournament. Pracownik zdarzeń może obsłużyć następne zdarzenie. Candidate Shadow nadal korzysta ze swojego odrębnego mechanizmu pracy w tle; ta zmiana uwalnia ścieżkę jego rodzica. Nie zmienia reguł sterowania HA, progów pewności ani potwierdzenia OFF.

Kopia w RAM zachowuje stany sensorów, decyzję i pochodzenie zmiany Root, wagi modelu, historię czasową, prognozę domu oraz czas obserwacji. Weryfikacja checksum i uczenie kandydatów nadal obowiązują. Diagnostyczna funkcja przechwytująca cechy Root nie jest przenoszona do kopii. Przy ręcznej zmianie lub holdzie zachowujemy przyszłą etykietę i nowy czas obserwacji, mimo że Root świadomie utrzymał poprzednią decyzję.

Powtarzające się odświeżenia sensorów przy tej samej wartości urządzenia można scalić. Zaobserwowane zmiany wartości urządzenia pozostają w kolejności FIFO. Przekroczenie pojemności, błąd lub zmiana generacji oznaczają jawną przerwę: kolejne etykiety nie są naliczane przez brakujący fragment. Zachowujemy przyjęte próbki i nauczone modele.

## RAM i zabezpieczenia

Nowe ustawienie `runtime_tournament_ram_mb` ma domyślnie 64 MiB i zakres 1–256 MiB. To budżet naliczany konserwatywnie dla kolejki i aktywnej kopii, a nie cel procentowego wykorzystania całego RAM. Maksymalna liczba oczekujących kopii wynosi 64. Efektywny limit uwzględnia pamięć hosta i nadrzędnych cgroup: do 5% pamięci oraz do 25% wolnego miejsca ponad rezerwę 128 MiB. Sprawdzanie dostępnej pamięci odbywa się poza obsługą zdarzeń.

Pozostają limity bazy, WAL, minimalnego miejsca na dysku, payloadu i oryginalne bramki zapisu. Pracownik korzysta z istniejącego ograniczonego cache stron SQLite i zwalnia połączenie przed oczekiwaniem. Zamykanie aplikacji czeka na aktywną obserwację przed końcowym zapisem buforów.

Awans schematu i aktywna probation nadal przechodzą przez świeży, synchroniczny przebieg z istniejącymi cooldownami, rollbackiem i ponowną kwalifikacją Shadow. Taka operacja może nadal wydłużyć pojedynczy przebieg. Obliczenia natywne oraz blokady SQLite także pozostają wspólnym zasobem; zmiana nie ustanawia gwarantowanego terminu reakcji.

## Sprawdzenie

Podsumowanie rzeczywistych danych wejściowych: `benchmarks/TOURNAMENT_QUEUE_0_14_214_INPUT.json`. Wyniki testów, porównania z niezależnym źródłem 0.14.213 i hashe przetestowanego kodu: `benchmarks/TOURNAMENT_QUEUE_0_14_214_*.json`.

Porównania uruchamiają rzeczywistą kolejkę Engine, budowanie cech, czterech kandydatów i chroniony Executor. Akcja i pewność Root są deterministyczne. Scenariusze z dodatkowym oczekiwaniem 100 ms w obserwatorze izolują wpływ zajętego pracownika. To test hosta, z HA stubem: nie mierzy sieci HA, fizycznego ACK, czasu na Raspberry Pi ani trafności zainstalowanych modeli.

Pełny zestaw: **2579 testów OK**, bez pominięć. Przeszły także start finalnego entrypointu 0.14.214, produkcyjny kod JS w VM, składnia wszystkich 21 skryptów JS i compileall. Porównano 592 pliki kodu z przetestowaną kopią Linux.

Trzy naprzemienne porównania niezależnych źródeł, po 24 zdarzenia:

| Scenariusz hosta | p95 przed | p95 po |
| --- | ---: | ---: |
| Control, dodatkowe 100 ms w obserwatorze | 111,86 ms | 14,62 ms |
| Shadow, dodatkowe 100 ms w obserwatorze | 107,12 ms | 5,56 ms |
| Control, zwykły odstęp obserwacji, bez dodatkowego oczekiwania | 9,59 ms | 9,02 ms |

W tabeli podano medianę p95 z trzech przebiegów. Wszystkie 24 decyzje w każdej parze były identyczne; Control wykonał jedną usługę do stubu, Shadow zero. Przy obciążeniu scalono zbędne odświeżenia sensorów przy niezmienionym celu. Nie było błędów ani przerw; osobna regresja sprawdza zachowanie zmian wartości celu i porównuje dokładne parametry czterech kandydatów po pięciu przyszłych etykietach. Koszt kopii w tym fixture wynosi około 2–2,5 ms, a szczyt naliczanego budżetu około 4,1 MiB.

Control z rzeczywistym odzyskiwaniem miejsca obsłużył 64 zdarzenia i jedną usługę do stubu; osobny scenariusz Root Shadow obsłużył 32 zdarzenia bez usług HA. To nie dowód poprawy reakcji fizycznego urządzenia.

13 nowych regresji sprawdza między innymi brak blokowania kolejnych decyzji, identyczne cechy i nauczone głowy czterech kandydatów, kolejność etykiet, scalanie, braki danych, pamięć kontenera, zamrożony kontekst, zmianę generacji, feedback do właściwego modelu oraz odroczenie awansu.

## Aktualizacja

Zainstalować pakiet add-on 0.14.214 i zrestartować dodatek. Full Rebuild nie jest potrzebny. Nowe ustawienie RAM ma wartość domyślną, nie trzeba zwiększać limitu dysku dla tej zmiany.

Po aktualizacji porównać świeże 60-sekundowe okno `event_to_decision` i `event_to_target_start`. W eksporcie `engine.context_persistence.deferred_observations` pokazuje liczbę oczekujących kopii, używany budżet, czas tworzenia kopii, opóźnienie kolejki, scalania i przerwy. Długie `context_shadow_observation` może nadal oznaczać wolne uczenie w tle; istotne jest, czy nadal opóźnia ono kolejną decyzję Root. Pomiar fizycznego działania wymaga osobnych próbek service/ACK.
