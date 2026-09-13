# 0.10.6 — naprawa startu aplikacji

Baza: świeżo pobrany main, 3050ec8 (Release Adaptive AI 0.10.5). Zachowano ręczne korekty 0.10.3–0.10.5, uczenie szerokiego kontekstu, adapter odkurzaczy oraz wcześniejsze eksperymenty i przejmowanie automatyzacji.

Przyczyną awarii było wywołanie install_manual_context_learning(core) na poziomie importu fast_queue_main.py. W tym momencie main.STORE było None. Tworzenie tabeli manual_context_feedback kończyło proces błędem AttributeError przed core.main(), więc panel HTTP nie mógł się uruchomić. Błąd odtworzono na niezmienionych źródłach 0.10.5 z pustym katalogiem danych.

Nowa kolejność to HTTP → baza → adaptery i rozszerzenia polityki → silnik i obserwatory → pracownicy. Adaptery nadal rejestrowane są przed importami funkcji przez engine/history/executor. Inicjalizacja tabel ręcznych korekt odbywa się po przypisaniu bazy. Szybki profil i obserwatory ręcznych zmian są instalowane przed pierwszym zdarzeniem, a obsługa błędów obejmuje również zewnętrzny wrapper kolejki. Wolny lub nieudany start nie usuwa dostępu do diagnostyki HTTP.

Numer wersji pochodzi z settings.py; adapter urządzeń nie nadpisuje go swoim numerem. Narzędzie paczkowania odczytuje aktualną wersję z BUILD_INFO, sprawdza zgodność config/settings oraz wszystkie pliki JS. CI po budowie obrazu uruchamia go bez sieci i bez danych HA, sprawdzając /health aż do gotowości.

Weryfikacja: 174 testy, w tym cztery nowe testy pełnego punktu wejścia w izolowanych procesach. Test HTTP odpowiada podczas celowo blokowanej inicjalizacji rozszerzeń i przechodzi do ready po jej zakończeniu. Sprawdzono także tabelę ręcznych korekt, obserwatory przed uruchomieniem pracowników, kolejkę i semantykę usług odkurzacza. [Pełny log](TEST_RESULTS_0_10_6.txt).

Podmień cały katalog adaptive_ai w tej samej lokalnej aplikacji i wykonaj Rebuild aplikacji. Modele, ustawienia, feedback i archiwum zostają zachowane; Train ani Rebuild modeli nie są potrzebne. [Instrukcja instalacji](INSTALLATION_PL.md).

Nie wykonano lokalnego builda Docker (daemon niedostępny), testu na urządzeniach użytkownika ani pomiaru Raspberry Pi. Test kontenera w CI jest nową kontrolą, nie deklaracją pomyślnego wdrożenia na HA.
