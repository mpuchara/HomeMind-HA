# HomeMind Adaptive AI 0.14.178

Poprawka powtarzających się pauz WAL i komunikatów `runtime_housekeeping_error` w wersji 0.14.177. Eksport diagnostyczny pokazał bazę około 350 MiB, około 22 GiB wolnego miejsca, WAL tuż powyżej progu 64 MiB oraz 24 udane odzyskania WAL bez błędów. Trening nadal importował historię, ale jego zapis wielokrotnie czekał na upływ 60-sekundowego odstępu między odzyskaniami WAL. Housekeeping błędnie opisywał oczekiwaną odmowę zapisu jako awarię co kilka sekund.

- Podczas blokady WAL worker może ponawiać bezpieczne odzyskiwanie co 5 sekund. Zwykłe odzyskiwanie poniżej progu blokady zachowuje odstęp 60 sekund. Nadal wymagany jest kompletny checkpoint PASSIVE; TRUNCATE ma busy timeout 0 i nie omija aktywnych czytelników ani writerów. Wolny dysk może wydłużyć samą operację I/O, więc 5 sekund jest odstępem prób, nie gwarancją czasu odzyskania.
- Udane odzyskanie odświeża budżet w RAM, dzięki czemu odbiór nowych danych nie pozostaje zablokowany na podstawie poprzedniego rozmiaru WAL.
- Housekeeping traktuje `StoragePressure` jako oczekiwanie, zachowuje już zgromadzone wiersze i ponawia próbę po 5 sekundach. Nowe wybudzenia nie skracają tej przerwy. Inne błędy nadal są widoczne, z komunikatem najwyżej raz na minutę.
- Nowe surowe wpisy lokalnego archiwum nie powiększają kolejki RAM podczas pauzy. Odbiór zdarzeń i aktualizacja bieżącego kontekstu pozostają aktywne. Pominięcia są liczone, a brakujące dane historyczne mogą zostać uzupełnione z Home Assistant Recorder. Eksport diagnostyczny pokazuje pauzę housekeeping i liczbę oczekujących wierszy.

Limity pozostają bez zmian: domyślnie baza 2 GiB, WAL 128 MiB, rezerwa wolnego miejsca 2 GiB i surowa historia maksymalnie 10 dni. Zapis nie omija ochrony pojemności. Trening zachowuje ponawianie tej samej wycofanej partii danych z wersji 0.14.177.

Walidacja: 7 nowych testów odtwarza wielokrotne osiąganie progu WAL, aktywnego czytelnika, niepełny checkpoint, pauzę housekeeping, odzyskanie oczekującego zapisu, ograniczenie wzrostu RAM i diagnostykę. Wydanie podlega pełnym testom Python 3.11 i 3.13, benchmarkom, budowie obrazu oraz walidacji paczek instalacyjnych.

Instalacja: odśwież repozytorium dodatku i zaktualizuj do 0.14.178. Aktualizacja restartuje dodatek. Jeśli wcześniejsze zadanie treningu zostało wstrzymane, wznów je po aktualizacji; zachowany postęp nie wymaga ponownego kasowania uczenia przez Full Rebuild.
