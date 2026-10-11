# Adaptive AI 0.14.181

Domyślna ochrona dysku dopasowuje się do instalacji. Nie zatrzymuje treningu tylko dlatego, że baza przekroczyła dawny próg około 1,8 GiB przy dużej ilości wolnego miejsca. Poprawiony jest również start runtime, który w 0.14.180 mógł zakończyć się błędem w kroku 2/7 podczas unieważniania obserwacji przerwanych restartem.

## Automatyczny budżet

W konfiguracji dodatku w Home Assistant:

```yaml
storage_budget_mode: auto
storage_auto_use_percent: 50
```

Domyślnie aplikacja może wykorzystać 50% puli dostępnej na swoim systemie plików. Procent można zmienić w zakresie 10–80. Pula to aktualne wolne miejsce powiększone o fizyczny rozmiar istniejącej bazy i WAL. Dane innych dodatków nie są doliczane. Dzięki temu baza nie dostaje coraz większego budżetu przy kolejnych restartach: jej wzrost zmniejsza wolne miejsce, a suma pozostaje taka sama. Zmiany zajętości przez inne aplikacje mogą zmienić budżet wyliczony po restarcie.

95% przydziału stanowi limit bazy, a 5% limit WAL. Nieprzydzielona część puli jest rezerwą wolnego miejsca. Wyliczenie pozostaje stałe przez czas działania procesu, więc wzrost innych dodatków nie obniża wtedy chronionej rezerwy. Limity są pułapami, a nie rezerwacją ani poleceniem zapełnienia dysku. Panel Storage pokazuje tryb, procent i wynikowe pojemności.

W trybie `auto` zapisane wcześniej wartości `storage_database_max_mb`, `storage_wal_max_mb` i `storage_min_free_mb` nie sterują ochroną. Po aktualizacji stare domyślne 2048/128/2048 MiB nie przywrócą sztywnego limitu. Aby świadomie używać własnych wartości w MiB, wybierz `storage_budget_mode: manual`. Zmiany konfiguracji wymagają restartu dodatku.

## Ochrona i start

- Zapis nowych danych historii i trening nadal wstrzymują się przy 90% użytych stron bazy, połowie limitu WAL albo naruszeniu rezerwy wolnego miejsca. Twardy limit stron SQLite, całego WAL oraz kontrola przestrzeni przed commit pozostają aktywne. Nieznana pojemność oznacza blokadę zapisu.
- Przerwane obserwacje automatycznych nagród są unieważniane porcjami najwyżej 128 aktualizacji na transakcję, również przy miękkiej pauzie historii. Nie zamieniamy ich w udane nagrody. Ten wyjątek dopuszcza aktualizację istniejących obserwacji podczas odzyskiwania; nie dopuszcza nowych rekordów i nie omija twardej ochrony dysku.
- Workery treningu i offline RL dziedziczą wyliczony budżet rodzica, więc ich późniejsze uruchomienie nie obniża chronionej rezerwy. Pliki komunikacji workerów korzystają z tej samej rezerwy. Ich małe limity techniczne i ograniczenie logów pozostają, żeby błędny worker nie produkował nieograniczonych plików.
- Szybszy import z 0.14.180, odzyskiwanie WAL, wykorzystanie ukończonych porcji Recorder i retencja historii do 10 dni pozostają. Modele oraz ręczne preferencje nie są usuwane.

W 0.14.147 nie było kontroli pojemności, która powoduje obecne pauzy. Przywrócenie nieograniczonego wzrostu ponownie groziłoby zapełnieniem karty. To wydanie zachowuje szybki zapis i dodaje budżet zależny od rzeczywistej instalacji.

Po aktualizacji wystarczy uruchomić dodatek i wznowić trening. Nie ma potrzeby kasowania bazy ani wykonywania Full Rebuild z powodu tej poprawki. Wyliczenie opiera się na wolnym miejscu widocznym dla dodatku, a nie na nominalnej pojemności karty czy innym widoku pamięci.

## Weryfikacja

18 nowych regresji obejmuje różne pojemności, dane ze zrzutu 0.14.180, procentowy przydział, restart bez zwiększania limitu, wpływ innych dodatków, migrację starych wartości, tryb ręczny, niedostępny pomiar, rezerwę plików workerów, unieważnianie nagród w małych transakcjach i uruchomienie rzeczywistego końcowego entrypointu przy miękkiej pauzie bazy. Istniejące regresje ochrony i wydajności importu nadal obowiązują.

Wynik testów komponentu nie gwarantuje czasu pełnego treningu na konkretnej karcie SD. Potwierdzenie czasu treningu i zachowania rezerwy w instalacji wymaga nowego eksportu runtime po aktualizacji.
