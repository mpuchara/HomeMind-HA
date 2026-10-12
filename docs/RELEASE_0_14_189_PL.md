# 0.14.189: Correct zachowuje wyuczony model rodzica

Eksporty łazienki z .188 pokazują pięć unikalnych punktów Correct, wszystkie z Desired=ON. Dziesięć aktywnych wierszy etykiet to po pięć na rodzicu i kandydacie, a nie dziesięć niezależnych głosów. Model rodzica ma schemat v12, kontrakt cech 4 i prawidłową sumę kontrolną. Kandydat `cd8a5181` trafił jednak na `schema_upgrade_rebuild`, a jego raport końcowy nie ma wyniku historycznego ani oceny dopasowania korekt. Pozostał `insufficient_evidence`; nie uzyskał zgody na promocję.

Zapisane stany radaru w punktach Correct wskazują stationary energy=100. W ostatnim pustym okresie wartość wynosiła około 3–4, a rodzic raportował OFF. Wykres przesłany przez użytkownika pokazuje wtedy stałe ON kandydata. Eksport nie zawiera pełnych macierzy modeli, więc nie umożliwia dokładnego odtworzenia obu zestawów wag i wskazania każdego kroku prowadzącego do stałego ON.

## Potwierdzona przyczyna

Końcowa kompozycja runtime importuje moduł Correct przed instalacją kontraktu obserwacji. Moduł zachowywał importowaną klasę `ExplicitFeatureSchema` v11, podczas gdy model i właściwy runtime używają v12. Test uruchomiony w nowym interpreterze odtworzył wynik: schemat modelu 12, wymagany schemat Correct 11, zgodność false. To uruchamiało pełną historyczną przebudowę ukrytego kandydata, zamiast zwykłej korekty kopii wyuczonego rodzica.

## Poprawka

Correct odczytuje klasę schematu z aktywnego modułu polityki w chwili sprawdzania modelu. Zgodny rodzic jest kopiowany, a istniejący optymalizator stosuje korekty wraz z poprawnymi historycznymi przykładami drugiej klasy. Pozostają bramki regresji i przyszłe porównanie A/B. Nie zmieniamy wag rodzica ani reguł automatyzacji łazienki.

Podczas startu dodatku niezaakceptowany kandydat z powodem `schema_upgrade_rebuild` wraca do kolejki Correct, jeśli rodzic ma zgodny model. Zachowane są ID, punkty i historia; bramki poprzedniej budowy są wyzerowane. Aktywnego zadania historycznego, odrzucanego dziecka ani zaakceptowanego kandydata nie przerywamy. Prawdziwie niezgodne i uszkodzone modele nadal nie przechodzą sprawdzenia.

Rozszerzenie kontekstu używa aktualnej klasy schematu i zachowuje kontrakt normalizacji rodzica. Po zmianie kolejności cech przenosi także klasyfikator Desired z jego próbkami, współczynniki odrzuceń i cały ogon obserwacji domu. Rekonstrukcja benchmarku i przykładów stabilizujących korzysta z trackera obserwacji zainstalowanego w runtime.

## Weryfikacja

Test rzeczywistego modelu binarnego uruchamia moduły w kolejności zimnego startu, trenuje rodzica na danych ON/OFF i stosuje pięć korekt ON. Błędne OFF w zaznaczonym kontekście staje się ON; pięć sprawdzanych pustych kontekstów nadal daje OFF. Wszystkie 24 historyczne przykłady ON/OFF pozostają poprawne, suma kontrolna rodzica pozostaje identyczna, a liczba wywołań pełnej przebudowy wynosi zero. To kontrolowana regresja na danych syntetycznych, a nie pomiar poprawności na urządzeniu użytkownika.

Pełny zestaw 2263/2263 testów przeszedł w Linuksie/WSL bez pominięć, wraz z 11 nowymi regresjami. Sprawdzono kompilację modułów.

[Raport weryfikacji](benchmarks/CORRECT_PARENT_0_14_189_VALIDATION.json).

## Zastosowanie

Zainstaluj .189 i zrestartuj dodatek. Opisany kandydat powinien automatycznie wrócić do kolejki Correct oraz zostać skorygowany z kopii rodzica z zachowaniem pięciu punktów. Model rodzica nie wymaga Full Rebuild. Sprawdź wynik w Shadow oraz raport bramki historycznej. Jeśli istniejący kandydat był już odrzucony, użyj Create Candidate na rodzicu; zachowane punkty Correct zostaną użyte ponownie.
