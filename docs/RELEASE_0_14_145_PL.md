# Adaptive AI 0.14.145

Eksport 0.14.144 potwierdza poprawny wybór czterech kanałów ESPEN4 i gotowy klasyfikator, ale nie zawiera próbek z opisanego testu. Użytkownik potwierdził, że światło było potrzebne przez cały długi pobyt. Po sprostowaniu także krótki ON jest poprawny: był to krótki pobyt przy umywalce, żeby umyć ręce.

## Trening

Kotwica zmiany numerycznej uwzględnia czas odebrania próbki przez HA. Poprzednio kontekst dokładnie w czasie zdarzenia mógł jeszcze zawierać poprzednią wartość. Korekta dotyczy ON, OFF i wyboru przykładu spadku sygnału. Opóźnione starsze zdarzenie nie zastępuje nowszej już znanej wartości.

Trzy równomierne próbki podtrzymania mogły omijać każdy krótki zanik energii. Teraz dochodzi najwyżej jeden przykład takiego spadku wewnątrz zaakceptowanego ON. Wszystkie próbki danego pobytu i horyzontu dzielą łączną masę 1. Przy ON nie uczymy podtrzymania po ustalonym końcu pobytu. Historia pozostaje przyczynowa: kontekst próbki nie zawiera późniejszych obserwacji; późniejsze zakończenie służy wyłącznie ocenie demonstracji.

Nie dodajemy odrzucania ani dodatkowego osłabiania epizodu wyłącznie z powodu krótkiego czasu. Krótkie mycie rąk jest prawidłowym przykładem ON. Korekty błędnych automatyzacji nadal wymagają dowodu z kontekstu albo jawnej intencji; sama długość wizyty nie stanowi dowodu błędu. Rzadkie poprawne użycie przy innej odległości nie powinno znikać z treningu tylko dlatego, że większość pobytów trwa dłużej.

Radar będący dokładnym źródłem pary automatyzacji ON/OFF zachowuje niepewność także bez wpisu obszaru w rejestrze HA. Spadek numerycznej energii nie ucina wtedy uczenia zaobserwowanego ON przed rzeczywistym końcem pobytu. Nie oznacza to stwierdzenia obecności z dodatniej liczby. Znany konflikt obszarów nadal wyklucza takie dopasowanie. Regresja używa wildcard, starego schematu z obcym radarem oraz brakujących wpisów obszaru ESPEN4.

## Aktualność wykresu

GET api/live i api/candidate-live nadal współdzielą żądania w locie, ale nie buforują zakończonej odpowiedzi. Karty Live odświeżają się co około 0,5 sekundy plus czas obsługi. Correct domyślnie przesuwa bieżące okno i odświeża rzeczywiście zapisane decyzje co około sekundę. Kliknięcie punktu, zaznaczenie zakresu, zoom i edycja dat zatrzymują śledzenie; przycisk Na żywo wznawia je. Ukryta karta i zamknięty dialog nie pobierają historii. Nie ma nakładających się automatycznych odczytów ani odtwarzania Desired aktualną policy.

Current korzysta także z fizycznych zdarzeń w buforze archiwizacji. Odczyt robi migawkę przed i po SELECT, żeby nie zgubić zdarzenia podczas flush. Nie wymusza zapisu i nie blokuje wspólnego writera. To usuwa opóźnienie zależne od batchowania, oprócz dodatkowego opóźnienia cache i nieruchomego końca wykresu.

Eksport diagnostyczny ma recent_observations: ostatnie 10 minut wybranych sygnałów (maksymalnie 12 encji, 2048 najnowszych wierszy) oraz maksymalnie 256 rzeczywistych decyzji, także z bufora. Flagi truncated wskazują przekroczenie limitu. Eksport nie wymaga punktu Correct. Nie zbiera nowych danych w tle.

## Walidacja i granice wyniku

Rzeczywisty izolowany proces historycznego treningu i zapisany model przechodzą syntetyczne wejście, nieruchomy pobyt, umywalkę, wyjście oraz dwusekundowy spadek energii. Historia zawiera również rzadkie prawidłowe wizyty trwające 12 sekund przy innej odległości radaru. Test wymaga ON zarówno przy wejściu na taką wizytę, jak i podczas niej. Polecenie `python tools/benchmark_stationary_relay.py --noisy` sprawdza ten zestaw. To regresja syntetyczna, nie odtworzenie brakujących próbek z domu.

Osobny test komponentu pokazuje, że jawne OFF dla rzeczywiście błędnego dalekiego sygnału uczy rozróżnienia od prawdziwego wejścia i zachowuje ON podczas siedzenia. Nie stosujemy takiej etykiety do poprawnej krótkiej wizyty użytkownika. Historyczna zgodność z automatyzacją, nawet 100%, nie dowodzi przewagi nad nią. Szerszy benchmark pełnego produktu pozostaje osobnym kryterium i w poprzedniej wersji nie osiągał wymaganej jakości światła i fałszywych ON.

## Instalacja i dalszy test

Po instalacji 0.14.145 wykonaj pełny Rebuild i testuj w Shadow. Aktualizacja interfejsu działa po przeładowaniu strony; nowe reguły próbkowania wymagają nowego treningu. Powtórz długi nieruchomy pobyt i krótkie mycie rąk. Poprawny Desired powinien obejmować cały czas potrzeby światła, także krótką wizytę. Punkty Correct = OFF zapisuj tylko przy rzeczywiście zbędnym świetle, nie dla opisanego krótkiego ON. Wyeksportuj diagnostykę krótko po teście, żeby recent_observations obejmowało scenariusz.
