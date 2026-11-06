# Aktualizacja i odtworzenie — procedura przygotowawcza

**Status: BLOCKER. To nie jest zgoda ani rekomendacja instalacji obecnej gałęzi.**
Numer deweloperski 0.14.232 nie oznacza wydania. Nie opublikowano nowej paczki
instalacyjnej w ramach tej finalizacji. Procedura obowiązuje dopiero po zamknięciu
bramek wymienionych w `FINAL_RELEASE_READINESS_REPORT.md` i opublikowaniu paczki
powiązanej z dokładnym zweryfikowanym SHA.

## Przed aktualizacją

1. Zapisz wersję aplikacji, wersję HA Core, identyfikator dodatku oraz użyty obraz
   lub dokładne źródło poprzedniej wersji. Zachowaj poprzednią paczkę i jej sumę.
   Zachowaj opcje Supervisor bez tokenów w raportach lub repozytorium.
2. Zapisz liczbę i identyfikatory agentów, urządzenia docelowe, tryby
   Control/Shadow/Paused, numery modeli i generacji, aktywnych Candidate oraz
   zadania oczekujące/aktywne. W UI „Live” może oznaczać rodzica linii; nie
   utożsamiaj go automatycznie z trybem fizycznego sterowania `control`.
3. Zachowaj przykłady historycznego Current/Desired, istniejących Correct oraz
   cofniętych korekt. W kopii testowej porównaj identyfikatory, rekordy i sumy
   modeli. Nie uruchamiaj Rebuild jako czynności przygotowawczej.
4. Przygotuj spójną kopię **całych trwałych danych dodatku** oraz jego konfiguracji
   i poprzedniej aplikacji. Preferuj sprawdzone odtworzenie backupu Supervisor na
   środowisku testowym. Jeżeli wykonujesz kopię plikową: zatrzymaj dodatek i
   upewnij się, że procesy zapisujące zakończyły pracę. Nie kopiuj wyłącznie
   `adaptive_ai.db` podczas aktywnego WAL. Zachowaj kompletny katalog `/data`,
   pliki towarzyszące DB i artefakty modeli, nie tylko wybrane tabele.
5. Przetestuj nową wersję na **kopii** tych danych przy odciętym fizycznym
   dispatcherze HA. Zachowaj nienaruszony backup sprzed migracji. Test danych
   wygenerowanych kodem poprzedniej wersji jest dodatkową regresją, nie zastępuje
   testu kopii konkretnej instalacji.

## Instalacja dopiero po zamknięciu bramek

1. Sprawdź SHA-256 pobranej paczki, ZIP i `MANIFEST.sha256`; `SOURCE_COMMIT.txt`
   musi odpowiadać commitowi z raportu, pełnego CI i testu migracji. Nie używaj
   pliku nazwą podszywającego się pod nowe wydanie ani archiwum z niezatwierdzonymi
   zmianami roboczymi.
2. Aktualizuj **ten sam dodatek z tym samym slugiem i katalogiem danych**. Nie
   odinstalowuj go, nie usuwaj danych i nie zakładaj drugiej pustej instalacji.
   Nie zmieniaj ręcznie wersji w manifestach, aby wymusić aktualizację.
3. Wykonaj zwykły restart/aktualizację aplikacji. Nie naciskaj Train/Rebuild,
   Clear Learning ani Reset. Zachowaj dotychczasowe opcje; nowe przełączniki
   Universal pozostają domyślnie wyłączone. Nie włączaj kolektora jako obejścia
   niezamkniętej akceptacji.
4. Po starcie porównaj dane z inwentaryzacją: agenci i cele, tryby, modele,
   generacje, Candidate, korekty, Undo, historia i ustawienia. Odśwież UI oraz
   otwórz historię także po ponownym restarcie. Kontroluj błędy migracji,
   `needs_retrain`, stan kolejki i widoczność problemów storage/SQLite.
5. Jeżeli zgodny dotąd model wymaga Rebuild po zwykłym restarcie, agent zmienia
   tryb/urządzenie, znika historia albo występuje nieautoryzowana akcja Shadow:
   oznacz aktualizację **FAIL/BLOCKER**, zachowaj logi i kopię danych po awarii.
   Nie naprawiaj tego automatycznym resetem, ponownym uczeniem lub usuwaniem DB.

## Odtworzenie poprzedniej wersji

Zgodność starej aplikacji z nowszym schematem nie jest domyślną gwarancją.
Sam downgrade aplikacji nie jest zatwierdzonym rollbackiem. Nawet PASS dla
wygenerowanego fixture nie dowodzi zgodności każdej rzeczywistej bazy.

1. Zatrzymaj nową aplikację i wszystkie jej procesy zapisujące.
2. Zachowaj osobną spójną kopię stanu po awarii i logi do diagnostyki. Nie
   nadpisuj jedynego backupu sprzed aktualizacji.
3. Odtwórz razem **poprzednią aplikację, pełne dane sprzed migracji i opcje**,
   z tego samego sprawdzonego backupu. Nie łącz plików DB/WAL z różnych momentów
   i nie kopiuj pojedynczych tabel między wersjami.
4. Uruchom poprzednią wersję i porównaj inwentaryzację, modele, generacje,
   historię Correct/Undo i tryby. Jeśli odtworzenie nie odpowiada backupowi,
   pozostaw wynik FAIL i zachowaj dane; nie twórz pustych agentów w ich miejsce.

W ramach tego zadania nie wykonano aktualizacji, zatrzymania ani rollbacku
urządzenia użytkownika. Akceptacja na Pi/prawdziwym HA jest osobnym wynikiem.
