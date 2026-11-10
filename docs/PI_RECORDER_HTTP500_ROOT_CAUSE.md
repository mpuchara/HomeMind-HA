# HomeMind 0.14.235 — status przyczyny HTTP 500 (Pi 5)

## Ustalenia

Na fizycznym Pi 5 HomeMind 0.14.234 poprawnie wystartował, pobrał rejestr encji (1350 wpisów) i 47 automatyzacji. Żądania historii HA/Recorder zwracały HTTP 500 o 00:05:55 i 00:07:55 (10.11.2026). `resource_guard.recorder_last_failure` rozpoznaje klasę `ha_server_or_rate_limit`; **nie identyfikuje wyjątku wewnątrz HA Core**.

Kod `HAClient.history()` wywołuje udokumentowane REST `/api/history/period/<timestamp>` z `filter_entity_id`, `end_time`, `minimal_response`, `no_attributes` i `significant_changes_only=0` przy pełnej historii. Na Pi jedna paczka najnowszej historii może obejmować do 8 encji × 3 godziny, starsza uproszczona do 32 × 24 godziny. Resilient fetch rozdziela obciążenie po błędzie; limit odpowiedzi wynosi 8 MiB. Są to prawidłowe parametry według dokumentacji HA, ale rzeczywisty koszt SQL zależy od bazy i encji.

**Przyczyna HTTP 500 nadal nieustalona.** Przyczyną może być przeciążenie, błąd Recorder, encja lub historia danych. Nie modyfikuj bazy HA ani nie usuwaj danych.

## Nowa bezpieczna diagnostyka

W `/api/status` → `resource_guard.recorder_last_failure` widać kategorię, `http_status`, liczbę encji i długość okna (`request_shape`), a także flagi `minimal`, `no_attributes`, `significant`. Nie logujemy identyfikatorów encji, pełnych dat/URL, tokenów ani danych stanów. Trzy kolejne HTTP 5xx/429 w jednym odkrywaniu przerywają *ten* przebieg, zachowując dane, a automat może ponowić bez kasowania postępu. Zabezpieczenie 120 s pozostało aktywne.

## Dane wymagane do potwierdzenia przyczyny

1. W HA: **Ustawienia → System → Dzienniki**, filtr Home Assistant Core, znacznik czasu odpowiadający odpowiedzi 500; potrzebny pełny traceback z `homeassistant.components.history`, `recorder` lub sterownika SQL, po usunięciu sekretów.
2. W HomeMind: diagnostyka `resource_guard.recorder_last_failure`, status discovery i `history_slice_skipped`. Nie publikuj tokenów ani encji prywatnych.
3. W izolowanym HA: czy historia jednej encji w krótkim zakresie (np. 30 minut) działa oraz czy problem pojawia się dopiero w większym oknie. Nie wysyłaj serii równoległych żądań.

Bez tego **nie wolno raportować HTTP 500 jako naprawionego**. Zmiany 0.14.235 zapobiegają nieprawidłowemu oznaczaniu niepełnego skanowania i umożliwiają bezpieczne wznowienie.
