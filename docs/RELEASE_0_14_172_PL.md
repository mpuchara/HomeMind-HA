# HomeMind Adaptive AI 0.14.172

Zajęty agent ponawia teraz decyzję wyłącznie dla własnego urządzenia. Poprzednio callback kończącego się zadania zwracał rzeczywiste encje wyzwalające do globalnego `dirty_entities`. Inni agenci korzystający z tych sensorów otrzymywali stare zdarzenie ponownie, a zajęci odkładali je do kolejnych ponowień. Przy nakładającej się pracy kilku celów powstawała pętla decyzji i zapisów bez nowych zdarzeń HA.

## Nowy log 0.14.171

Średni `wrapped_inference` wynosi około 334 ms, ale pojedyncze przebiegi dochodzą do około 15 s. Trace obejmuje już cztery różne agenty Shadow, więc prostego porównania CPU i średnich z wcześniejszym pomiarem dwóch agentów nie można przypisać samej aktualizacji. Otwarcia sesji SQLite są już wspólne dla przebiegu, zgodnie z poprawką 0.14.171.

Metryka event → decision pokazuje około 150 s. Ponowne globalne wysyłanie starych zdarzeń może sztucznie podtrzymywać ten wiek. Osobno REST resync dodawał zmienione encje do kolejki, pozostawiając ich stary timestamp WebSocket — nowe wykrycie stanu mogło więc wyglądać jak reakcja na zdarzenie sprzed kilku minut. Sam log nie dowodzi, że wszystkie widoczne 150 s to rzeczywiste oczekiwanie użytkownika.

WAL ma 3,64 GB, checkpointy dochodzą do około 17,8 s, ale ostatni raport pokazuje `remaining_frames=0`. Rozmiar pliku nie oznacza tutaj 3,64 GB nieprzepisanych danych. SQLite może zachowywać i wykorzystywać plik ponownie; [opis checkpointów i resetowania WAL](https://www.sqlite.org/wal.html). Ograniczamy zbędne decyzje i zapisy, zachowując istniejący PASSIVE checkpoint. Rozmiar i koszt WAL trzeba sprawdzić następnym logiem; to wydanie nie wykonuje TRUNCATE ani nie zmienia ustawień trwałości.

## Zachowane zachowanie

Ponowienie zachowuje rzeczywiste encje wyzwalające, trafia do właściwego celu i korzysta z aktualnego, spójnego snapshotu stanu oraz rewizji. Łączy się z nowym zdarzeniem w jednym przebiegu tego celu. Aktualna konfiguracja i zależności są sprawdzane przed wysłaniem; usunięty lub wyłączony właściciel nie jest zastępowany innym agentem. Chwilowy błąd odczytu routingu pozostawia ponowienie do kolejnej próby.

Każda nowa zmiana HA nadal trafia do wspólnego modelu domu i historii. Oryginalne trajektorie, cechy, wagi, kontrola sum modeli oraz zabezpieczenia Executor pozostają zachowane. Timer i dokładny termin gaszenia nadal mają swoją ścieżkę. Pętla silnika obsługuje gotowe ponowienie także bez kolejnego zdarzenia HA i zachowuje je przy zamkniętej bramie startowej.

REST resync usuwa stary timestamp WebSocket tylko dla encji faktycznie zmienionej przez odpowiedź REST. Dalej budzi decyzję dla tego stanu. Nowsze zdarzenie WS odebrane w trakcie requestu zachowuje swój stan i timestamp; nie zostaje cofnięte przez odpowiedź REST.

## Walidacja

Dodano 17 regresji: izolacja celów, zachowanie obu rzeczywistych zdarzeń, zbieżność kolejki, aktualny snapshot, łączenie retry ze świeżym eventem, zmiana schematu i właściciela, błąd indeksu i uruchomienia zadania, timer, koalescencja burstu, callback kończący się w wyścigu, anulowany worker, ponownie zajęty cel, brama startowa i oba przypadki REST/WS. Dwie wcześniejsze regresje nadal sprawdzają zachowanie prawdziwego wyzwalacza, teraz w kolejce właściwego celu. Pełny zestaw obejmuje 2060 testów.

Paired benchmark porównuje zamrożony dispatcher 0.14.171 z nowym, wykorzystując rzeczywisty `Engine.process`, `process_target` i zapisy SQLite. Po dwóch zdarzeniach stary kod wykonuje 64 decyzje w limicie eksperymentu i nadal ma kolejne w kolejce. Nowy kończy na czterech decyzjach dla dwóch celów albo ośmiu dla czterech. Oba zdarzenia docierają do każdego celu, końcowy Desired jest identyczny, a wszystkie wykonane decyzje są zapisane. [Pełny raport](benchmarks/TARGET_RETRIES_0_14_172.json).

To deterministyczna regresja nakładających się workerów, bez kosztu modeli i HA. Timery nie są należne podczas tego krótkiego scenariusza. Wynik nie jest prognozą przyspieszenia całej instalacji; pokazuje usunięcie samopodtrzymującej się dodatkowej pracy.

Symulator antycypacji ma stały czas scenariusza. Jego cechy kalendarzowe wcześniej zależały od godziny uruchomienia, a test gałęzi Bedroom zawodził również na bazowym 0.14.171. Oczekiwane predykcje, prawdziwy model, kontrola dispatchu i nagrody pozostają sprawdzane.

## Instalacja

Zaktualizuj do 0.14.172 i uruchom dodatek ponownie. Model i historia nie wymagają Rebuild ani ponownego treningu. Po 10–15 minutach pracy tych samych agentów sprawdź `wrapped_inference`, liczbę decyzji, CPU, opóźnienie event → decision oraz checkpointy WAL. Kolejny eksport pozwoli ocenić pozostałe skoki I/O i koszty pełnej walidacji modeli.
