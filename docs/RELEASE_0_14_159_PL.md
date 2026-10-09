# HomeMind Adaptive AI 0.14.159

## Aktualne przyciski od pierwszej karty

Przy otwieraniu Ingress karta pokazywała przez chwilę Confidence, Shadow → Control, Wrong decision i Teach, po czym zamieniała je na Decision strength, Pause Shadow, Correct, Create Candidate i pozostałe aktualne akcje. app.js oraz odczyty Live/Candidate uruchamiały się podczas parsowania HTML. Gdy późniejszy plik workflow jeszcze się pobierał, szybka odpowiedź API tworzyła kartę przez starszy renderer P0. Później workflow przebudowywał akcje.

Nowy ui_bootstrap.js instalowany przed pozostałymi warstwami odkłada start trzech pętli odczytu do DOMContentLoaded. Zdarzenie następuje po wykonaniu wszystkich klasycznych skryptów strony. Widoczność iframe ani ręczne odświeżenie przed gotowością nie omijają tej bariery. Start jest jednorazowy, także przy późniejszym window.load. Decision strength i average decision strength są użyte od razu w źródle kart i podsumowania.

To gotowość frontendu, niezależna od gotowości backendu. Po instalacji warstw Live nadal może pokazać Current/Desired i komplet akcji nawet podczas wolnego /api/status. Nie zmieniamy 500 ms pętli Live ani 4 s statusu/Candidate, timeoutów lub reguł backend-ready. Pierwsza hydratacja Candidate w chwilowo ukrytym Ingress pozostaje. Pierwsze karty czekają na pobranie skryptów; nie wymagają już późniejszej wymiany starszych przycisków.

## Nowy log wydajności

Log adaptive-ai-runtime-debug-20261009-165708.json pochodzi z 0.14.158 po około 290 s pracy. Nie zawiera krytycznych ani aktywnych spanów przy eksporcie, błędów SQLite/checkpointu/kolejek lub workera Candidate. Model encoding cache: 540 hits/7 misses/0 bypasses; Shadow JSON: 140 calls/140 hits/0 encodes/0 fallbacks. Checkpoint: 64 runs, 0 busy/error, wszystkie 5492 ramki skopiowane, remaining=0. Fizyczny WAL około 59 MiB nie dowodzi zaległości.

Recent event → decision p95 wynosi 185,770 ms, wrapped inference p95 1232,922 ms, obserwacja Shadow p95 882,192 ms. CPU recent nadal wysokie: 60,398%; RSS 103,527 MiB. W zachowanym oknie nie ma czterosekundowych przebiegów, ale też nie ma próbek binary_classifier_fit ani etapów treningu. Nie możemy z tego testu ocenić przyspieszenia dopasowania w 0.14.158 ani przypisać różnicy opóźnień samej poprawce. To wydanie usuwa wyłącznie problem startu UI; nie jest poprawką modelu lub CPU.

## Walidacja i instalacja

8 nowych regresji wykonuje rzeczywiste skrypty pollingu i workflow: opóźnioną instalację przycisków, szybkie i wolne API statusu, Live bez czekania na status, ukryty Ingress, stany loading/interactive/complete, fallback load, późną rejestrację i brak podwójnych timerów. Test indeksu pilnuje kolejności klasycznych skryptów. Cały zestaw: 1895 testów. Pełna strona dodatkowo sprawdzona w przeglądarce z celowo wstrzymanym skryptem workflow oraz szybkim i wolnym statusem.

Paczki HomeMind-Adaptive-AI-0.14.159-addon-root.zip oraz repository.zip z SHA256 są w wydaniu GitHub. Aktualizacja i restart, bez Rebuild. Trening, format modeli, v26, bramki kwalifikacji i sterowanie pozostają bez zmian.
