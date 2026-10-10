# HomeMind Adaptive AI 0.14.170

Moduł promocji Sensor Tournament nie kopiuje już całego wytrenowanego modelu po każdej obserwacji, gdy nie zmieniły się próbki ani okna oceny. Takie kopie omijały istniejący minutowy zapis okresowych statystyk i generowały dodatkową pracę CPU, serializację JSON, zapisy SQLite i checkpointy WAL.

## Co pozostaje zachowane

Każda obserwacja nadal aktualizuje dostępność czujników w RAM i uczestniczy w predykcji. Nowa niezależna próbka, inicjalizacja lub zmiana konfiguracji okna oraz zamknięcie okna od razu kolejkują pełny snapshot wraz z końcowymi licznikami promocji. Krótki ON/OFF pozostaje dwiema próbkami; własne komendy nie stają się dowodem treningowym.

Warunki promocji są sprawdzane po każdej obserwacji, również gdy upływa cooldown bez nowej próbki. Kontrola sumy modelu, cechy kontekstu, trajektoria i zasady sterowania pozostają bez zmian. Nie zmieniamy formatu bazy, ustawień WAL ani częstotliwości sprawdzania modeli.

Statystyki dostępności pozostają aktualne w RAM i korzystają z istniejącego zapisu co 60 sekund. Nowe próbki i okna nie czekają na ten okres. Nieoczekiwany restart może cofnąć same okresowe liczniki dostępności do ostatniego snapshotu; powolna baza nadal może opóźnić zapis zakolejkowanych danych. Background writer, ponawianie błędów i bariera zamykania nadal obsługują pełne zakolejkowane snapshoty.

## Wyniki i granice pomiaru

W dołączonym logu 0.14.169 nie było błędów blokady bazy. Shadow observation zajmowała średnio około 225 ms, CPU ostatnio około 60%, a checkpointy WAL średnio około 1,1 s. To koszty różnych wątków, których nie należy sumować jako opóźnienia jednej decyzji.

Paired benchmark czterech dużych modeli, 64 obserwacji i ośmiu niezależnych próbek zachował stan każdego modelu i wynik każdej obserwacji oraz identyczny końcowy JSON na dysku. Liczba snapshotów spadła z 264 do 44, zapisanych wierszy z 52 do 36. Cały mierzony fragment snapshot/JSON/SQLite był 1,66–2,47 razy szybszy w trzech powtórzeniach na Linuxie. To syntetyczny benchmark komponentu; poprawę opóźnienia decyzji w rzeczywistym domu trzeba sprawdzić następnym logiem.

Pozostają koszty pełnej walidacji Candidate, obliczania cech i I/O checkpointów. Nowy log zawiera liczniki `context_promotion_snapshot_changed` i `context_promotion_snapshot_skipped`, aby sprawdzić, czy zbędne kopie faktycznie zniknęły.

Dodano dziewięć regresji obejmujących skomponowane metryki, zapis próbek ON/OFF, własne komendy, puste okna, zmianę konfiguracji, ciągłe wartości i promocję po cooldown. Pełny zestaw obejmuje 2031 testów. CI wykonuje również paired benchmark z kontrolą wszystkich bieżących stanów i końcowego zapisu.

## Instalacja

Zainstaluj 0.14.170 i uruchom dodatek ponownie. Ta zmiana nie wymaga ponownego treningu ani pełnego rebuild. Po kilkunastu minutach porównaj log runtime z podobnego okresu: CPU, `wrapped_inference`, `context_shadow_snapshot`, `context_shadow_json`, checkpointy WAL oraz nowe liczniki pominiętych snapshotów.
