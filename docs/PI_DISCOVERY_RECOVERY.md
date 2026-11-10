# Recorder discovery — bezpieczne odzyskiwanie 0.14.235

## Kontrakty trwałości

- `entity_history` pozostaje kanonicznym archiwum historii. Zapisane rekordy nie są usuwane, a ponowne pobranie jest bezpieczne dzięki istniejącej unikalności rekordów.
- `discovery_recorder_coverage_v1` przechowuje ograniczone sumy przedziałów `[start,end]` na każdą encję i źródło danych. Wpis powstaje wyłącznie po pomyślnym pobraniu i zapisaniu całego fragmentu. To podpowiedź wydajnościowa — uszkodzenie lub utrata metadanych powoduje ponowne pobranie, nigdy fałszywy sukces.
- Przy wznowieniu system odejmuje ukończone przedziały od planowanych, również gdy nowy `end_ts` się przesunął. Luki muszą zostać pobrane. Nie wolno zastępować tego jednym maksymalnym kursorem.
- HTTP 500, timeout, odpowiedź >8 MiB lub pominięty fragment zostawiają skan jako **niekompletny**, o ile retry/podział nie zakończą go w całości. Anulowanie także nie zapisuje znaczników finalnych.
- `discovery_deep_history_complete` zapisuje się dopiero po pełnym starszym *oraz* najnowszym zakresie. `initial_discovery_complete` i `manual_discovery_refresh` dopiero po klasyfikacji poprawnie pobranej historii.
- Wcześniej istniejące znaczniki i agenci pozostają nienaruszeni. Program nie może automatycznie ocenić wiarygodności znacznika zapisanego przez starszą wersję po historycznym częściowym błędzie; rozstrzygnięcie wymaga jawnego Rescan na kopii danych.

## Ograniczenia

Maksymalnie 4096 wpisów przedziałów, jeden worker Recorder, oryginalny 120-sekundowy backoff, oryginalny limit RSS/response. Przy przepełnieniu metadanych cache jest przycinany (może wzrosnąć ilość pobrań). Nie ma automatycznej modyfikacji danych modeli ani sterowania urządzeniami. Weryfikacja live Pi i traceback HA pozostają otwarte.

## Testy

`python -m unittest tests.test_release_235_recorder_discovery -v` w środowisku repo; dodatkowo pełne CI Python 3.11/3.13, Docker amd64 i ARM64. W testach symulujemy częściowy sukces i restart, luki między przedziałami, przerwanie, HTTP 500, timeout, prawidłowy koniec i brak wycieku sekretów.
