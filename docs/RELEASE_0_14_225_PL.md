# HomeMind 0.14.225

Użytkownik zgłasza brak punktów wykresu po zamknięciu panelu oraz około 10 s między działaniem urządzenia a stanem widocznym w aplikacji. Nie otrzymaliśmy świeżego eksportu z tej próby. Poniższe ścieżki zostały odtworzone na kodzie .224; nie dowodzą dokładnego źródła opóźnienia na urządzeniu.

## Zbieranie wykresu bez panelu

Engine uruchamia jeden daemon próbkowania co 5 s, kończony jego stop_event. Trzy serie przechowują maksymalnie po 121 punktów, do 10 minut historii p95 z ostatnich 60 s. Nie ma zapisów na kartę ani nowych odczytów HTTP. Okna bez próbek pozostają lukami. Wykres istnieje w RAM również po zamknięciu strony; restart dodatku rozpoczyna go od nowa. Status zachowuje dotychczasowy format trend i kompatybilne próbkowanie dla samodzielnych narzędzi.

## Dostarczenie Current i historia

Odczyt /api/live nie odświeża już indeksu agentów, kontekstu ani routingu. Te czynności mogą wykonywać SQLite i pozostają własnością harmonogramu runtime. Endpoint kopiuje opublikowane konfiguracje i stan z RAM. Dodane znaczniki rozdzielają HA last_updated, czas odebrania stanu oraz czas migawki Current. Metryka ha_state_to_ws_receive pokazuje opóźnienie od znacznika HA do lokalnego odbioru WebSocket; nie jest pomiarem od fizycznego przełączenia urządzenia. Zdarzenia z czasem HA wyprzedzającym odbiornik są liczone oddzielnie.

Buffer Teaching oraz archiwum zachowują widoczną referencję do partii w trakcie zapisu aż do zakończenia transakcji. Czytniki historii łączą dane zatwierdzone, oczekujące i zapisywane, z deduplikacją po czasie. Przy rollback wiersze wracają do bufora. Nie generujemy wstecz Desired z aktualnej policy ani nie przesuwamy czasu decyzji do czasu zmiany urządzenia.

Panel rozstrzyga świeżość Current i Desired niezależnie. Opóźniona odpowiedź nie może nadpisać nowszej migawki Current ani nowszej zakończonej decyzji otrzymanej drugą ścieżką.

## Tryb bez WebSocket

Wcześniejszy domyślny interwał fallback 10 s przechodzi na 2 s, a minimalny próg harmonogramu dla fallback wynosi 1 s. Inne jawne interwały pozostają zachowane. Harmonogram nadal dopuszcza tylko jeden odczyt /states naraz i zachowuje retry/backoff. Zdrowy WebSocket utrzymuje dotychczasową rzadką resynchronizację 900 s; zmiana nie zwiększa jego ruchu REST. Częstszy fallback zwiększa liczbę odczytów wyłącznie podczas braku realtime.

REST publikuje powiadomienie Current zaraz po zaakceptowaniu migawki, przed archiwizacją. Nie wykonuje już flush archiwum w jedynym workerze odczytu stanów: istniejący housekeeping zapisuje kolejkę, więc wolny commit nie blokuje następnego poll. Prędkość raportowania przez integrację HA i sieć urządzenia pozostaje poza tym harmonogramem.

## Walidacja i instalacja

Końcowa walidacja w toku. 53 testy celowane OK, w tym 9 nowych regresji. Cztery testy ujawniają odpowiadające problemy na zamrożonym kodzie .224 i przechodzą po zmianie. Testy obejmują zamknięty panel, limit i zakończenie samplera, rzeczywistą SQLite podczas zapisu/rollback, brak odświeżania indeksu przez Live, migrację fallback, pojedynczy poll, powiadomienie przed archiwizacją i opóźnione odpowiedzi UI.

To walidacja funkcjonalna na hoście. Nie stanowi pomiaru sprzętowej reakcji Pi ani potwierdzenia przyczyny zgłoszonych 10 s bez nowego eksportu. Weryfikacja na Pi pozostaje otwarta.

Instalacja: zachować identyfikator, konfigurację, bazę i modele dotychczasowego dodatku; podmienić jego źródła, przebudować obraz, uruchomić i odświeżyć panel Ctrl+F5. Sprawdzić wersję .225. Rebuild model nie jest potrzebny. Zamknąć panel na kilka minut i ponownie otworzyć, aby sprawdzić historię wykresu. Przy próbie urządzenia sprawdzić również status realtime/REST fallback oraz świeży eksport metryk i znaczników czasu.
