# HomeMind 0.14.208: Candidate Shadow przy zablokowanym zapisie

Eksporty z 16.10.2026, 11:38:32 i 11:39:01, pokazują `database_budget`,
a nie awarię obliczania modelu. Zajęte strony SQLite: około 9,028 GB, limit
bazy: 10,031 GB, próg pauzy: 90% limitu (około 9,028 GB). Na dysku jest około
12,1 GB wolnego miejsca. W trybie auto aplikacja ma 50% rozpoznanej puli;
pozostała część jest rezerwą. Retencja pracuje i usuwa stare wiersze, ale
poziom zajęcia w tych eksportach nadal oscyluje przy progu pauzy.

Bufor Candidate Shadow zawiera 128 obserwacji, około 77 kB, przy limitach
128 wpisów i 4 MiB. Kolejne zgłoszenia osiągnęły 9170 odrzuceń. `BufferError`
z obsługi tego limitu był raportowany jako `candidate_shadow_deferred_error`;
analogiczny błąd z pasywnych obserwacji trafiał do zdrowia workera. Zwiększenie
samego bufora opóźniłoby ponowne przepełnienie podczas długiej pauzy zapisu.

## Zmiana

- Pełny bufor wstrzymuje nowe diagnostyczne obliczenia Candidate, zarówno dla
  kontekstów od Live, jak i pasywnych eventów/heartbeatów. Live zachowuje swój
  istniejący tor sterowania. Candidate nadal nie wywołuje Executor ani usług HA.
- Istniejące, niemodyfikowalne obserwacje czekają na zapis w dotychczasowych
  limitach. Próby opróżniania respektują cooldown SQLite i ochrony pojemności.
  Po odzyskaniu miejsca zapisujemy dokładne wcześniejsze wyniki, a nowe
  inferencje korzystają wyłącznie z nowego faktycznie zaobserwowanego kontekstu.
- `EvidenceBufferFull` odróżnia wyczerpany budżet kolejki od błędu modelu.
  Pozostałe błędy, w tym obcy `BufferError`, pozostają widoczne. Również
  `StoragePressure` przy zapisie porównania/outcome daje lukę w dowodach,
  zamiast przerywać hook przed inferencją Live.
- Luka przerywa ciągłość `desired_since_ts` i bufora predykcji do porównania.
  Późniejszy outcome nie jest łączony z predykcją sprzed pominiętej obserwacji.
- Liczniki `admission.skipped`, `persistence_gaps`, przyczyna i stan wznowienia
  są w `engine.deferred_persistence.candidate_shadow` eksportu. Bieżący status
  `candidate_worker.shadow_observations` pokazuje pauzę i zajętość bufora;
  panel Runtime debug wyświetla jej przyczynę. Nie zapisujemy komunikatu błędu
  dla każdej pominiętej obserwacji do tej samej przeciążonej ścieżki.

## Wznowienie zapisu na zgłoszonej instalacji

Ta poprawka usuwa błędną klasyfikację, lawinę wyjątków oraz niepotrzebną pracę.
Nie zmienia wybranego przez użytkownika budżetu dysku ani retencji 14 dni.
Dopóki baza pozostaje przy progu ochronnym, nowe dane nadal mogą być lukami.

Przy wolnym miejscu pokazanym w eksportach można świadomie zwiększyć przydział
z 50% do 60% w konfiguracji dodatku i uruchomić go ponownie:

```yaml
storage_budget_mode: auto
storage_auto_use_percent: 60
```

To podnosi limit głównej bazy w tej puli do około 12 GB, pozostawiając około
40% puli jako rezerwę oraz osobny budżet WAL. W trybie auto ustawienia ręczne
`storage_database_max_mb` nie zmieniają limitu. Po restarcie sprawdź świeży
`sqlite.storage_budget.level` oraz czy `candidate_shadow.pending` maleje.
Nie ma potrzeby Full Rebuild ani kasowania bazy. Gdy instalacja ma inny stan
wolnego miejsca niż w dostarczonych eksportach, ocenę przydziału oprzyj na jej
aktualnym `sqlite.storage_budget`.
Bufory oczekujące są w RAM: restart nie odzyskuje niezapisanych obserwacji,
a istniejące luki historyczne pozostają lukami.

## Weryfikacja

Reprodukcja na dwóch niezależnych źródłach, 0.14.207 i 0.14.208, używa
prawdziwego SQLite, księgowania zajętych stron i produkcyjnego toru Candidate.
Dla 64 kontekstów przy pełnym buforze: wcześniej 64 wywołania Candidate i
32 błędy inferencji, po poprawce 0 wywołań i 0 błędów, 64 jawne luki.
W obu wersjach późniejszy zapis zachowuje dokładnie te same 128 obserwacji
(identyczny SHA-256 wierszy). Zero wywołań usług HA. Wyniki są reprodukcją
na hoście, nie pomiarem fizycznej latencji urządzeń ani Raspberry Pi.

Uruchomienie:

```bash
python tools/verify_shadow_backpressure.py --baseline-root ../HomeMind-HA-correct-lighting-20261016
python tools/verify_shadow_backpressure.py --one --check
python -m unittest discover -s tests -v
```

Wyniki źródłowe: `docs/benchmarks/SHADOW_STORAGE_0_14_208_DIAGNOSIS.json`
i `SHADOW_STORAGE_0_14_208_REPRODUCTION.json`. Końcowe kontrole i skróty źródeł
zawiera `SHADOW_STORAGE_0_14_208_VALIDATION.json`.

Końcowy wynik: 2512 testów Linux/Python 3.12.3 z NumPy 2.3.3, w tym
10 nowych regresji. Entrypoint 0.14.208: ready. Compileall, składnia wszystkich
plików JS oraz render pauzy/wznowienia w DOM: poprawne.
