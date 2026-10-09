# HomeMind Adaptive AI 0.14.160

## Jeden właściciel przycisków

Starsze akcje Shadow → Control, Wrong decision i Teach zostały usunięte z szablonu P0. Zniknęło podpinanie ich handlerów i odczytywanie/aktualizowanie starego przełącznika przy każdym odświeżeniu. Renderer app.js także zostawia pusty slot akcji. Aktualne przyciski Train lub Pause Shadow, Resume training, Autonomous, Correct i pozostałe akcje generacji tworzy wyłącznie workflow, zgodnie ze stanem agenta. Zachowane funkcje backendu i używane przez współczesne dialogi mechanizmy korekt nie są usuwane.

Poprzednia bariera DOMContentLoaded z 0.14.159 rozwiązywała kolejność ładowania, lecz nie dostępność plików na starcie serwera. HTTP startuje przed inicjalizacją Engine. Trasy feature scripts były rejestrowane dopiero podczas końcowej kompozycji runtime. Przed nią żądanie workflow mogło trafić w bramkę not-ready i skrypt nie był instalowany. DOMContentLoaded następuje także po nieudanym pobraniu, więc sama bariera nie wystarczała. Test przeglądarkowy poprzedniego wydania używał zastępczego serwera statycznego, który ukrywał ten problem.

Teraz główny Handler udostępnia wszystkie 21 skryptów wymienionych w index.html przed bramką runtime. Lista zawiera dokładne nazwy; nie ma dostępu do dowolnych plików lub ścieżek. Kontrola trusted client pozostaje przed odczytem, a API nadal wymaga właściwej gotowości backendu. Bariera UI z 0.14.159 i interwały pollingu pozostają. Karta nie ma już starszych akcji zastępczych.

## Walidacja

5 nowych regresji sprawdza wszystkie skrypty indeksu przy runtime not-ready, zgodność katalogu, trusted-client, odrzucanie nieznanych plików i path traversal, a także rzeczywisty uruchomiony trial_queue_main.py z celowo zatrzymaną inicjalizacją na prywatnym porcie. Health pozostaje not-ready, ale każdy skrypt zwraca HTTP 200, odpowiedni MIME i dokładne bajty źródła. Wykonywany test P0 zakazuje nawet zapytań o starsze przyciski; utworzona karta nie ma buttonów przed dekoracją workflow. Zaktualizowane istniejące regresje sprawdzają jednego właściciela Settings i Train. Cały zestaw: 1901 testów. Pełna strona dodatkowo sprawdzona w przeglądarce z rzeczywistym serwerem plików dodatku podczas zatrzymanej inicjalizacji.

## Log 175331

Log adaptive-ai-runtime-debug-20261009-175331.json z 0.14.159 ma 63 pomiary decyzji. Recent CPU 20,337%, RSS 101,430 MiB; recent event → decision p95 111,393 ms, wrapped inference p95 569,391 ms, obserwacja Shadow p95 472,750 ms. Brak błędów SQLite/kolejek/workera, krytycznych lub aktywnych spanów. Pomiary wyglądają lepiej niż poprzednie okno, lecz obciążenia są różne; nie przypisujemy różnicy tylko ostatniej poprawce. Brak próbek classifier fit nie pozwala ocenić przyspieszenia uczenia.

## Instalacja

Paczki HomeMind-Adaptive-AI-0.14.160-addon-root.zip oraz repository.zip z SHA256 są w wydaniu GitHub. Aktualizacja i restart, bez Rebuild. To poprawka dostępności i renderowania UI; modele, trening v26 oraz decyzje agenta pozostają zgodne.
