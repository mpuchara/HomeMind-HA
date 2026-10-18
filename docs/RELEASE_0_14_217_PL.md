# 0.14.217: pomiar, szybsze odczyty, bieżące ramki i odciążenie zapisu

Bazą jest 0.14.216, lokalny commit `cd8a0f817d360cf39351248db7a119f26ea4fca7`, drzewo `abd44d645e0f5f9fd96ba982d00de587e2a5f769`. Plan etapowy: [audyt](PERFORMANCE_AUDIT_0_14_216_PL.md).

## Zmiany wykonawcze

- User Promotion pomija dwa pomocnicze odczyty obserwacji, jeżeli dany manager ma zainstalowany kontrakt stabilnego zbierania dowodów dla zablokowanego Candidate. Ten adapter już wcześniej miał pustą bramkę obserwacji. Standardowy i niestandardowy Promote zachowują swoje trwałe kontrole.
- Explore pomija odczyt sesji, gdy aktualny stan Experiments pod jego blokadą dowodzi braku aktywnej próby. Puste zakończenie nie zużywa budżetu wyników Explore. Aktywna próba nadal wykonuje świeży SELECT; usunięto osobny odczyt sqlite_master.
- Pełny opis urządzenia pobiera świeże mapowania SQL po maksymalnie 256 encji zamiast osobno dla każdej encji. Sprawdzanie konfliktów i wspólnego manual hold wylicza tylko klucze zasobów, bez enumerowania wszystkich capabilities domu. Jeden przebieg manual hold odczytuje mapowania wszystkich celów zbiorczo. Reguły device/group/zone, wykluczenie własnego agenta i trwałość blokad pozostają zgodne; nie ma cache mapowań pomiędzy wywołaniami.
- Zmiana tras Home Context przebudowuje indeks zależności z aktualnej konfiguracji w RAM. Nie wymusza odczytu wszystkich agentów. Osobny czas odczytu konfiguracji zapobiega przedłużaniu jej TTL przez częste zmiany tras. Zmiana rewizji konfiguracji, wymuszenie i dotychczasowe unieważnienie indeksu nadal wymagają odczytu SQL. Cache routingu nie jest zgodą na fizyczne sterowanie.
- Ewaluator epizodów pomija odczyt krawędzi Candidate dla potwierdzenia dodania zadania do kolejki bez wyników predykcji. Rzeczywiste wyniki Parent/Candidate nadal przechodzą dotychczasową analizę epizodów.
- Candidate Shadow pomija odczyt krawędzi i konfiguracji, gdy żaden zaobserwowany Desired się nie zmienił, więc nie mógł zakończyć się false-early excursion. Możliwa zmiana nadal wykonuje świeży odczyt. Ewaluator pomija odczyt, gdy nie ma rozpoczętej próby i wszystkie predykcje zgadzają się z Current. Rozpoczęta próba zachowuje świeże odczyty krawędzi, dotychczasowy termin i rozliczenie, także przy usunięciu generacji przez raw SQL.
- Cały odczyt `/api/candidates` współdzieli jedno połączenie SQL, zwalniane przed serializacją i wysłaniem odpowiedzi. Każdy zagnieżdżony helper zachowuje własny commit/rollback i świeży odczyt. Nie ma jednej transakcji na całe żądanie ani cache wyników. Test sprawdza commit z drugiego połączenia pomiędzy dwoma odczytami oraz zamknięcie połączenia po błędzie.
- Nie ma nowego cache zgody na sterowanie ani lifecycle TTL. Zmiana tabeli/sesji przez raw SQL lub inne połączenie pozostaje widoczna. Brak opcjonalnej tabeli oznacza brak sesji; inne błędy SQLite pozostają błędami.

## Odświeżanie widocznych kafelków Candidate

`candidate_preference_ui.js` wykonuje lekki odczyt co 100 ms po zakończeniu poprzedniego, zamiast co sekundę. `liveBusy` i wspólny broker zachowują współdzielenie odczytów w locie. Cache w `runtime_activity_ui.js` wygasa po 100 ms zamiast po sekundzie; początkowy pusty cache nie blokuje pierwszej karty. Ukryta strona nie odpytuje backendu, a brak kart Candidate oznacza zero żądań do tego endpointu. Ciężki `/api/candidates` nadal ma osobny, wolniejszy cykl.

Nowy test VM uruchamia wszystkie trzy produkcyjne warstwy JavaScript. Sprawdza 100 ms, wygaśnięcie cache, jeden odczyt przy równoległych klientach, pierwszą kartę oraz brak HTTP dla ukrytej strony i bez kart. To dowód zachowania interfejsu, a nie zmierzony czas reakcji fizycznego urządzenia. Benchmark backendu odtwarza teraz odczyt Candidate co 100 ms dla obu źródeł, żeby koszt szybszego panelu był uwzględniony w porównaniu. Próby ze starym profilem 1 s zachowano osobno w `PRE_UI_1S`.

## Bieżące ramki i kolejność celów, część E2/E3

Scheduler dopuszcza tyle rzeczywistych zadań zdarzeń/timerów, ile jest wolnych pracowników. Oczekujące cele mają kolejkę FIFO zmian w RAM, a ich stan i zegary są pobierane do ramki dopiero przy dopuszczeniu. Kolejne zmiany tego samego celu koalescują do bieżącego stanu. Nie tworzy się kolejka do 20 starzejących się ramek wewnątrz ThreadPoolExecutor. Liczba pracowników pozostaje bez zmian. Licznik `capacity_deferrals` ujawnia odroczenia; zakończenie pracy wybudza kolejne cele bez aktywnego oczekiwania. Ponowienia, anulowanie, zmiana właściciela/zależności i brak zegarów zdarzeń dla timerów pozostają sprawdzane testami.

Candidate Shadow również zachowuje pierwotne miejsce roota w FIFO przy wymianie oczekującego payloadu na nowszy. Częsty sensor nie przesuwa stale własnego agenta na koniec kolejki. Limit liczby rootów, najnowsza ramka, cutoff i brak HA z Candidate pozostają. Rewizja generacji jest dodatkowo weryfikowana po predykcji, przed publikacją do RAM. Jej zmiana w czasie obliczenia oznacza jawną lukę, bez publikacji starej decyzji lub dopisania jej do historii. `oldest_root_wait_ms` ujawnia czas oczekiwania roota niezależnie od wieku zastępującej ramki.

## Ograniczenie porcji zapisu, część E6

Pełny runtime ujawnił transakcje opcjonalnych writerów zajmujące Store przez sekundy. Po zapełnieniu kolejki okien Feature Journal włączał się istniejący synchroniczny fallback na pracowniku celu. Dlatego część E6 przesunięto do pierwszego pakietu. Sam podział transakcji nie daje gwarancji poprawy całej reakcji: wczesna próba zwiększyła przepustowość Candidate, ale pogorszyła p95 Root. Zachowano jej wynik jako `WRITER_PROBE`, a końcowy pakiet wymaga osobnego porównania `FINAL_*`.

`ContextRowBuffer` utrwala maksymalnie 64 niezależne, kumulatywne wiersze na commit i zwalnia Store pomiędzy porcjami. Przy błędzie wcześniej zatwierdzone porcje pozostają trwałe, niezapisane wracają do kolejki, a nowszy oczekujący stan danego klucza ma pierwszeństwo. To celowa zmiana granicy transakcji dla niezależnych diagnostyk, bez łączenia jej z komendami, feedback ani parami dowodów. Pamięć aktywnej porcji pozostaje naliczana do jej zatwierdzenia. Publiczny flush nadal opróżnia całą odłączoną porcję.

Writer Feature Journal wykonuje jeden przebieg po maksymalnie 64 obserwacjach i 128 oknach, następnie ponownie sygnalizuje zaległą pracę. Jawny flush i bariera zamknięcia nadal opróżniają obie kolejki. Każda porcja obserwacji i każda porcja okien zachowuje atomowość, w tym powiązania encji i ochronę zdarzeń. Istniejące `storage_batch` ogranicza powtarzanie kosztownych pomiarów admission w transakcji, zachowując świeże kontrole na wejściu, wyjściu i commit oraz limity payload, stron, WAL i wolnego miejsca. Nowe metryki oddzielają oczekiwanie na Store i czas jego zajęcia.

Ochrona zdarzeń dla nakładających się okien jest aktualizowana grupowo według zestawu encji. SQL wylicza dokładnie ten sam punktowy MAX czasu ochrony, z domkniętymi granicami i bez wypełniania przerw pomiędzy oknami. Zostają wszystkie wpisy okien, dotychczasowa retencja i semantyka duplikatu ID. Grupy aktualizacji mają maksymalnie 898 parametrów, wstawiania 900. Test obejmuje 600 okien z różnymi terminami ochrony, zestawami encji i powtarzanymi ID, na połączeniu z wymuszonym limitem 999 parametrów.

Okna i ich powiązania są wstawiane przez ograniczone grupy VALUES. Usuwa to powtarzane przełączanie interpretera pomiędzy pojedynczymi krokami SQLite i inferencją. Zwykła grupa ma do 256 KiB parametrów, a każdy zapis nadal przechodzi istniejące kontrole admission i VM. Pojedynczy większy wiersz zachowuje dotychczasową kontrolę limitu payload. Wcześniejsze wyniki samego grupowania ochrony i mniejszych porcji zapisu pozostają próbami historycznymi; końcowy pomiar obejmuje także grupowe wstawianie.

Obserwacje również otrzymują grupowy odczyt ochrony i zapis VALUES. Jeżeli najwyższy czas ochrony pokrywa cały przedział czasów próbek danej encji w porcji, jest dokładnym wynikiem dla każdej z nich. W innych przypadkach pozostaje dokładny odczyt per próbka, bez domykania przerw. Ten skrót nie zmienia ochrony ani zbioru przyjętych obserwacji. Oba warianty oraz encje bez okien mają test zgodności. Odczyt zachowuje reguły duplikatów, z zachowaniem reguł MIN czasu odbioru oraz MAX jakości/ochrony dla duplikatów zdarzenia. Odczyt i zapis należą do jednej rezerwacji writera SQLite, poprzedzonej dotychczasowym admission WAL/checkpoint. Inny writer nie może dopisać okna pomiędzy wyliczeniem ochrony a wstawieniem obserwacji. Testy sprawdzają rzeczywisty konflikt z drugim połączeniem i odmowę zapisu w czasie reclaim WAL.

Przy pełnej kolejce nieobowiązkowe okno `decision` jest odrzucane przed przyjęciem do RAM, z jawnym `overflow_gaps`, czasem i powodem ostatniej luki oraz licznikiem telemetryki. Nie wykonuje synchronicznego SQL przed Executor. Wszystkie wcześniej przyjęte okna i surowe obserwacje pozostają zachowane. Okna korekt użytkownika mają dotychczasowy trwały fallback i retry. Test z zajętym Store i 4096 oczekującymi oknami sprawdza, że nowa decyzja dociera do Executor, a osobny test sprawdza trwałość korekty przy tej samej presji.

Luki dotyczą metadanych opcjonalnej retencji. Mogą ograniczyć dostępność starszych fragmentów historii poza zwykłą retencją obserwacji; nie wolno przedstawiać archiwum z lukami jako kompletnego. Nie jest to redukcja częstotliwości odczytu sensorów ani odrzucanie przyjętych komend/feedback. Wynik porównania należy oceniać także po liczbie takich luk i wieku kolejki, nie tylko p95.

## Pomiar E0

`ws_receive_to_engine`, `ws_receive_to_target_start`, `ws_receive_to_decision` oraz `ws_receive_to_worker_release` zaczynają się po powrocie `ws.recv`, przed parsowaniem i oczekiwaniem na Engine. Znacznik jest własnością transportu, korelowany z encją i rewizją tego samego snapshotu. Nie obejmuje wcześniejszego buforowania w HA/socket. Przy koalescencji jest to czas dla zachowanych najnowszych zmian encji przypisanych do celu, a nie pomiar każdej odebranej zmiany.

Dotychczasowe `event_to_decision` i `event_to_intent` zachowują punkt początkowy, sposób liczenia i nazwy. Ich punkt początkowy to dotychczasowy zapis wewnątrz Engine po przejęciu jego blokady. Nowy kafelek diagnostyki pokazuje osobno WS receive → decision. Timer i REST nie pożyczają poprzedniego znacznika WebSocket. Dodatkowa mapa zegarów ma limit 8192 encji; ewikcja ogranicza diagnostykę, bez zmiany decyzji.

`event_to_worker_release` ujawnia czas do wyjścia całego pipeline celu, także po gotowej decyzji. `context_ingest_lock_wait/locked_work` i `context_forecast_lock_wait/locked_work` oddzielają oczekiwanie od pracy pod wspólną blokadą. Zapis pomiaru następuje po zwolnieniu mierzonej blokady. Eksport runtime zawiera istniejący szczegółowy `inference_hot_path`.

## Benchmark pełnej kompozycji

`tools/benchmark_full_runtime.py` uruchamia `trial_queue_main` i rzeczywiste `core.main()`, HTTP, schedulery, usługi końcowej kompozycji, polityki i obserwatorów. Podstawiony jest transport HA, adres serwera i jawna konfiguracja fixture. Predykcja, cechy Root, dopuszczenie Executor i uczenie pozostają produkcyjne. Fixture tworzy wytrenowane modele i opcjonalne generacje Candidate `offline_blocked`, 778 encji oraz do 20 kwalifikujących się Root.

Skrypt sprawdza 7 warstw pipeline agenta, 4 warstwy zdarzeń, faktyczne wykonanie Candidate i brak usług HA w Shadow. Osobne `control_dispatch_exercised` rozróżnia Control, który wysłał usługi, od samego przebiegu blokowanego przez bramki. Wygasłe intencje są raportowane; TTL pozostaje bez zmian. Raport podaje CPU, start, liczbę próbek, p50/p95/p99/max, zakończone obserwacje, luki i hash źródeł. Rozgrzewka czeka na ukończoną decyzję każdego Root; kolekcja rozkładów zaczyna się później. `warmup_pipelines_drained` ujawnia, czy pełne przebiegi były już zwolnione. `lifecycle_read_function_calls` liczy wywołania wskazanych helperów, z osobnym koszykiem pracowników celu; nie jest licznikiem wszystkich instrukcji SQL.

`tools/benchmark_full_runtime_pair.py` uruchamia niezależne procesy z obu źródeł, odwraca kolejność co drugą parę, zapisuje wszystkie serie i bootstrap całych par uruchomień. Jedna konfiguracja nie stanowi odbioru całej macierzy. Przykład dla głównego scenariusza:

```sh
python tools/benchmark_full_runtime_pair.py --baseline-root /path/to/216 \
  --seconds 180 --pairs 5 --active 3 --configured 20 --mode shadow \
  --candidates --ui --output /path/to/result.json
```

Strumień ma ustaloną liczbę kroków 20 Hz, naprzemienne zmiany obecności każdego aktywnego agenta oraz sygnał numeryczny. Para musi mieć identyczny hash kolejności i wartości wejść oraz tego samego narzędzia. Raport pokazuje spóźnienia generatora. UI odtwarza niezależne odczyty status/agents/events/candidates co 4 s, reaktywne live z rewizją i 100 ms przerwy oraz candidate-live co 100 ms. Jest to obciążenie rzeczywistych tras HTTP, bez kosztu DOM i przeglądarki/Ingress.

Okno pomiaru kończy się z podawaniem zdarzeń. Zakończenia podczas wygaszania klientów są osobno w `tail_window` i `tail_latency_samples`; `backlog_at_input_stop` ujawnia niezakończoną pracę. Przy przeciążeniu należy czytać także liczby ukończonych decyzji i ogon, nie sam p95 szybkiego podzbioru. `service_calls` obejmuje rozgrzewkę; liczba usług w oknie jest widoczna jako count metryki `measured_window.ha_service_request`. Samo `control_dispatch_exercised` nie dowodzi wykonywania usług w okresie pomiarowym.

Modele są przygotowaną fixture kwalifikacji, z 80 zasileniami/validacjami głów i ustawionym progiem decyzji agenta 0,5. Zachowane są rzeczywiste pozostałe kontrole Executor. Limitów TTL, support/novelty, świeżości, takeover ani dysku nie poluzowano. Wątki BLAS mają te same limity środowiska co Dockerfile.

## Odbiór i kolejne etapy

Wyniki testów i porównań są w `docs/benchmarks/PERFORMANCE_PLAN_0_14_217_*.json`. Granice dowodów: modele i strumień są syntetyczne, transport HA lokalny, start dotyczy przygotowanej bazy fixture. Nie ma pomiaru fizycznego ACK, pełnego startu na kopii bazy 10 mln rekordów ani pomiaru docelowego Raspberry Pi.

Końcowe wyniki pełnej regresji i pięciu naprzemiennych par są aktualizowane w `VALIDATION`, `SOURCE_HASHES` i `FINAL_*`. Pliki prób pośrednich nie są odbiorem końcowego kodu. Zawsze należy porównać hash źródeł, czas próby, liczbę zakończeń, wiek kolejki, luki i błędy UI.

Oddzielny pracownik Candidate Shadow został sprawdzony i wycofany z kompozycji: w długiej próbie z 20 Root pogorszył p95 oraz wiek kolejki. Końcowy kod używa dotychczasowego pracownika Candidate. Samo dodanie wątku nie usunęło współdzielenia Store i CPU. Zachowano FIFO, redukcję zbędnych odczytów oraz kontrolę generacji; pełna izolacja lifecycle pozostaje otwarta.

W 120-sekundowej próbie z 20 aktywnymi Root i otwartym UI końcowy wariant miał rosnące kolejki obserwacji i okien oraz luki nieprzyjętych metadanych decyzji. Usunięto timeouty listy Candidate, ale ten scenariusz nie spełnia warunku stabilnego backlogu. Krótsze porównanie p95 nie unieważnia tej obserwacji. Pakiet wymaga przeglądu przed wdrożeniem w takim obciążeniu.

Pakiet wykonuje E0/E1 oraz części E2/E3/E6. Cały plan E0–E7 nie jest zakończony. Pozostają: odłączone przyrostowe ramki i epoka mutacji modelu, pozostała praca po decyzji, zaakceptowana pomiarowo izolacja lifecycle Shadow, priorytety writerów i stabilność przy długim przeciążeniu, ocena IPC/CPU oraz start na dużej rzeczywistej bazie. E4 należy wdrażać warunkowo: bieżące pomiary nie wskazują blokady Home Context jako głównego ograniczenia. Większy limit RAM nie naprawia rosnącej stale kolejki; istniejące ograniczone kolejki i indeksy w RAM pozostają używane.

Control, OFF confirmation, świeżość danych, future-only pairing, checksum modeli oraz ograniczenia dysku/WAL/payload/RAM pozostają wymagane. Ten pakiet nie dowodzi skrócenia fizycznej reakcji ani prawie dziesięciominutowego startu instalacji użytkownika. Nie wymaga Full Rebuild.


## Końcowe wyniki pierwszego pakietu

Kod wykonawczy zamrożono w commicie GitHub `67b24a11fa84412b4fc24cbf9c045246d85a4305`. Manifest `SOURCE_HASHES` potwierdza 618 plików źródłowych, testów i narzędzi; późniejsze zmiany dotyczą wyłącznie raportów i metadanych. Poniżej p95 Root `event_to_decision`, przy działającym Candidate i UI. Nie jest to p95 decyzji Candidate.

| Scenariusz | 0.14.216 | 0.14.217 | Zmiana | Bootstrap 95% zmiany | Liczba decyzji 216 / 217 |
| --- | ---: | ---: | ---: | --- | ---: |
| 3 Root Shadow | 16,51 ms | 13,74 ms | -16,8% | -21,5% do -10,7% | 10707 / 10696 |
| 20 Root Shadow | 419,58 ms | 109,36 ms | -73,9% | -80,2% do -71,2% | 22275 / 34930 |
| 3 Root Control, ograniczona interpretacja | 490,49 ms | 75,92 ms | -84,5% | -96,0% do -56,6% | 247 / 8979 |

Każdy scenariusz ma pięć naprzemiennych par po 36 sekund po rozgrzewce. Porównywane pary mają identyczne hashe wejść i narzędzia. Bootstrap losuje całe pary. Odczyt Candidate-live co 100 ms obciąża obie wersje. Wszystkie serie są zachowane. Przy 3 Root Shadow kolejka Candidate na końcu wejścia jest pusta we wszystkich seriach i nie ma luk retencji. Przy 20 Root p95 szybszego Root nie oznacza stabilnej obsługi wszystkich Candidate: kolejka nadal ma 14 do 20 oczekujących rootów. W pięciu seriach 217 nie było błędów HTTP; 216 miała po trzy timeouty ciężkiej listy Candidate. Wiek roota nie był instrumentowany w 216, więc brak tej metryki nie oznacza zerowego oczekiwania.

Control wymaga osobnej interpretacji: 216 jest mocno zablokowana, kończy tylko 247 decyzji i nie wysyła usług. Każda seria 217 wysyła trzy usługi podczas rozgrzewki, lecz **właściwe okno pomiarowe obu wersji ma zero usług HA**. Wynik opisuje gotowość decyzji i blokowanie pipeline, bez dowodu poprawy ścieżki komenda → ACK. Nie jest odbiorem fizycznego sterowania.

Próba 120 sekund / 20 Root: Root p95 144,20 ms, 22409 decyzji; Candidate na końcu wejścia ma 20 oczekujących rootów i najstarszy czeka 20676 ms. Pozostają 1322 surowe obserwacje oraz pełna kolejka 4096 okien; 9659 nowych opcjonalnych okien retencji nie przyjęto, z jawnymi lukami. Zero błędów na 2207 żądaniach UI nie znosi tego przeciążenia. Ten scenariusz nadal nie spełnia odbioru.

Osobna próba tego samego źródła, pięć par po 18 sekund: debug trace OFF p95 13,69 ms, ON 16,14 ms (+17,9%); proces zużył o 14,2% więcej CPU. Dotyczy hosta i tego obciążenia. Szczegółowy trace ma mierzalny koszt, więc porównania wersji wykonano z trace OFF. CPU/RSS raportu dotyczą procesu runtime, bez pełnego rozliczenia jego procesów potomnych.

Pełna lokalna regresja kończy się `Ran 2651 tests ... OK` w 330,119 s. CI tego samego kodu: Python 3.11 2651 testów w 268,624 s, Python 3.13 2651 w 323,831 s, budowa obrazu i smoke PASS: [przebieg CI](https://github.com/mpuchara/HomeMind-HA/actions/runs/39148328833). Entry point, oba testy VM interfejsu, compileall i składnia 21 plików JS przechodzą. Pierwszy lokalny przebieg miał jeden timeout trzysekundowego join writera w istniejącym teście 10 MiB. Zachowano jego log i hash; sześć naprzemiennych powtórzeń modułu i pełny ponowny przebieg przechodzą bez zmiany testu ani timeoutu. Przyczyna pierwszego timeoutu nie została dowiedziona. Szczegóły: `QA_RETRY`.

`ACCEPTANCE` zestawia wyniki i otwarte bramki. Raporty `FINAL_*`, `LONG_CURRENT_20` i `TRACE_OVERHEAD` publikują wszystkie serie, rozkłady, liczniki i hashe. Wyłącznie duże tablice pojedynczych próbek są zachowane w pełnych lokalnych JSON w `tmp/performance217-final-raw`; każde opublikowane zestawienie zawiera ich ścieżkę, rozmiar i SHA-256. Próby pośrednie i stary profil 1 s są odrębne i nie stanowią podstawy tabeli.

Status pozostaje draft. Następny priorytet to izolacja lifecycle/obserwatorów od oceny Candidate oraz wydajność writerów przy długim obciążeniu. Dopiero stabilna kolejka, brak luk i komendy w oknie pomiaru pozwolą zamknąć odpowiednie bramki; większy bufor RAM sam nie rozwiązuje stałego niedoboru przepustowości.
