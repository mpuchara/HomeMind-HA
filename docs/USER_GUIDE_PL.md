# Adaptive AI — aktualna obsługa

Ten dokument opisuje bieżący produkt. Starsze wersje instrukcji pozostają w repo jako dokumenty historyczne.

## Główny ekran

Aplikacja uruchamia interfejs HTTP przed cięższą inicjalizacją runtime. Status startu pokazuje, czy gotowe są baza, Engine, realtime Home Assistant, historia i workery. Normalnie stan HA jest odbierany przez WebSocket; REST pozostaje ścieżką resynchronizacji/fallbacku. Od 0.14.30 pierwszy pełny przebieg inference nie startuje w trakcie składania runtime: po `startup.ready` Ingress dostaje 3 s zapasu, a pula workerów inference jest ograniczona do maksymalnie 4. Initial REST snapshot służy do rozgrzania stanu i nie jest traktowany jako tysiące realtime transition. Od 0.14.31 runtime jest faktycznie event-driven: 1-sekundowy tick tylko sprawdza lekkie deadline'y, nie uruchamia wszystkich agentów. Fast target ma awaryjny heartbeat 10 s, pozostałe 30 s, a zwykły event HA uruchamia tylko agentów zależnych od targetu, ich aktywnego schema, jawnych wejść lub źródeł obecności z tego samego obszaru. Od 0.14.32 końcówka background discovery po imporcie Recorder nie wykonuje pełnych statystyk całego archiwum ani osobnych skanów historii dla każdego targetu/właściwości. Klasyfikacja aktywności targetów korzysta z jednego ograniczonego strumienia lokalnego archiwum, dzięki czemu istniejący throttle CPU faktycznie obejmuje całą pracę.

## Karta Live

### Current
Aktualna wartość targetu z Home Assistant.

### Desired
Bieżąca decyzja polityki. W Shadow jest wyłącznie predykcją. W Control może przejść dalej do `ActionIntent`, ale dopiero Executor może wysłać usługę HA.

### Decision strength
Starsze pole nazywane `confidence` pozostaje kompatybilne w API, ale nie jest prezentowane jako prawdopodobieństwo komfortu. To siła decyzji wynikająca m.in. z separacji akcji, niepewności modelu i pokrycia kontekstu.

### Presence probability / Forecast uncertainty
To osobne wielkości modelu percepcji. Jeżeli pole jest prawdziwym prawdopodobieństwem, jego kalibracja jest oceniana na niezależnych przyszłych epizodach (Brier/reliability). Nie należy go utożsamiać z Decision strength.

### Data coverage / Held-out policy quality
Pokrycie mówi, ile odpowiednich dowodów ma model. Jakość polityki opisuje wynik na odłożonych danych. Dla bezpieczeństwa ON i OFF są oceniane osobno.

## Shadow, Control i Paused

### Shadow
- polityka wykonuje inference,
- Desired jest widoczne,
- może powstawać future evidence i porównanie Candidate,
- **żadna komenda fizyczna nie jest wysyłana**.

Shadow to proxy kontrfaktyczne: pokazuje, co model chciałby zrobić. Nie dowodzi fizycznego skutku niewykonanej akcji.

### Control
Control przechodzi dodatkowe zabezpieczenia niezależne od rewardu:
- ważna kwalifikacja,
- wystarczające osobne dowody dla ON/OFF,
- legal action mask,
- support/novelty i abstain,
- manual override,
- cooldown/min dwell,
- ownership/lease współdzielonego urządzenia,
- aktualność modelu i konfiguracji,
- ACK oraz bezpieczeństwo Executora.

Dla binarnego Control bieżący kontrakt statystyczny wymaga co najmniej 20 held-out próbek na akcję i 95% dolnej granicy Wilsona powyżej progu 78% dla każdego kierunku. Sam wysoki procent accuracy nie wystarcza.

### Paused
Normalne realtime inference/trening jest wyłączone lub ograniczone. Brak historii nie powoduje automatycznego obniżenia progów; system powinien pokazać brak dowodu i korzystać z fallback/Shadow.

## Live i Candidate

Live jest aktualną stabilną generacją. Candidate jest izolowanym bezpośrednim dzieckiem konkretnego rodzica i pozostaje Shadow do jawnej, atomowej promocji.

Karta generacji udostępnia:
- **Autonomous** — rozwój dziecka z dostępnych danych,
- **Correct** — jawna korekta na obserwowanej historii,
- **Explore** — kontrolowane badanie, w tym targeted sensor,
- **Change decision** — natychmiastowa jawna informacja o decyzji w konkretnym kontekście,
- **Promote** — osobny krok lifecycle,
- **Discard** — odrzucenie dziecka bez niszczenia rodzica.

Dalsza nauka Candidate tworzy kolejne dziecko. Feedback nie może przechodzić przez granicę niewłaściwego parent generation.

## Correct i Change decision

**Correct** jest etykietą użytkownika dla konkretnego obserwowanego kontekstu/Desired. Nie jest zwykłym rewardem „+1”. Model dziecka musi spełnić zaznaczone korekty i przejść regresję względem ważnych anchorów.

**Change decision** opisuje bieżącą preferencję/oczekiwaną decyzję. Brak feedbacku nie oznacza akceptacji. Negatywna ocena jednej akcji nie wymyśla automatycznie poprawnej przeciwnej akcji.

Trwała instrukcja użytkownika nie wygasa tak jak statystyka z historii.

## Explore i eksperymenty

Explore korzysta z istniejącej infrastruktury eksperymentów. Próba ma wersjonowany `TrialRecord`: hipotezę, dostępne akcje i propensity, przypisaną akcję, dispatch/ACK, źródła outcome, wynik i status aplikacji do konkretnego dziecka.

Candidate nie przejmuje fizycznego sterowania. Jeżeli Explore wymaga fizycznej próby, jej właścicielem pozostaje root Live oraz istniejący Executor. Brak outcome pozostaje nieznany zamiast być zamieniany na sukces/porażkę.

## Promocja

Promocja nie opiera się na jednym `promotable=true`. Runtime składa listę nazwanych wyników walidacji. Każde veto pozostaje widoczne; kolejność modułów nie może go usunąć.

Dane do wyboru challengera i finalnej oceny są rozdzielone. Po selection zbierany jest fixed future test; regularne odpytywanie UI nie wydłuża go opportunistycznie. Same poprawne OFF nie kwalifikują ON.

Promote wykonuje atomowy swap modelu/generacji/trybu/ownership. W razie błędu poprzedni snapshot jest odtwarzany.

## Cold start i dryf

Nowy dom lub agent bez historii pozostaje w fallback/Shadow i raportuje brak danych. System może zasugerować niewielką liczbę opcjonalnych pytań użytkownikowi, ale odpowiedź nie omija zabezpieczeń.

Monitor dryfu rozróżnia:
- awarię sensora,
- zmianę topologii/przeniesienie sensora,
- zmianę zwyczaju,
- nową preferencję.

Pogorszenie tworzy izolowanego Candidate. Live nie jest natychmiast resetowany. Po promocji monitorowane jest odzyskanie jakości i możliwy rollback.

## DeviceAgent i współdzielone urządzenia

Kilka encji HA może reprezentować jeden fizyczny zasób. HomeMind używa `device_id` lub jawnego mappingu, a nie friendly name. Power i brightness jednej lampy współdzielą arbiter, manual override i lease. Wydanie brightness jest spójną pojedynczą komendą; 0% oznacza OFF.

Nieopisane `switch/number/select` nie dostają autonomii tylko dlatego, że są zapisywalne. HVAC i rolety mają inną dynamikę niż szybkie światło i wymagają odpowiedniego modelu procesu.

## Co oznacza „RL” w HomeMind

W produkcie występuje kilka różnych mechanizmów, których nie należy mieszać:

- **historyczna demonstracja**: obserwowane zachowanie targetu; służy do bootstrapu/uczenia, ale nie jest prawdą o komforcie,
- **contextual bandit**: aktualizuje tylko logowaną/wykonaną akcję; niewybrana akcja ma nieznany reward,
- **model obecności**: osobna percepcja/prognoza z własną kalibracją,
- **Correct/preference**: jawna informacja użytkownika,
- **experiment/TrialRecord**: kontrolowana próba z outcome attribution,
- **Shadow**: kontrfaktyczna predykcja/proxy,
- **physical outcome**: obserwowany skutek faktycznie wysłanej komendy po Executorze.

ACK potwierdza wykonanie transportowe, a nie komfort. Replay automatyzacji nie jest etykietą potrzeby światła.

## Benchmark produktu F24

Repo zawiera deterministyczny benchmark produktu z ukrytą obecnością i ukrytą potrzebą światła. Obserwacje mają delay, noise, missingness, różne formaty oraz sprzężenie `light -> lux`. Scenariusze obejmują m.in. jednego/dwóch domowników, rozwidlenie, bezruch, brak przyjścia, quick return, dzień/noc, ręczną zmianę, fałszywy/przeniesiony sensor i zmianę zwyczaju. Przeniesiony sensor naprawdę zmienia w future test swoje przypisanie obszaru w Entity Registry. Ręczna zmiana jest zdarzeniem targetu z pochodzeniem użytkownika (`context.user_id`), dzięki czemu benchmark może odróżnić manual hold od zwykłej zmiany hidden truth. Szybkie targety (np. światła) zachowują krótki timing akcji, ale korzystają z tego samego manual hold co pozostałe urządzenia. Dla `light.power` historyczna polityka nie może zgasić już włączonej lampy po pojedynczym krótkim flipie predykcji: statystyczny OFF musi utrzymać się ciągle przez 6 s. ON pozostaje natychmiastowe. Ręczny OFF oraz jawne instrukcje/preferencje nie są przez to opóźniane.

Oficjalne uruchomienie:

```bash
python tools/run_product_runtime_benchmark.py --seeds 11,23,37 --replicas 1
```

Executable instaluje finalny `RuntimeCompositionRoot` przed treningiem i uruchamia każdy seed w świeżym procesie. Porównywane są stała automatyzacja, bieżący runtime, full-ridge Shadow oraz ostrożny fallback na oddzielnych train/validation/future danych. Wynik zawiera średnie, 95% przedziały niepewności i jawną listę `unmet_criteria`.

Benchmark nie promuje modelu i nie obniża progów. Brak poprawy lub niespełnione kryteria są prawidłowym wynikiem. Wynik syntetyczny nie zastępuje testu na realnym Home Assistant ani fizycznego M&V. W CI raport z trzech seedów jest zachowywany jako artefakt `product-benchmark-f24-py311`; zawiera metryki, 95% przedziały niepewności i `unmet_criteria`. Czytelne podsumowanie aktualnego przebiegu znajduje się w `BENCHMARK_PRODUCT_F24.md`.

### Tryb operational-first od 0.14.33

Po restarcie dodatek uruchamia zapisanych agentów i realtime Home Assistant bez automatycznego importu Recorder, auto-discovery ani okresowego historycznego maintenance. `Rescan devices` jest jawną akcją użytkownika i uruchamia discovery w osobnym workerze; request HTTP wraca od razu. Cykliczne odczyty UI używają tylko konfiguracji agentów i bieżącego stanu runtime, bez COUNT/AVG po tabelach historii. Dzięki temu system może pracować stale bez uruchamiania ciężkich zadań. Train/Resume/Rebuild/Correct/Candidate nadal korzystają z istniejącej kolejki ciężkich zadań i nie zmieniają własności fizycznego sterowania.


### Odciążony runtime agentów od 0.14.34

W zwykłej pracy event z Home Assistant uruchamia tylko agentów zależnych od zmienionej encji. Konfiguracje agentów, zależności, preference facts, Candidate lineage bez aktywnego Candidate, provenance aktywnych komend oraz pochodzenie bieżącego eventu są trzymane w RAM i jawnie unieważniane przy zmianach. Shadow nie wykonuje durable walidacji Control, a feature observations, Shadow provenance i evidence windows są zapisywane poza ścieżką event -> intent przez ograniczone kolejki i batch write do SQLite. Control zachowuje pełną walidację przed fizycznym HA service call.

W statusie runtime dostępny jest ponownie recent `event -> intent p95` oraz backlog odroczonego feature journal. Dla testu na Raspberry Pi ważne jest obserwowanie, czy p95 i backlog pozostają stabilne po kilkudziesięciu minutach pracy wielu agentów; wersja 0.14.34 jest pierwszym buildem po tej przebudowie i wymaga realnego soak testu przed uznaniem PR za gotowy do merge.


### RAM-first dla instalacji na karcie microSD od 0.14.35

W Raspberry Pi baza dodatku zwykle znajduje się na karcie microSD, dlatego 0.14.35 ogranicza małe, częste operacje SQLite. Dane tymczasowe i łatwe do odtworzenia są buforowane w RAM i zapisywane większymi paczkami: bieżące eventy diagnostyczne, provenance eventów HA, feature observations/evidence windows, decision history oraz live archive. Cykliczne `/api/status`, `/api/agents` i `/api/events` korzystają z pamięci RAM zamiast wykonywać regularne odczyty tabel przy każdym pollingu.

Konfiguracja agentów i routing są unieważniane zmianą revision, także gdy lista agentów jest pusta. Pełny skan tabeli agentów pozostaje tylko awaryjnym fallbackiem co 10 minut dla zewnętrznych zmian wykonanych bez API Store. SQLite używa `temp_store=MEMORY`, większego cache stron i mmap, jeśli platforma go obsługuje.

Trwałe granice bezpieczeństwa nie zostały przeniesione do RAM. Modele, konfiguracja, explicit feedback oraz ścieżka Control/command pozostają trwałe, a Control nadal odczytuje i waliduje konfigurację przed fizycznym service call. Przy twardej utracie zasilania można utracić jedynie ostatnią krótką porcję danych obserwacyjnych/diagnostycznych oczekujących na batch flush, nie stan wymagany do bezpiecznego sterowania.

W teście Raspberry Pi obserwuj `event -> intent p95`, `feature_journal.pending`, `provenance_queue.events.pending` oraz `ram_persistence_buffers`. Kolejki mogą chwilowo rosnąć, ale przy stabilnej pracy powinny okresowo wracać w okolice zera, a p95 nie powinno narastać wraz z czasem działania.


W 0.14.35 także Sensor Tournament shadow oraz fast-light timing nie utrwalają już każdej pojedynczej próbki osobną transakcją. Bieżące residual models, timing metrics i weight-only checkpoints pozostają w RAM i są deduplikowane, a writer zapisuje najnowszy stan paczką co maksymalnie kilka sekund. Diagnostyka wieku schematu jest odczytywana z cache i dotyka SQLite tylko po zmianie revision/signature.


### Poprawki Raspberry Pi po pierwszym soak teście - 0.14.36

0.14.36 naprawia regresje widoczne po optymalizacji 0.14.35. Lista agentów w UI korzysta teraz z pełnej pamięci konfiguracji, dlatego agenci PAUSED, WAITING i NEEDS_RETRAIN nie znikają tylko dlatego, że nie są aktualnie dopuszczeni do inferencji. Osobny, mniejszy indeks nadal obsługuje wyłącznie routing realtime.

Widok Candidate po restarcie nie wykonuje już zbiorczych COUNT/AVG po dużej historii uczenia tylko po to, aby narysować kartę. Status Candidate, confidence i promotion validation korzystają z konfiguracji oraz trwałego podsumowania porównania, a każdy Candidate jest oceniany raz na odświeżenie. Polling Candidate został zmniejszony z 1,5 s do 4 s.

Panel Home Intelligence ponownie pokazuje rzeczywisty stan modelu trajektorii zamiast zer pochodzących z pustego payloadu lifeline. Diagnostyka jest liczona z RAM i cache'owana przez 5 s. Liczniki buforów RAM w `/api/status` nie czekają na blokady zapisu SQLite, więc wolna transakcja na microSD nie powinna blokować samego odczytu statusu.
