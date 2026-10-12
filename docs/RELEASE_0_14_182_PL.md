# Adaptive AI 0.14.182

Poprawione jest zarządzanie WAL podczas importu Recorder oraz ponawianie odroczonych zapisów po chwilowym konflikcie SQLite. Raport z 0.14.181 pokazywał aktywny import historii, bez blokady pojemności, ale także wielosekundowe skracanie WAL przy około 64 MiB mimo budżetu około 565 MiB.

Próg zwykłego skracania WAL wynosi teraz 25% budżetu WAL wyliczonego dla instalacji lub ustawionego w trybie ręcznym. Zwykłe próby dzieli przynajmniej minuta. Szybkie odzyskiwanie, z odstępem przynajmniej 5 sekund, włącza się dopiero przy 40% budżetu, przed istniejącą pauzą zapisów przy 50%. Nie ma wspólnego dla wszystkich instalacji progu 64 MiB. Zwykłe checkpointy PASSIVE nadal działają w tle; aktywni czytelnicy i niezakończony checkpoint nadal uniemożliwiają skracanie.

Odroczony zapis po `SQLITE_BUSY` lub `SQLITE_LOCKED` zachowuje niezatwierdzoną partię i ponawia ją po przerwie. Nie tworzy przy tym ostrzeżeń `runtime_housekeeping_error` ani `provenance_batch_flush_failed`. Eksport debug pokazuje liczbę takich ponowień w `storage_retry.busy_retries`, aktualną przyczynę i czas do następnej próby. Pozostałe błędy, w tym I/O i uszkodzenie bazy, nadal są zgłaszane. Jawne żądanie trwałego zapisu nadal otrzymuje błąd SQLite, jeśli nie może zatwierdzić danych.

Budżet procentowy z 0.14.181, ochrona przestrzeni i limity SQLite pozostają aktywne. Zachowane są wszystkie wiersze historii, próbkowanie sensorów, niezależne transakcje po najwyżej 128 wierszy, zapisywanie ukończonych fragmentów Recorder oraz modele i preferencje użytkownika.

12 nowych regresji sprawdza różne pojemności, wartości z raportu użytkownika, odstępy prób i progi odzyskiwania, rzeczywisty konflikt dwóch połączeń SQLite, zachowanie kolejki, późniejszy commit, brak ostrzeżeń podczas ponawiania oraz widoczność błędów I/O. Testy nie potwierdzają czasu pełnego treningu na karcie SD. Raport użytkownika obejmuje ponad 6 milionów rekordów i przygotowanie historii przed rozpoczęciem uczenia modelu; ta poprawka usuwa zbędne operacje WAL i poprawia obsługę konfliktów, nie obiecuje konkretnego czasu całej pracy.

Po aktualizacji uruchom ponownie dodatek i wznów trening. Nie trzeba usuwać bazy ani wykonywać Full Rebuild. Ukończone fragmenty Recorder pozostają zapisane.
