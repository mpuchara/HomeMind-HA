# Adaptive AI 0.14.142 — nieruchoma obecność z LD2410

Agent ma uczyć się całego poprawnego pobytu, także bez ruchu. Ta wersja
rozpoznaje binarne Still/Static/Stationary Target również bez device_class,
rezerwuje najważniejsze lokalne kanały radaru przed energiami poszczególnych
bramek i zachowuje rozróżnialne wartości odległości w skali pomieszczenia.

W historycznym treningu spadek ruchu nie skraca poprawnego okresu ON,
jeżeli radar nie potwierdził pełnej nieobecności. Still Target OFF nie jest
samodzielnym dowodem wyjścia: kanał ruchu tego samego urządzenia musi być
znany i wyłączony. Nieznany stan pozostaje niepewnością. Energia i odległość
pozostają cechami liczbowymi; nie stosujemy uniwersalnego progu energii.
Jawne decyzje mieszkańca zachowują wcześniejsze reguły pierwszeństwa.

## Instalacja i trening

Odśwież repozytorium dodatku w Home Assistant i zaktualizuj do 0.14.142.
Alternatywnie rozpakuj paczkę addon-root do /addons/adaptive_ai/, zachowując
dane istniejącego dodatku. Przed aktualizacją wykonaj kopię zapasową.

Włącz wymagane encje radaru i upewnij się, że ich historia jest dostępna.
Przypisz radar i światło do właściwego obszaru HA; identyfikator urządzenia
łączy kanały nieruchomego celu i ruchu. Wykonaj Train/Rebuild, a potem oceń
w Shadow przebieg: wejście, dłuższe siedzenie, ruch przy umywalce, wyjście.
Nie jest to bezwarunkowa blokada OFF w wykonawcy: zmiany dotyczą danych
wejściowych, doboru cech i etykiet treningowych. Samo zainstalowanie wersji
nie poprawia starego modelu bez ponownego treningu.

## Weryfikacja

Siedem nowych testów obejmuje nieruchomy cel bez klasy urządzenia,
podtrzymanie potrzeby podczas pobytu, potwierdzone wyjście, niedostępność
sensora, pomiary liczbowe, jednostki odległości i ograniczony wybór kanałów.
Wyników nie należy traktować jako pomiaru jakości na konkretnej instalacji.
