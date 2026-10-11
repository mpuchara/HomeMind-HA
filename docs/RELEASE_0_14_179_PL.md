# Adaptive AI 0.14.179

Panel treningu pokazuje teraz rzeczywisty etap aktywnej sesji, również podczas przygotowywania historii Recorder. W 0.14.178 import pracował, lecz panel mógł nadal wyświetlać komunikat ze startu aplikacji, „Preparing training history”, 0% i nieaktualne ETA około jednej sekundy.

## Zmiany

- Status pobiera komunikat, liczniki, czas rozpoczęcia i ETA z aktywnej sesji treningu. Przy dwóch sesjach każda zachowuje własne dane, a ogólny procent pozostaje średnią obu sesji.
- Przygotowanie historii zajmuje pierwsze 10% nowego przebiegu; replay, walidacja i finalizacja zajmują pozostałe 90%. Jest to podział etapów interfejsu, nie prognoza czasu. Resume uwzględnia zachowany kursor, bez cofania postępu na granicy przygotowania i replay.
- Licznik importu aktualizuje się przed żądaniem Recorder, po ukończeniu porcji oraz maksymalnie raz na sekundę po zatwierdzonych krótkich transakcjach dużej odpowiedzi. Pokazuje wiersze odpowiedzi i liczbę zapisanych wierszy archiwum.
- Pełne ETA nie jest wyliczane z samego importu; pojawia się po rozpoczęciu mierzalnego replay. Zerowe lub nieznane ETA etapu nie jest wyświetlane jako jedna sekunda.
- Eksport diagnostyki zawiera status historii i aktywnych sesji także po wyłączeniu szczegółowego śledzenia. Odczyt korzysta z RAM i nie skanuje bazy.
- „Slice target” zastępuje mylące „max slice”: kooperacyjny cel 35 ms nie ogranicza czasu oczekiwania na Recorder, sieć ani SQLite. Ta wersja nie obiecuje skrócenia tych operacji.

## Zakres i aktualizacja

Log 0.14.178 potwierdził kolejne pomyślne importy dużych porcji historii aż do momentu eksportu. W tym momencie budżet dysku był normalny, baza zajmowała około 1,4 GiB, a wolne miejsce wynosiło około 21 GiB. Nie było dowodu na całkowite zatrzymanie workerów.

Historia, źródła, próbkowanie, kolejność replay, modele, kwalifikacja i trwałe kursory pozostają bez zmian. Limity dysku oraz odzyskiwanie WAL z 0.14.178 są zachowane. Duży pierwszy import może nadal długo trwać.

Po aktualizacji i restarcie wznów przerwany trening. Nie wykonuj resetu ani Full Rebuild tylko po to, aby naprawić panel. Pomyślnie zaimportowane zakresy Recorder są ponownie wykorzystywane.

## Weryfikacja

Siedem nowych testów sprawdza status aktywnej sesji bez zapytań SQL, niezależne liczniki dwóch sesji, postęp zapisanego prefiksu dużej odpowiedzi, anulowanie z zachowaniem zatwierdzonych danych, ciągłość Resume, ponowne użycie pokrycia Recorder i eksport diagnostyki.

Pełna kontrola wydawnicza obejmuje 2145 testów na Pythonie 3.11 i 3.13, benchmarki, składnię JavaScript, obraz dodatku oraz oba formaty pakietów instalacyjnych. Wyniki CI są warunkiem publikacji.
