# HomeMind 0.14.222

Poprawka po zgłoszeniu zawieszonego okna Rebuild w 0.14.221.

## Przyczyna w kodzie

Końcowy entrypoint instaluje startup_train_guard, który nadpisywał enqueue i wywoływał _try_start_head bezpośrednio w wątku HTTP. HistoryManager przed utworzeniem wątku treningu wykonywał dobór schematu sensorów dla Rebuild. To mogło zatrzymać odpowiedź HTTP na kosztownym przygotowaniu. Poprzednia poprawka UI dodatkowo blokowała Cancel i Escape oraz nie ograniczała oczekiwania na odpowiedź.

Nowa wersja usuwa synchroniczną próbę startu w HTTP. Kolejka jest budzona przez istniejący Condition.notify_all. Dobór sensorów wykonuje zarejestrowana sesja treningowa, z widocznym etapem przygotowania i sprawdzeniem anulowania przed rozpoczęciem replay/reset. Odczyty konfiguracji kolejki nie liczą już statystyk całej historii. Trwały zapis stanu oczekiwania i zwolnienie Control nadal poprzedzają przyjęcie żądania.

## Zachowanie okna

Przed wysłaniem Cancel zamyka okno bez żądania. Po wysłaniu Close i Escape zamykają tylko okno; nie oznaczają zatrzymania treningu na serwerze. Po 30 sekundach bez potwierdzenia panel pokazuje wynik nieznany i nie ponawia operacji. Ponowne otwarcie okna podczas oczekiwania nie wysyła drugiego Rebuild. Przy wyniku nieznanym należy sprawdzić kolejkę przed ponownym żądaniem po odświeżeniu strony.

Do zachowania częściowego modelu i kursora służy Resume training. Rebuild wymaga potwierdzenia i nadal oznacza ponowne uczenie modelu. Zmiana nie wyłącza kontroli dysku, WAL, wolnego miejsca ani reguł Candidate/Control.

## Walidacja

Końcowa walidacja w toku. Nowe testy uruchamiają rzeczywisty punkt startowy trial_queue_main i celowo zatrzymują przygotowanie treningu; nie opierają się wyłącznie na kolejce bez wrapperów. Test HistoryManager sprawdza rejestrację sesji, anulowanie podczas doboru sensorów i zachowanie modelu, kursora oraz benchmarku. Test UI używa modelu DOM, więc zachowanie przeglądarki Home Assistant po instalacji pozostaje do potwierdzenia. Testy hosta nie stanowią pomiaru wydajności Raspberry Pi.

## Instalacja

Paczka repository zawiera katalog adaptive_ai. Dla lokalnego dodatku Home Assistant rozpakować repozytorium do /addons/homemind (plik konfiguracyjny w /addons/homemind/adaptive_ai/config.yaml), odświeżyć sklep lokalnych dodatków i przebudować/zaktualizować istniejący dodatek. Paczka addon-root zawiera bezpośrednio zawartość katalogu adaptive_ai i służy do instalacji w katalogu konkretnego lokalnego dodatku. Po uruchomieniu zweryfikować wersję 0.14.222 w panelu. Dotychczasowe dane dodatku muszą pozostać zachowane.
