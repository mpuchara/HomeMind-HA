# HomeMind 0.14.167

Trening światła rozróżnia obecność i potrzebę oświetlenia. Zajęta kuchnia przy dobrym świetle dziennym może prawidłowo pozostawać OFF. Łazienka z automatyzacją bez warunku jasności zachowuje dotychczasowe uczenie obecności i nieruchomego pobytu.

## Znalezione problemy

Korekta jakości historycznych przykładów karała OFF przy lokalnej obecności bez sprawdzania jasności. Odtwarzanie OFF mogło również zakończyć poprawny dzienny odcinek po samym przekroczeniu progu radaru. Kanał `sensor.*_light` LD2410 nie trafiał do pakietu radaru, a normalizacja surowego odczytu szybko nasycała się, tracąc rozdzielczość.

Pełny test treningu ujawnił dodatkowo brak historii stanu żarówki w kontekście odtwarzanym dla fotometrii. Stan celu pozostaje wykluczony z wejść modelu, lecz jego chronologia jest potrzebna do odtworzenia jasności sprzed zapalenia. Bez niej historyczna jasność stawała się „unknown”. Nowy kontrakt rekonstruuje tę zależność i uwzględnia ją w kluczu cache cech.

## Zmiany

- Zachowujemy strukturę warunków ON: AND/OR/NOT, granice strict above/below, atrybut i próg wskazany przez inną encję. Nie wykonujemy szablonów. Nieczytelne, złożone akcje i nieznane odczyty zachowują niepewność.
- Obecność przy niespełnionym warunku jasności lub nieznanej jasności nie zmienia historycznego OFF w ujemną etykietę. Automatyzacja bez ograniczenia jasności nadal pozwala wykrywać przedwczesne OFF przy obecności. Ręczne polecenia zachowują pierwszeństwo.
- Przyspieszanie ON przez RoomBelief uwzględnia warunek jasności; główna decyzja modelu nadal wynika z nauki. Warunki automatyzacji nie przyznają Control i nie generują samodzielnie etykiet ON.
- Dzienny OFF nie jest skracany wyłącznie z powodu wejścia. ON nie jest cofany do momentu, w którym znany warunek jasności nie był spełniony.
- Surowy pomiar światła LD2410 jest osobnym kanałem oświetlenia, a nie dowodem obecności. Pakiet wybiera lokalny radar przez device_id albo ścisłą rodzinę identyfikatora.
- Nowy kontrakt cech 4 kompresuje odczyt światła i wykorzystuje jasność sprzed zapalenia podczas ON. Nie interpretuje światła samej żarówki jako zniknięcia potrzeby oświetlenia. Brak pewnego odczytu daje brak cechy, nie sztuczną ciemność.

LD2410 publikuje w engineering mode surowe `light` 0–255; poza tym trybem odczyt jest unknown. To inna skala niż kalibrowane luksometry: [dokumentacja ESPHome](https://esphome.io/components/sensor/ld2410/).

## Sprawdzenie

12 regresji warunków, jasności, wyboru lokalnych sensorów, priorytetu ręcznego, ON assist, starego kontraktu i fotometrii. Osobny test uruchamia rzeczywisty izolowany HistoryManager, zapisuje model i sprawdza pięć scenariuszy bez twardego nadpisania decyzji: pusto/ciemno OFF, obecność/ciemno ON, pusto/jasno OFF, obecność/jasno OFF, nieruchomy pobyt ze światłem emitowanym ON. Trening zachowuje 59 dodatnich przykładów occupied_daylight_off i nie nadaje im kary premature_off_confirmed_presence. Benchmark używa syntetycznych danych; nie jest potwierdzeniem wyniku w konkretnej kuchni ani przewagi nad jej automatyzacją.

Pełny lokalny zestaw 1990 testów przechodzi na Linux. Regresje i CI obejmują także nieruchomą obecność, krótkie poprawne wizyty, trajektorię, kwalifikację, manual feedback i wydajność Recorder.

## Instalacja i ograniczenia

Zainstaluj addon-root ZIP 0.14.167, uruchom ponownie dodatek i wykonaj Rebuild agenta kuchni. Stare modele zachowują kontrakty 1–3 i dotychczasowe współczynniki; aktualizacja nie przepisuje ich w miejscu. Łazienka może nadal używać istniejącego modelu. Nowe generacje używają kontraktu 4.

Nie znamy YAML ani identyfikatora czujnika konkretnej kuchni z samego wykresu. Odczyt automatyzacji HA musi dostarczyć rozpoznawalny warunek, a Recorder historię sensora. Szablony, branched/device actions i nieznane odczyty nie są interpretowane jako potwierdzona ciemność. Progi wskazane encją wymagają jej stanu w odtwarzanej historii; brak nie tworzy fikcyjnej wartości. Wbudowana w radar funkcja OUT/light_function nie jest automatycznie traktowana jako warunek HA. Nie obiecujemy wygaszenia światła dziennego, które narasta podczas ON: bez oddzielnego pomiaru ambient odczyt może zawierać światło lampy.
