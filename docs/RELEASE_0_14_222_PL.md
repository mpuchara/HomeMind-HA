# HomeMind 0.14.222

Poprawka po zgłoszeniu zawieszonego okna Rebuild w 0.14.221.

## Przyczyna w kodzie

Końcowy entrypoint instaluje startup_train_guard, który nadpisywał enqueue i wywoływał _try_start_head bezpośrednio w wątku HTTP. HistoryManager przed utworzeniem wątku treningu wykonywał dobór schematu sensorów dla Rebuild. To mogło zatrzymać odpowiedź HTTP na kosztownym przygotowaniu. Poprzednia poprawka UI dodatkowo blokowała Cancel i Escape oraz nie ograniczała oczekiwania na odpowiedź.

Nowa wersja usuwa synchroniczną próbę startu w HTTP. Kolejka jest budzona przez istniejący Condition.notify_all. Dobór sensorów wykonuje zarejestrowana sesja treningowa, z widocznym etapem przygotowania i sprawdzeniem anulowania przed rozpoczęciem replay/reset. Odczyty konfiguracji kolejki nie liczą już statystyk całej historii. Trwały zapis stanu oczekiwania i zwolnienie Control nadal poprzedzają przyjęcie żądania.

## Zachowanie okna

Przed wysłaniem Cancel zamyka okno bez żądania. Po wysłaniu Close i Escape zamykają tylko okno; nie oznaczają zatrzymania treningu na serwerze. Po 30 sekundach bez potwierdzenia panel pokazuje wynik nieznany i nie ponawia operacji. Ponowne otwarcie okna podczas oczekiwania nie wysyła drugiego Rebuild. Przy wyniku nieznanym należy sprawdzić kolejkę przed ponownym żądaniem po odświeżeniu strony.

Do zachowania częściowego modelu i kursora służy Resume training. Rebuild wymaga potwierdzenia i nadal oznacza ponowne uczenie modelu. Zmiana nie wyłącza kontroli dysku, WAL, wolnego miejsca ani reguł Candidate/Control.

## Walidacja

Końcowa walidacja lokalna: **2751 testów OK / 355.393 s**, entrypoint, oba UI, compileall i 21 skryptów JS. Manifest obejmuje 634 plików źródeł/testów/narzędzi. Regresje blokującego przygotowania nie przechodzą na .221 i przechodzą na .222. Nowe testy uruchamiają rzeczywisty punkt startowy trial_queue_main i celowo zatrzymują przygotowanie treningu; nie opierają się wyłącznie na kolejce bez wrapperów. Test HistoryManager sprawdza rejestrację sesji, anulowanie podczas doboru sensorów i zachowanie modelu, kursora oraz benchmarku. Test UI używa modelu DOM, więc zachowanie przeglądarki Home Assistant po instalacji pozostaje do potwierdzenia. Testy hosta nie stanowią pomiaru wydajności Raspberry Pi.

## Instalacja

Z ZIP-a repository podmienić cały katalog adaptive_ai w dotychczasowych źródłach tego samego lokalnego dodatku. Paczka addon-root zawiera bezpośrednio zawartość tego katalogu. Zachować identyfikator, konfigurację i prywatne dane dotychczasowej instalacji. Odświeżyć sklep lokalnych dodatków, wykonać przebudowę obrazu dodatku i uruchomić go. Po odświeżeniu panelu sprawdzić 0.14.222. Przebudowa obrazu dodatku nie jest operacją Rebuild model i nie wymaga kasowania modelu.

Kod wykonawczy: 4d8f8b9c0d37a061babe676e7f73c63e1f9aa2a5. PR: https://github.com/mpuchara/HomeMind-HA/pull/338. CI: https://github.com/mpuchara/HomeMind-HA/actions/runs/39476946194. Raport QA zawiera aktualny stan sprawdzenia CI. Instalacja i faktyczny trening na urządzeniu użytkownika pozostają do potwierdzenia.
