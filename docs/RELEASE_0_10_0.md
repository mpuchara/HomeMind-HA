# Wydanie 0.10.0

Baza: świeżo pobrany `main`, commit `2b544ab` (Merge realtime light timing fixes). Zachowano zmiany wykonane poza wcześniejszym wątkiem: `fast_queue_main`, FIFO, kwalifikację Control, przegląd generycznych urządzeń oraz transakcyjny dziennik przejmowania automatyzacji.

Dodano osobne dla każdego agenta eksperymenty obecności, otoczenia i pracy innych urządzeń. Wdrożenie obejmuje menu, API, trwały kontekstowy bandyta, porównania z decyzją bazową, opóźnione wyniki, budżety i obsługę przerwanych obserwacji. Model historyczny zachowuje wersję 10 i schemat 11. Eksperymenty są początkowo wyłączone, również jeśli stara flaga `micro_exploration` była ustawiona.

`Executor` pozostaje jedynym miejscem wywołania usług HA. Próby przechodzą przez kwalifikację, przegląd urządzenia, wsparcie historyczne, limit nowości, własność celu, kontrolę świeżości stanu, ACK, stabilizację, limity nastaw i ręczne przejęcie. Token przygotowanej próby jest sprawdzany ponownie przed wysłaniem. Budżet rezerwowany jest przed usługą, aby niepewny błąd transportu nie otwierał nieograniczonych ponowień. Wyłączenie/rebuild/review Control kończy obserwację bez wymyślania nagrody.

## Weryfikacja

- 152 testy unittest, w tym 25 nowych testów eksperymentów.
- Pełna ścieżka Engine → Executor → mock HA: wcześniejsze ON, ACK bez nagrody, brak wielokrotnych poleceń, korekta ręczna −1, eksperymentalny cooldown mimo szybkiego profilu bez manual hold.
- Wybór grupy kontekstu, przesunięcie granicy jasności, wpływ innych urządzeń na kolejne wybory, porównanie bazowe, ograniczenia fizyczne, brak sygnału, odrzucenie fałszywego tokenu, restart/budżet, awaria usługi, pojedyncza równoczesna próba, długa obserwacja termostatu mimo zwykłego age decay modelu.
- Rzeczywista przeglądarka, lokalny fixture bez połączenia HA: otwarcie menu, wybór urządzeń, włączenie, zapis, ponowne otwarcie z zachowanymi ustawieniami i kontrola wyglądu.
- Składnia wszystkich sześciu plików JavaScript, kompilacja Python, istniejący symulator i mikrobenchmark oraz kontrola integralności ZIP.

Pełny log: [TEST_RESULTS_0_10_0.txt](TEST_RESULTS_0_10_0.txt). Nie wykonano prób na fizycznych urządzeniach użytkownika ani pomiarów Raspberry Pi 4. To wydanie źródłowe; lokalne testy nie potwierdzają optymalności polityki w konkretnym domu. Szczegóły i ograniczenia: [EXPERIMENTS_PL.md](EXPERIMENTS_PL.md).

Lokalny build Docker nie został wykonany: daemon Docker Desktop nie był dostępny. Repozytorium zachowuje workflow budowania obrazu w GitHub Actions.
