# Poprawka 0.9.2

Zmiana Shadow → Control mogła kończyć się błędem "Automation scan incomplete: 7 automation config(s) unavailable", nawet gdy niedostępne konfiguracje nie dotyczyły danego urządzenia. Executor traktował globalne ostrzeżenie skanera jako bezwzględną blokadę.

## Poprawka

Niepełny skan jest teraz jawnym ostrzeżeniem w diagnostyce i dzienniku. Control przejmuje znane automatyzacje danego celu: wysyła automation.turn_off ze stop_actions=true i sprawdza stan OFF. Nie wyłącza wszystkich nieznanych automatyzacji domu. Błąd usługi lub brak potwierdzenia OFF nadal zatrzymuje przejęcie.

Skaner zachowuje ostatnie poprawnie odczytane powiązania celów i kontekstu także po błędzie odczytu. Cache jest zapisany w app_meta pod automation_target_cache_v1 i dostępny po restarcie. Udany kolejny odczyt zastępuje poprzednią mapę; usunięta automatyzacja znika z cache, a nowe ID tej samej encji nie dziedziczy starego powiązania.

Brak ID i nieprawidłowa odpowiedź są raportowane dla konkretnej automatyzacji. Automatyzacji, której konfiguracji nigdy nie udało się odczytać, nie można wiarygodnie przypisać do urządzenia — panel pokazuje tę niepewność. Dynamiczne cele i pośrednie skrypty pozostają ograniczeniem dotychczasowego analizatora.

## Aktualizacja

Wersja 0.9.2 zawiera również pełną architekturę 0.9 i poprawki bootstrapu 0.9.1. Aktualizacja zgodnych modeli 0.9 nie wymaga Train ani ponownego bootstrapu z powodu samej poprawki skanowania. Jeśli bootstrap poprzednio się nie zakończył, uruchom go ponownie.

Zachowaj dane i aktualizuj tę samą aplikację. Przy lokalnych źródłach podmień cały katalog adaptive_ai i wykonaj Rebuild. Po publikacji w dotychczasowym repozytorium instalację repozytoryjną aktualizuj przez sklep aplikacji HA.

## Testy

107 testów przeszło. Nowe przypadki obejmują HTTP PATCH zmieniający Shadow na Control przy siedmiu błędach odczytu, wyłączenie znanej automatyzacji mimo niepełnego skanu, blokadę przy braku OFF, pamięć powiązań po restarcie, usunięcie/zmianę celu i ID automatyzacji oraz brak ID lub błędny format odpowiedzi.

[Pełny wynik](TEST_RESULTS_0_9_2.txt), [symulator](SIMULATOR_0_9_2.json), [mikrobenchmark](BENCHMARK_LOCAL_0_9_2.json).

Testy używają kontrolowanych odpowiedzi HA; nie wykonywano przejęcia prawdziwych urządzeń podczas przygotowania tej wersji. Wynik testów CI/budowy obrazu należy sprawdzić osobno na GitHubie. Cele wydajności Raspberry Pi 4 nadal wymagają fizycznego pomiaru.
