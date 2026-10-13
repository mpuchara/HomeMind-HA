# 0.14.190: stabilne kolejne generacje Correct

Eksport .189 pokazuje poprawnie skopiowany model Gen 1, a więc bez błędnego Full Rebuild naprawionego w .189. Gen 2 użył ośmiu unikalnych punktów ON, z których tylko jeden był nowy. W nowym punkcie stationary energy wynosiło 12, podczas gdy poprzednie przykłady obecności miały około 100. Liczba dopasowań klasyfikatora wzrosła ze 122 do 138: osiem punktów ON i osiem historycznych kotwic OFF wywołało po jednym pełnym dopasowaniu. Każde z nich odtwarzało normalizację i wagi od zera.

Gen 2 zaliczył poprzednią bramkę na 1462 historycznych wektorach, z poprawnością OFF=100%. W późniejszych 18 wspólnych obserwacjach rodzic miał 9/9 poprawnych OFF, a Gen 2 0/9. To potwierdza rozjazd poza użytym benchmarkiem historycznym. Wektory z Recorder i bieżącej obserwacji różnią się także wiekiem komunikacji i zdarzeń. Eksport zawiera podsumowania modeli, a nie ich pełne wagi; nie umożliwia dokładnego odtworzenia polityki użytkownika. Ponowne globalne dopasowania oraz brak zabezpieczenia ostatnich poprawnych obserwacji są potwierdzonymi słabymi punktami ścieżki Correct.

## Zmiana

Dla gotowego klasyfikatora binarnego Correct zachowuje normalizację i rozpoczyna od współczynników rodzica. Wcześniejsze dopasowane punkty pozostają ograniczeniami stabilności. Aktualizacja obejmuje tylko nierozwiązane punkty, a nie ponowne wzmacnianie całej odziedziczonej listy. Nie dodaje globalnych kar do ramienia OFF.

Oprócz historycznych kotwic chronione są różnorodne poprawne decyzje historyczne oraz do 24 ostatnich zgodnych obserwacji rodzica i Current na klasę, wybranych z maksymalnie 512 wierszy w ostatnich sześciu godzinach. Cechy rekonstruuje jeden tracker zgodny z aktywnym kontraktem obserwacji. Niepełna rekonstrukcja, brak Current, rozbieżność rodzica z Current i okolice punktów Correct nie dostarczają etykiet stabilizujących. Po rekonstrukcji rodzic musi ponownie potwierdzić tę decyzję.

Aktualizacja jest ograniczona do 240 kroków i zachowuje budżet pracy treningu. Wagi trafiają do kandydata tylko wtedy, gdy korekty uzyskają wymagany margines, a chronione decyzje pozostaną poprawne. W przeciwnym razie odtwarzany jest klasyfikator rodzica, a nierozwiązane Correct blokują bramkę. Nadal możliwa jest dotychczasowa analiza brakującego kontekstu. Te próbki są zabezpieczeniem stabilności, nie niezależnym pomiarem jakości.

Raport publikuje nazwę optymalizatora, liczbę nierozwiązanych i wcześniej dopasowanych punktów, liczbę chronionych obserwacji oraz rzeczywiste dopasowanie przed aktualizacją. Dawny etap naprawy marginesu nie nadpisuje już wyniku sprzed tej aktualizacji.

## Weryfikacja

Test rzeczywistego klasyfikatora stosuje dwie kolejne generacje korekt przy coraz słabszym sygnale radaru, z kontekstem wieku komunikacji odmiennym od treningu bazowego. Obie naprawiają wskazane ON i zachowują OFF, normalizację i kary rodzica. Zapis i odczyt modelu zachowuje te decyzje. Kolejna generacja zawierająca same dopasowane punkty nie zmienia parametrów ani próbek klasyfikatora. Sprzeczna korekta przywraca cały poprzedni stan. Sprawdzane są też ograniczenia liczby ostatnich obserwacji i odrzucanie niepełnej rekonstrukcji.

Dotychczasowy test całej ścieżki Correct naprawia pięć punktów ON, zachowuje pięć kontekstów OFF i 24 historyczne decyzje. Model rodzica pozostaje identyczny, a wywołań Full Rebuild jest zero. Są to testy syntetyczne; wynik łazienki po instalacji wymaga obserwacji w Shadow.

Pełny zestaw 2272/2272 testów przeszedł w WSL bez pominięć, wraz z dziewięcioma nowymi regresjami. Sprawdzono kompilację modułów. [Raport weryfikacji](benchmarks/CORRECT_STABILITY_0_14_190_VALIDATION.json).

## Zastosowanie

Zainstaluj .190 i zrestartuj dodatek. Nie używaj Discard: obecna implementacja tego przycisku usuwa cały cykl, także Gen 1.

Podczas startu porównywany, niezatwierdzony kandydat starego optymalizatora wraca do Correct tylko wtedy, gdy obie klasy mają co najmniej osiem wspólnych obserwacji, rodzic poprawnie rozpoznaje wszystkie przypadki utraconej klasy, kandydat nie rozpoznaje żadnego, a w drugiej klasie kandydat rozpoznaje wszystkie. Ten warunek spełnia przesłana Gen 2 (OFF 0/9 wobec 9/9 rodzica, ON 9/9). Aktualny model rodzica musi przejść sprawdzenie schematu i sumy kontrolnej. Aktywnego zadania ani zatwierdzonego kandydata nie przerywamy.

Zachowane są ID Gen 2, Gen 1 i zapisane punkty. Dawna zgoda offline Gen 2 zostaje unieważniona przed wyborem bazy, żeby jej uszkodzone wagi nie zostały ponownie wybrane jako najnowszy dopuszczony model. Dotychczasowy worker kopiuje stabilnego rodzica i wykonuje nową korektę. Ponowny restart nie kolejkuje tej samej operacji. Gen 1 nie wymaga Full Rebuild. Wynik nadal podlega nowej bramce i porównaniu Shadow.
