# 0.10.1 — poprawka startu panelu

W 0.10.0 usunięto funkcję `toggleExplore`, ale pozostało przypisanie `window.toggleExplore=toggleExplore`. Przeglądarka zgłaszała `ReferenceError` przed podpięciem przycisków, pierwszym `load()` i timerem odświeżania. Efektem był pusty panel z napisem Starting, niezależnie od czasu oczekiwania. Test składni JS tego nie wykrywał; wcześniejszy podgląd korzystał z części starszych zasobów przeglądarki.

Usunięto nieaktualne przypisanie. Wszystkie skrypty mają teraz wersjonowane adresy `?v=0.10.1`, aby aktualizacja nie korzystała z mieszanki poprzednich plików. Nie zmieniono uczenia, sterowania ani migracji danych.

Regresję odtworzono przez wykonanie opublikowanego `app.js` z 0.10.0 w Node VM. Poprawiony skrypt przechodzi ten sam test: wykonuje pierwsze zapytanie `api/status`, rejestruje odświeżanie i podłącza przyciski. Łącznie 153 testy przeszły. Na świeżych adresach zasobów w przeglądarce sprawdzono pojawienie się karty agenta, brak błędów konsoli i otwarcie eksperymentów. Podgląd był lokalny, bez sterowania fizycznym HA.

Instalacja: podmień kompletny katalog `adaptive_ai` w tej samej lokalnej aplikacji, wykonaj Rebuild aplikacji, następnie otwórz panel ponownie. Modele i dane pozostają zgodne; trening agentów nie jest potrzebny. Szczegóły: [INSTALLATION_PL.md](INSTALLATION_PL.md).

Log testów: [TEST_RESULTS_0_10_1.txt](TEST_RESULTS_0_10_1.txt). Lokalny Docker daemon jest niedostępny; nie deklarujemy lokalnego builda obrazu ani wdrożenia na HA użytkownika.
