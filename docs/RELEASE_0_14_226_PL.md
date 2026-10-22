# HomeMind 0.14.226

Użytkownik zgłasza powtarzający się brak wykresu Correct, tytuł Gen ... i timeout odczytu 12000 ms. Nie otrzymaliśmy eksportu stosu z urządzenia. Na zamrożonym kodzie .225 odtworzono dwie ścieżki pasujące do objawu: otwarcie okna oczekuje na pełny workflow status, a odczyt historii korzysta z resolvera synchronizującego metadane Live pod Store.lock i deserializującego model.

## Poprawka

Correct history oraz inspektor punktu korzystają z osobnego resolvera wyłącznie do odczytu. Potrzebują identyfikatora generacji, konfiguracji i rzeczywistych danych historycznych. Nie aktualizują lineage, nie tworzą root rows i nie wczytują ani nie hashują wag modelu. Gdy generacja Live nie ma jeszcze wiersza root, odczyt tworzy jedynie opis w odpowiedzi, bez zapisu. Zachowane dowody historyczne można obejrzeć także przy niedostępnym modelu. Brak Desired pozostaje luką; nie odtwarzamy go przez replay aktualnej policy.

Okno pobiera historię bez wcześniejszego GET workflow status. Odpowiedź wykresu zawiera tytuł, numer generacji i konfigurację. Liczba nowych punktów Correct pochodzi z istniejącej inwentaryzacji tylko do odczytu, więc użyte wcześniej punkty nie stają się ponownie nowymi. Zapis punktu, Undo, tworzenie Candidate i trening zachowują dotychczasowy resolver i walidację modelu.

Powtarzane odczyty tego samego okna i zakresu współdzielą jedno żądanie. Błąd nie usuwa poprzedniego wykresu; ponowne połączenie odczytuje dane. Opóźniona odpowiedź po zamknięciu lub przełączeniu generacji nie nadpisuje nowego widoku. Limit 12 sekund nie został wydłużony.

## Walidacja

Końcowa walidacja w toku. 26 testów celowanych OK. Test przez rzeczywisty końcowy serwer HTTP trzyma Store.lock i transakcję BEGIN IMMEDIATE SQLite, odczytując Live, Candidate G1/G2 i punkt bez deserializacji modelu. Harness rzeczywistych funkcji JS sprawdza wygenerowanie SVG bez workflow status, retry bez duplikacji, zachowanie wykresu po błędzie i odrzucenie starej odpowiedzi. Testy obejmują także brak tworzenia root row, luki Desired, poprawnego bezpośredniego rodzica i zachowaną walidację modelu dla operacji zmieniających dane.

Odtworzenie na hoście potwierdza podatne ścieżki kodu, a nie dokładny stos przyczyny na Pi. Czas odpowiedzi z testu hosta nie jest pomiarem szybkości na Raspberry Pi ani przez HA ingress.

## Instalacja

Podmienić źródła tego samego dodatku, zachowując identyfikator, konfigurację, bazę, modele i kursory. Przebudować obraz dodatku, uruchomić i wykonać Ctrl+F5. Sprawdzić wersję .226, otworzyć Correct i ewentualnie użyć Połącz ponownie. Rebuild model nie jest potrzebny. Jeśli timeout pozostanie, świeży eksport z próby pozwoli rozdzielić etap resolve_generation, labels, live_observed_history lub current_history; pomiary etapów są dostępne w debug trace.
