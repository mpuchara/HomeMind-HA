# Adaptive AI 0.14.228

## Zmiany

Przy fizycznej zmianie celu porównanie Parent/Candidate zapisywało parę do SQLite przed wywołaniem Live. W pełnej kompozycji aplikacji ten etap przyjmuje teraz dokładny wynik do ograniczonej kolejki RAM (128 wyników / 4 MiB). Istniejący writer Candidate zapisuje parę i liczniki porównania w jednej transakcji. EpisodeEvaluator dostaje konkretną zapisaną parę, zamiast najnowszego wyniku dla agenta. Retry nie nalicza pary dwukrotnie, a kontrola dat utworzenia generacji odrzuca wyniki zastąpionego modelu. Przepełnienie pozostaje jawną luką dowodową. Odczyt statusu/promocja uwzględnia oczekujące wyniki.

Timeout 5000 ms podczas odczytu statusu eksportu nie kończy już obserwowania działającego zadania. UI ponawia tylko GET tego samego job_id, także po HTTP 429/502/503/504. Kolejne kliknięcie po utracie połączenia w tej samej stronie wznawia odczyt istniejącego zadania. POST nie jest automatycznie ponawiany. Oczekiwanie ma limit 15 minut; po restarcie/wygaśnięciu zadania HTTP 404 pozostaje widocznym błędem.

Eksport asynchroniczny korzysta z własnego połączenia SQLite mode=ro/query_only, z ponownym użyciem stron i przygotowanej konfiguracji. Nie wykonuje konfiguracji dziennika ani migracji przy każdym drobnym odczycie. Historyczne okna surowych danych korzystają z limitowanych skanów po encji i połączenia wyników w RAM. Budżet wykonywania zapytań diagnostycznych wynosi 120 s; progress handler nie przerywa blokady I/O w systemie operacyjnym.

Zwykły odczyt etykiet Correct wylicza brakujące identyfikatory nadzorowania w odłączonej odpowiedzi. Nie uruchamia migracji pod Store.lock. Zapis etykiet i kopiowanie Candidate zachowują swoją obsługę trwałych danych.

Po rozpoznaniu trwałego uszkodzenia raw audit writer tła pomija ten zapis i nie budzi się natychmiast ponownie z powodu zachowanej uszkodzonej partii. Jawny flush nadal zgłasza błąd. Pozostałe kolejki są obsługiwane niezależnie. Zachowane 1108 zdarzeń z eksportu użytkownika nie jest usuwane ani uznawane za zapisane.

## RAM

Domyślny limit bufora wierszy kontekstu rośnie z 16 do 64 MiB na bufor. Rzeczywisty limit wynosi najwyżej 1% całej i 5% dostępnej pamięci po rezerwie 256 MiB. Przy nieznanej pamięci pozostaje najwyżej 16 MiB. Termin flush 30 s i transakcje 64 wiersze / 1 MiB pozostają zachowane.

Domyślny limit odłączonych obserwacji Tournament rośnie z 64 do 256 MiB. Nadal obowiązują limity 5% całej pamięci, 25% dostępnej pamięci po rezerwie 128 MiB, liczby zadań i jawnych luk przy odrzuceniu. Przy nieznanym headroom limit wynosi najwyżej 64 MiB. Migracja obejmuje poprzednie wartości domyślne; inne własne limity są zachowane. To limity wzrostu według potrzeb, a nie rezerwacja RAM ani gwarancja przyspieszenia obliczeń.

## Diagnoza z plików użytkownika

Oba eksporty 23.10.2026, 12:12:29 i 12:13:00, pochodzą z 0.14.227. Pierwszy pokazuje RSS około 342 MiB. P95 event_to_decision w ostatnim oknie wynosi odpowiednio 1162 i 1048 ms. W drugim p95 context_rows_store_lock_wait sięga 7106 ms; context_shadow_persist trwa około 20149 ms, głównie poza CPU. Ślad pierwszego zawiera migrację Correct pod Store.lock. Te pomiary wskazują rywalizację o zapis/I/O; małe RSS samo w sobie nie jest dowodem na potrzebę zapełnienia RAM.

SQLITE_CORRUPT pozostaje aktywne. Nowe zmiany izolują kolejne koszty i nie naprawiają bazy. Nie ma dostępu do rzeczywistego pliku bazy ani docelowego Pi. Wymagane jest zabezpieczenie aktualnej kopii offline i ustalenie uszkodzonych obiektów według [instrukcji diagnozy bazy](DIAGNOZA_BAZY_0_14_227_PL.md). Rebuild modelu, zwiększenie limitów oraz restart nie naprawią SQLite. Oczekujące dowody RAM nie przeżywają restartu.

## Instalacja i sprawdzenie

Zachowaj dotychczasową tożsamość dodatku i katalog danych. Przed restartem zabezpiecz bazę zgodnie z instrukcją offline. Wgraj paczkę 0.14.228, przebuduj obraz dodatku, wykonaj Ctrl+F5 w panelu i sprawdź numer wersji. Rebuild modelu nie jest wymagany.

Sprawdź Export debug z Live i Candidate oraz świeży eksport runtime w Shadow i Control. Oceniaj event_to_decision osobno od service-dispatch i zgodnego state-ACK. Wynik kontrolowanego testu hosta usuwa zależność od mutexu, ale nie potwierdza opóźnienia urządzenia HA ani wydajności karty SD na Pi. Walidacja jest zapisana w raporcie QA tej wersji.

## Walidacja wydania

2797 testów lokalnych zakończyło się OK (Ubuntu WSL, Python 3.12.3, 320,059 s). [CI dokładnego kodu](https://github.com/mpuchara/HomeMind-HA/actions/runs/39940912430) zakończyło się sukcesem: Python 3.11, 2797 testów / 280,819 s; Python 3.13, 2797 testów / 256,761 s; dodatkowe benchmarki, build/start obrazu oraz spakowany trener wektorowy. Job paczkowania ograniczony do main jest pominięty; ZIP-y instalacyjne zostały utworzone i sprawdzone lokalnie.

Kod/testy/narzędzia odpowiadają commitowi a378fc2288d903c9206d66883bb35141705b4b47. Wszystkie 653 sumy z normalizacją CRLF do LF odpowiadają zamrożonej walidacji. Końcowy commit dodaje wyłącznie raporty i metadane. Sprawdzono też 22 pliki JavaScript, compileall, start rzeczywistego entrypointu, UI eksportu/Correct/Current-Desired oraz rzeczywisty eksport HTTP przy zajętych mutexie Store i writerze SQLite. Przykład eksportu HTTP nie zawierał etykiet Correct; rekonstrukcję historii i zgodność modeli obejmują testy regresyjne. Wyniki hosta nie są pomiarem docelowego Pi ani urządzeń HA.
