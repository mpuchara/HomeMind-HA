# Architektura HomeMind-HA 0.9.0

## Przepływ

~~~mermaid
flowchart LR
  HA[HA Event] --> C[Context Engine]
  C --> H[Shared Home State]
  H --> P[Micro Policy / PolicyBackend]
  C --> P
  P --> I[Immutable ActionIntent]
  I --> E[Deterministic Executor]
  E --> S[HA Service]
  S --> O[State observation / ACK]
  O --> R[Reward v2]
  R --> P
~~~

## Moduły

| Moduł w adaptive_ai/src | Odpowiedzialność |
| --- | --- |
| main.py | HTTP API, kompozycja i start usług |
| settings.py, storage.py | Konfiguracja, SQLite, migracja i zapis modeli |
| ha.py | Transport HA, wiedza o automatyzacjach |
| context.py, context_engine.py | Adaptery, dobór kontekstu, rejestry HA i mapowanie obszarów |
| home_state.py, home_bootstrap.py, home_sources.py | Wspólny model przejść, ręczny import historii |
| policy_backend.py, policy.py | Kontrakt backendu, diagonalny LinUCB, kalibracja |
| intent.py, executor.py, control.py | Kontrakt intencji, jedyne miejsce wykonania, profile czasu |
| engine.py | Obsługa zdarzeń, planowanie agentów i feedback |
| history.py, replay.py | Ręczne uczenie, strumieniowy replay i wznowienia |
| rewards.py, telemetry.py | Reward v2, ograniczone statystyki wydajności |
| static/home.js, app.js, index.html, style.css | Home Intelligence i diagnostyka agentów |

## Wspólny model domu

Jeden model korzysta z mapowania obszarów HA: entity registry, następnie device registry, następnie jawny fallback użytkownika. Nie zgaduje pokoi z nazw. Przyjmuje ruch, obecność i aktywność, także sensory ESPHome na urządzeniu zawierającym encje sterujące. Pomija pomiary elektryczne W/V/A. Brak dostępnego pomiaru nie oznacza pustego pokoju.

Model zbiera ważone statystyki przejść A→B oraz kontekstu A→B→C, rozkład czasu przyjścia i czasu przebywania. Nieprzyjście w oknie także zasila statystykę. Prognozuje occupancy_now, occupancy_1s/3s/5s, arrival, departure i confidence. Prawdopodobieństwa uwzględniają czas od ostatniego zdarzenia, wygładzanie i zanik starej wiedzy. Occupancy_now jest agregatem dostępnych źródeł, a prognoza uwzględnia przewidywane wyjście.

To lekki model statystyczny bez identyfikowania konkretnych osób. Wartości są przybliżeniami opartymi na sensorach; pewność rośnie z ważonym wsparciem. Nie należy utożsamiać confidence z prawdopodobieństwem obecności ani traktować grafu jako dowodu przyczynowości.

Limity modelu: 128 obszarów, 4096 źródeł, 2048 kontekstów przejść, do 15 nazwanych wyników na kontekst i agregat pozostałych. Histogramy mają stały rozmiar. Zapis zawiera statystyki; restart nie przywraca starej obecności jako aktualnej. Rejestry i bieżące stany odbudowują bieżący kontekst bez uczenia fikcyjnych przyjść.

## Micro policy

PolicyBackend udostępnia predict, update, serialize, deserialize, decay i diagnostics. Diagonalny LinUCB pozostaje małym contextual bandit. Wektor obejmuje jawne cechy wybranych encji oraz siedem cech wspólnej prognozy dla obszaru celu. Polityka wybiera stan docelowy deterministycznie, a Executor obsługuje dynamikę fizycznego urządzenia.

Dla fast binary decyzja o stanie ma krótki horyzont, a większość informacji zwrotnej daje obserwacja preferowanej nastawy. Contextual bandit ogranicza koszt uczenia i ryzyko eksploracji. Nie jest pełnym modelem długoterminowej dynamiki. HVAC może w przyszłości użyć innego PolicyBackend, np. z modelem bezwładności i planowaniem, bez zmiany ActionIntent i Executora. W 0.9 nie dodano ciężkiego modelu HVAC.

Confidence pochodzi z kalibracji na odłożonych próbkach; support oznacza podobieństwo/pokrycie kontekstu, novelty odmienność. Contributors wyliczane są z wkładów cech w predykcję, a nie z wygenerowanej narracji. Podsumowanie zachowania jest oparte na tych danych. Minimalny export zawiera parametry inferencji, schemat i kalibrację; pełny checkpoint zachowuje także statystyki potrzebne do uczenia.

## Granica ActionIntent → Executor

ActionIntent jest niemutowalny: identyfikuje agenta, cel i właściwość, stan docelowy, czas/TTL, schemat, rewizję modelu, zależności kontekstu, rewizję celu i domu, policy head, horyzont prognozy, confidence/support/novelty, contributors i powody decyzji.

Executor ponownie sprawdza aktualne ustawienia, kwalifikację, własność celu, wersje i TTL, dostępność, legalność wartości, ręczny hold, ACK, stabilizację, odstęp akcji, retry i konflikt sterowania. Sprawdza aktualność również po czasochłonnym przejęciu automatyzacji. Każdy cel ma osobną blokadę, więc inne urządzenia mogą działać równolegle. Zdarzenie przychodzące podczas trwającego polecenia wywołuje ponowną ocenę po jego zakończeniu.

Jedyny bezpośredni call HA.service znajduje się w Executor._service. Transport jest zdefiniowany w ha.py. Także Verify i automation.turn_off przechodzą przez Executor. Polityka nie wysyła usług. Shadow kończy się oceną bez żadnych wywołań usług HA. Verify jest dostępnym w Control testem wykonania, podlega kwalifikacji, własności, ręcznemu hold i pending ACK.

Control wyłącza rozpoznane automatyzacje celu (stop_actions=true) i weryfikuje ich stan. Automatyzacje nie są automatycznie przywracane przy opuszczeniu Control. Analiza obejmuje jawne cele entity/device/area; dynamiczne szablony, pośrednie skrypty i zewnętrzni kontrolerzy mogą pozostać nierozpoznani.

Kontekst własnego polecenia jest zapisywany przed HTTP. Echo/ACK tego polecenia nie tworzy ręcznej korekty. Jawna ingerencja użytkownika ma pierwszeństwo i daje sygnał uczenia. Przyjęcie usługi nie jest równoznaczne z fizycznym wykonaniem.

## Wygaszanie wiedzy

Dla upływu dt i half-life H stosowany jest współczynnik f = 2^(-dt/H). Domyślnie H=30 dni dla polityki i 45 dni dla modelu domu. Aktualizacje są leniwe, wykonywane zbiorczo; polityka nie przebiega wszystkich parametrów przy każdym zdarzeniu.

W LinUCB wygaszane są A-1 oraz b, z zachowaniem prioru ridge A=1. Zanikają też ważone liczebności, momenty kontekstu, statystyki nagród i kalibracja. Graf wygasza liczniki/histogramy. Stare próbki replay otrzymują wagę według wieku. Parametry i czas ostatniego decay są serializowane, więc restart nie odmładza modelu.

## Reward v2

| Składnik | Wartość |
| --- | --- |
| Ręczna korekta | -1 |
| Akceptacja po oknie bez korekty | +0.15 |
| Obserwowane przyjście w horyzoncie po akcji wyprzedzającej | +0.45 |
| Użyteczne wyprzedzenie | +0.2 × min(1, opóźnienie_przyjścia / horyzont) |
| Brak przyjścia po pełnej obserwacji przy działających sensorach | -0.6 |
| Przyjście dopiero po horyzoncie | -0.35 |
| Chatter | -0.2 |

Suma jest ograniczana do [-1,1], z jawnym składnikiem clipping, więc UI pokazuje składniki sumujące się do wyniku. Ręczna korekta wyklucza premię akceptacji i antycypacji. Sam ACK nie daje nagrody. Wyparta nowszą decyzją akcja wyprzedzająca zachowuje ograniczony rekord do rozstrzygnięcia opóźnionego wyniku.

## Historia, pamięć i migracja

Bootstrap domu i Train/Resume/Rebuild współdzielą jedną blokadę ciężkiego zadania. Import jest ręczny, porcjowany po czasie i encjach, z progressem, prędkością, ETA i anulowaniem bootstrapu. Restart nie uruchamia replay. Odpowiedź Recorder większa niż 8 MiB powoduje podział zapytania. Kontrola RSS przy ciężkich zadaniach używa limitu 500 MiB na Linuksie.

SQLite przechowuje archiwum i tymczasowe dane. Replay czyta uporządkowane wiersze strumieniowo; temporal lookup pobiera ograniczoną liczbę rekordów. Odłożone próbki kalibracji trafiają do pliku tymczasowego. Szybkie wyszukiwanie kontekstu ma ograniczenie na encję; nie materializuje całego archiwum. Starsze zbiorcze API archiwum odmawia pobrania ponad 20000 wierszy.

Historyczne cechy domu korzystają z checkpointu sprzed okna oraz krótkiego replay sensorów bez uczenia grafu z przyszłości. Bootstrap buduje statystyki poza modelem live, a przy instalacji zachowuje bieżące sensory. Od 0.9.1 nową wiedzę live agreguje w ograniczonym modelu statystyk zamiast buforować wszystkie zdarzenia; model i checkpointy zatwierdzane są razem.

Migracja jest addytywna. Istnieją agenci, entity_history, rl_feedback i ustawienia. Niekompatybilne rl_models są kopiowane do model_backups, oznaczane needs_retrain i pauzowane. Wersje: model polityki 10, feature schema 11, home 1, reward 2. Nowe metadane/checkpointy trafiają do SQLite. _history_watermark spina checkpoint modelu z derived experiences; po błędzie niezatwierdzone próbki są odrzucane przed wznowieniem. Surowe dane i feedback nie są usuwane przy Rebuild.

## Integracja HA i ograniczenia

[WebSocket API HA](https://developers.home-assistant.io/docs/api/websocket/) dostarcza zdarzenia i kontekst. Rejestry encji/urządzeń/obszarów są pobierane przez WebSocket. Semantykę wyłączania automatyzacji opisuje [HA automation services](https://www.home-assistant.io/docs/automation/services/).

Telemetria utrzymuje ograniczone próbki czasów, p95, RSS i CPU procesu oraz prędkości uczenia. Wyniki lokalne są w raporcie wydania. Cele Raspberry Pi 4 wymagają pomiaru na urządzeniu. Na Windows RSS /proc jest niedostępny, a nie równy zeru.

## Poprawka przejęcia 0.9.2

Globalny niepełny skan automatyzacji daje ostrzeżenie. Executor przejmuje znane automatyzacje celu i sprawdza OFF. Ostatnie poprawne powiązania są zachowywane w SQLite także na wypadek błędu odczytu lub restartu; nie zastępują świeżej konfiguracji, jeśli odczyt się powiedzie. [Szczegóły](RELEASE_0_9_2.md).
