# Adaptive AI 0.14.146 — trajektoria w treningu i decyzjach

Panel Home Intelligence pokazywał wyuczone przejścia między pomieszczeniami, ale nie gwarantowało to ich wykorzystania przez każdego agenta. Routing zdarzeń pomijał poprzedzające pomieszczenia spoza lokalnego schematu automatyzacji. Bez backfillu wspólnego modelu trening mógł odtwarzać kontekst z pustymi statystykami tras. W przekazanym eksporcie ESPEN4 był ponadto wykryty jako wejście automatyzacji, ale nie miał przypisania do obszaru modelu domu.

## Zmiany

- Zdarzenia z wyuczonych poprzedników, kolejnych pomieszczeń i alternatywnych odgałęzień trafiają do odpowiedniego agenta nawet przy schemacie ograniczonym do lokalnego radaru. Nowa trasa od razu unieważnia indeks routingu. Obce pomieszczenia bez powiązania z trasą nie uruchamiają wszystkich agentów.
- Podczas aktywnej prognozy wejścia lub wyjścia agent sprawdza ją również po sekundzie, ponieważ prawdopodobieństwa dla 1/3/5 s zmieniają się bez nowego zdarzenia HA. Hold ręczny zachowuje pierwszeństwo.
- Jednoznaczna para progów ON/OFF dla radaru może uzupełnić brakujący obszar wewnątrz HomeMind. Dotyczy to dokładnego źródła oraz kanałów tego samego urządzenia lub ścisłej rodziny kanałów ESPHome. Znany obszar HA, jawny sygnał z granicy pomieszczenia oraz konflikt między docelowymi obszarami blokują takie wnioskowanie. Przypisanie ma widoczne pochodzenie i dowody; rejestr HA nie jest zmieniany.
- Binarne kanały tej samej rozpoznanej rodziny radaru zachowują semantykę obecności radarowej. Sama dodatnia energia albo odległość nie staje się etykietą obecności i nie tworzy trajektorii.
- Trening uczy statystyki przejść przyczynowo z dostępnej historii bez konieczności wcześniejszego ręcznego backfillu Home Intelligence. Używa bezpośrednich sensorów obecności, nie sterowanych świateł ani bieżącego grafu z przyszłości. Migawki mają łączny limit 8 MiB; kursory odtwarzają własny stan. Rewind i późno odebrane zdarzenia nie ujawniają późniejszych tras wcześniejszym przykładom.
- Eksport Correct i szczegóły agenta pokazują źródła zdarzeń trajektorii. Diagnostyka sensorów pokazuje przypisania wywnioskowane z radaru automatyzacji.

## Instalacja i weryfikacja

Po aktualizacji odśwież stronę i wykonaj pełny Rebuild agenta. Rewizja treningu to `shared-home-intents-v26`. Ręczny backfill wspólnego modelu pozostaje opcjonalny. Sensorom obecności w pozostałych pomieszczeniach nadal potrzebne są obszary HA albo jawne mapowanie HomeMind. Jeśli radar udostępnia wyłącznie energię i odległość, brak potwierdzonej trajektorii nie będzie zastępowany wymyśloną obecnością; warto udostępnić jego binarną encję obecności.

Testy obejmują wpływ trajektorii na zapisany klasyfikator przy identycznym lokalnym odczycie, alternatywną trasę, natychmiastowe unieważnienie routingu, zmianę mapowania bez fikcyjnego wejścia, odtworzenie tras bez backfillu, rewind oraz czas odebrania zdarzenia. Krótkie poprawne wizyty pozostają poprawnymi demonstracjami. Wynik syntetyczny nie potwierdza jeszcze przewagi w domu ani usunięcia wszystkich przypadków przedwczesnego OFF.
