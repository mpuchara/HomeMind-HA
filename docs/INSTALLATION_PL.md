# Instalacja 0.10.6 z ZIP

ZIP zawiera kompletny katalog adaptive_ai oraz kod testów i dokumentację. To źródła aplikacji/dodatku HA, a nie integracja do custom_components.

## Podmiana istniejącej lokalnej aplikacji

1. Wykonaj kopię zapasową obecnej aplikacji wraz z danymi. Zapisz jej konfigurację.
2. Zatrzymaj Adaptive AI.
3. W istniejącym katalogu źródeł tej samej lokalnej aplikacji podmień cały katalog adaptive_ai na katalog z ZIP-a. Zachowaj dotychczasową lokalizację aplikacji i jej identyfikator. Nie wystarczy podmiana samego main.py: wersja 0.10 zawiera nowe moduły.
4. Nie kasuj prywatnego katalogu danych aplikacji. Baza i modele znajdują się w /data wewnątrz jej kontenera; nie jest to dowolny katalog /data innego dodatku.
5. Odśwież lokalne aplikacje w sklepie HA, wybierz istniejący Adaptive AI i wykonaj **Rebuild / Przebuduj**. Sam restart nie buduje nowego obrazu.
6. Uruchom aplikację i sprawdź numer 0.10.6 w panelu oraz logi migracji.
7. Modele z 0.8 wymagają ręcznego Train. Najpierw sprawdź Shadow, potem uruchom Control.

HA udostępnia lokalne źródła aplikacji w katalogu /addons, dostępnym przez odpowiednio skonfigurowany terminal lub Sambę; szczegóły opisuje [oficjalny samouczek lokalnej aplikacji](https://developers.home-assistant.io/docs/apps/tutorial/). Rebuild dotyczy lokalnie budowanych aplikacji: [Supervisor API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/).

## Jeśli obecna aplikacja pochodzi z repozytorium GitHub

Zainstalowanie ZIP-a jako nowej lokalnej aplikacji tworzy inny identyfikator i osobne dane. Nie przejmuje automatycznie bazy z wersji repozytoryjnej. Różnicę identyfikatorów local_slug i repository_slug opisuje [dokumentacja HA](https://developers.home-assistant.io/docs/apps/communication/).

Dla instalacji z repozytorium mpuchara/HomeMind-HA odśwież sklep aplikacji HA i zaktualizuj istniejącą aplikację do 0.10.6. Numer wersji źródeł sprawdzisz w adaptive_ai/config.yaml na gałęzi main. Zachowuje to identyfikator oraz prywatne dane aplikacji. ZIP służy również do podmiany źródeł lokalnej instalacji. Nie uruchamiaj dwóch instancji Control dla tych samych urządzeń.

## Po migracji

Migracja nie usuwa surowego archiwum, feedbacku ani konfiguracji agentów. Niekompatybilne modele zachowuje w model_backups i wstrzymuje ich sterowanie. Bootstrap wspólnego modelu domu jest osobnym, ręcznym zadaniem. Nie jest wymagany do działania bieżących czujników, ale dostarcza wcześniejszej wiedzy o przejściach.

Control wyłącza wykryte automatyzacje danego celu. Po opuszczeniu Control dziennik przejęcia przywraca wcześniej aktywne automatyzacje, które aplikacja sama wyłączyła. Wyłączenie samych eksperymentów pozostawia agenta w Control.

## Nowość 0.10.6 — eksperymenty

Modele z aktualnego 0.9.2 pozostają zgodne; nie trzeba robić Rebuild modelu ani Train tylko z powodu tej aktualizacji aplikacji. Eksperymenty są początkowo wyłączone. Na karcie agenta wybierz **Eksperymenty**, kierunek, opcjonalnie limity, zaznacz włączenie i zapisz. Próby wykonują się wyłącznie w Control. [Opis kierunków i uczenia online](EXPERIMENTS_PL.md).

## Poprawka startu 0.10.6

Przy aktualizacji z 0.10.2–0.10.5 także podmień cały katalog aplikacji i wykonaj Rebuild aplikacji. Kolejność inicjalizacji zmienia kilka modułów Pythona; podmiana samego HTML/JS nie wystarcza. Dane, modele i ręczne korekty pozostają zgodne. Nie trzeba uruchamiać Train ani Rebuild modeli agentów.
