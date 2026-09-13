# Eksperymenty kontekstowe — 0.10.0

Na karcie każdego agenta przycisk **Eksperymenty** otwiera menu. Wybierz kierunek, zaznacz **Eksperymentowanie włączone** i zapisz. Agent musi mieć ukończony trening; fizyczne próby wykonuje tylko w Control. Sam zapis ustawień nie wysyła żadnej usługi HA. Zmiana kierunku zachowuje dotychczasowe wyniki każdego kierunku osobno.

## Kierunki

| Kierunek | Co agent sprawdza |
| --- | --- |
| Obecność | Czy małe wzmocnienie słabego sygnału radaru, PIR lub prognozy obecności uzasadnia wcześniejszą akcję. Nie próbuje zapalać światła bez żadnego sygnału obecności. Eksperyment ON/OFF dotyczy tylko wcześniejszego ON. |
| Otoczenie | Czy niewielka zmiana wyuczonej granicy zależnej od jasności, temperatury, wilgotności, pogody lub słońca poprawi zachowanie. Kierunek wynika z wag istniejącej polityki: np. wpływ niższej jasności może uzasadnić próbne ON przy nieco jaśniejszym otoczeniu. |
| Praca innych urządzeń | Przegląda dostępne urządzenia wykonawcze, preferuje pracujące oraz ten sam obszar, wybiera do 32 nazwanych cech aktywności. Uczy się, kiedy ich stan przemawia za próbą lub pozostaniem przy zwykłej decyzji. Klimatyzacja używa `hvac_action`, jeśli jest dostępne. |

Obecność i otoczenie korzystają z już wybranych wejść polityki; nie zmieniają doboru czujników ani schematu historycznego. Sterowniki innych urządzeń mogą być wejściami wyłącznie osobnego modelu eksperymentów. Własny cel, rodzeństwo tego samego urządzenia oraz encje konfiguracyjne/diagnostyczne są wykluczone. Brak metadanych urządzenia ogranicza możliwość wykrycia rodzeństwa. Wpływy są powiązaniami wyuczonymi z obserwacji, a nie dowodem przyczynowości.

## Wielkość i tempo prób

Domyślnie ustawiono siłę kontekstu 20%, przerwę 15 minut i limit 6 prób w ruchomych 24 godzinach. Próba porównawcza bez zmiany nastawy również zużywa limit. Liczy się także próba wysłania zakończona niepewnym błędem transportu. Budżet i przerwa nie resetują się po restarcie ani przełączeniu kierunku.

Siła kontekstu odnosi się do znormalizowanych cech, nie do procentów lux czy stopni Celsjusza. Limit nastawy podajesz w jednostkach urządzenia. Dla liczb działa także ograniczenie do 5% zakresu użytkownika i wszystkie dotychczasowe ograniczenia fizycznego urządzenia. Używane są kroki rozpoznawane przez urządzenie; mogą być drobniejsze niż historyczna siatka akcji. Limit mniejszy niż martwa strefa lub rozdzielczość urządzenia daje oczekiwanie zamiast pozornej próby. Nie zakładamy, że sąsiednie tekstowe opcje `select` mają fizyczną kolejność: taki agent ma menu i diagnostykę, ale bez automatycznych mikroprób.

Obserwacja trwa co najmniej 30 sekund domyślnie, licząc od ACK, i co najmniej tyle, ile stabilizacja danego urządzenia. Dla termostatu domyślnie jest to 900 sekund. Drobna próba utrzymuje się tylko przy niezmienionej zwykłej decyzji i kontekście. Istotna zmiana któregokolwiek obserwowanego wejścia, nowa decyzja lub ręczna korekta pozwala wrócić do zwykłego sterowania. Po zakończeniu próby polityka bazowa może przywrócić swoją nastawę. W domu trwa najwyżej jedna próba ze zmianą urządzenia naraz; inne urządzenia nadal działają normalnie.

## Jak działa online reinforcement learning

To kontekstowy bandyta z opóźnioną informacją zwrotną, nie pełny model dynamiki domu. Model historyczny wyznacza normalną akcję. Eksperyment jest dopuszczany jedynie w pobliżu wyuczonej granicy: mała zmiana wybranej grupy cech musi wystarczać do zamknięcia różnicy ocen sąsiednich akcji. Dla nowych cech urządzeń dopuszczana jest mała różnica ocen zależna od ustawionej siły.

Osobny model utrzymuje trzy warianty: zwykła decyzja, mała zmiana w górę, mała zmiana w dół (tylko sensowne i legalne warianty). Wyniki aktualizują rzadkie wagi diagonalnego LinUCB; wyuczona ocena i niepewność wpływają na następny wybór w podobnym kontekście. Co najmniej 25% kwalifikujących się wyborów przypada na porównanie bez zmiany; model może częściej wybierać zwykłą decyzję, jeśli próby wypadają gorzej. Są to przeplatane porównania adaptacyjne, nie statystyczny dowód przewagi A/B.

| Wynik obserwacji | Sygnał |
| --- | --- |
| Potwierdzona ręczna korekta | −1 i minimum godzinę przerwy od kolejnych eksperymentów. Szybki profil zwykłego sterowania pozostaje bez długiej blokady. |
| Potwierdzona obecność po wcześniejszym ON | +0,6; pozostanie OFF do potwierdzenia obecności: −0,2. Dotyczy celów ON/OFF. |
| Nastawa obserwowana bez korekty przez pełne okno | Bardzo słaby sygnał: +0,02 dla próby, +0,05 dla decyzji bazowej. Niższa nagroda próby uwzględnia koszt dodatkowej zmiany. |
| Sam ACK | Brak nagrody. |
| Brak ACK, restart, brak danych, niejednoznaczna zmiana zewnętrzna, przerwana obserwacja | Bez etykiety i bez aktualizacji wag. |

Ręczna korekta musi być rozpoznawalna w kontekście HA jako działanie użytkownika. Nie każdy fizyczny przycisk przekazuje `user_id`; niejednoznaczne zmiany nie są wymyślanymi etykietami człowieka. Brak sprzeciwu również nie dowodzi poprawności — dlatego ma małą wagę. Model uczy się preferencji nastaw, nie gwarantuje optymalizacji zużycia energii ani komfortu.

Próby są oznaczone na karcie i w zdarzeniach `experiment_started` / `experiment_outcome`. Pewność na karcie podczas próby dotyczy bazowej predykcji, nie jest skalibrowaną pewnością eksperymentalnej akcji. W menu znajdziesz liczby zakończonych obserwacji, ostatnią nagrodę i wpływy innych urządzeń.

Po wyłączeniu eksperymentów wiedza pozostaje w bazie, ale nie zmienia decyzji agenta. Ponowne włączenie korzysta z zachowanych wyników. Dane są zapisane w istniejącej bazie aplikacji; nie trzeba pobierać całego archiwum ani ponownie trenować agentów 0.9.2.
