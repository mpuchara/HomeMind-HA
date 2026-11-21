# Retencja FeatureJournal podczas treningu w 0.15.17 TEST

21 listopada 2026 trzeci trening na instalacji użytkownika przerwał się komunikatem `feature_observation_events; DELETE inside frozen feature window; previous model retained`. Dwa wcześniejsze treningi zakończyły się poprawnie. Komunikat nie zawiera identyfikatora operacji retencji.

Na 0.15.16 testy SQLite odtworzyły ten komunikat zarówno przy przekroczeniu globalnego limitu zdarzeń, jak i przy wyprzedzającym czyszczeniu bazy, gdy zapis wciąż działał normalnie. W obu przypadkach istniały zdarzenia spoza wejścia treningu, które można było usunąć.

W 0.15.17 globalna retencja zaczyna od zdarzeń spoza prawidłowych okien aktualnie nadzorowanych treningów. Dopasowanie obejmuje encję, zamknięty przedział czasu zdarzenia i górną granicę `rowid`. Zdarzenia dopisane po zamrożeniu i wiersze chronione jedynie przez nieaktywne zadanie nadal kwalifikują się do usunięcia. Wyprzedzające czyszczenie również omija aktywne okna, dopóki budżet zapisu jest w stanie normalnym.

Przy braku innych wierszy globalny limit nadal wymusza usunięcie. Awaryjne czyszczenie może usunąć dane po wstrzymaniu zapisu przez budżet. Faktyczna zmiana zamrożonych danych zatrzymuje publikację modelu. Aktualizacja nie wymaga migracji schematu ani zmiany limitów.

Test kolejki uruchamia właściwy runtime HTTP i procesy trenujące na osobnej bazie SQLite z symulowanym HA. Po dwóch treningach restartuje runtime. Tuż przed publikacją trzeciego modelu wymusza przekroczenie limitu o jeden wiersz i sprawdza, że usunięto wpis spoza treningu. Test weryfikuje zapis modelu, stan Shadow, zachowanie dwóch wcześniejszych modeli i brak wywołań sterujących do HA.

Próba na instalacji użytkownika wymaga zainstalowania paczki TEST i ponownego uruchomienia treningu trzeciego agenta. Kryteria to zapis nowego modelu, postęp 100%, stan Shadow i zachowanie dwóch wcześniejszych modeli.
