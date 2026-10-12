# 0.14.186: dobór kontekstu przed treningiem światła

Rebuild szybkiego agenta ponownie ocenia kontekst, również gdy poprzedni schemat został zachowany w cache. Jasność nie zajmuje automatycznie miejsca w modelu tylko dlatego, że pochodzi z tego samego radaru. Kanały obecności, energii i odległości mają pierwszeństwo. Dodatkowy użyteczny kanał może wykorzystać dziewiąte miejsce istniejącego modelu o wymiarze 128.

Dotychczasowy ranking historycznych prekursorów i korelacji obecności/aktywności pozostaje używany. Dla niewyuczonego schematu z cache wykonuje się ponownie na ograniczonym zestawie: wejścia automatyzacji, lokalny radar, poprzednie wejścia oraz do dwóch wcześniej silnych sensorów zachowania. Kolejne fragmenty treningu i Resume zachowują już wyuczone kolumny. Rebuild rozpoczyna nową ocenę.

Ocena zależności od jasności porównuje wejścia obecności przy wyłączonej lampie. Używa wcześniejszego, dostępnego pomiaru jasności z okresu, gdy lampa również była wyłączona, i obserwuje włączenie w ciągu 30 sekund. Gdy jest dostępny `sun.sun`, jego stan jest oceniany podobnie. Wynik nie tworzy twardej reguły sterowania. Jasność może wejść do automatycznego schematu po wykazaniu silnego związku z akcją w obu chronologicznych połowach próby. Stały poziom jasności, zbyt mało danych lub brak wcześniejszego pomiaru oznaczają brak dowodu, a nie potwierdzenie braku okien.

Analiza wykorzystuje wyłącznie treningową część pierwszego fragmentu historii, przed wydzieloną częścią walidacyjną. Przechowuje najwyżej 256 obserwacji wejścia, a dodatkowy skan jasności kończy się po 65 536 wierszach. Opóźnione pakiety powyżej 100 ms nie certyfikują tej oceny. Znane własne polecenia HomeMind i działania użytkownika są wyłączone z oceny zależności; nadal obowiązują istniejące zasady nauki ręcznych przykładów. Brak wystarczających danych nie dodaje automatycznie jasności ani słońca do modelu.

Jawnie wybrane sensory, Additional sensor oraz kontekst automatyzacji HA pozostają zachowane. Ocena warunków jasności nowego automatycznego modelu korzysta z tego samego pomiaru otoczenia sprzed zapalenia lampy, co jego cechy. Chroni to przed uznaniem światła emitowanego przez sterowaną lampę za powód do jej wyłączenia. Dotychczasowe modele zachowują swoje znaczenie kolumn do czasu Rebuild.

Wynik jest zapisany w `selection_meta.training_context_assessment` i zdarzeniu `training_context_assessed`: zakres historii, liczba wejść, status każdego sensora, wybrane i odrzucone wejścia, korelacje zachowania oraz informacja o ograniczeniu skanu. Czas i zakres klasycznego screeningu znajdują się w `training_phase_timings`.

Nie zwiększamy retencji, wymiaru modelu ani limitów plików. Poprawki importu Recorder i WAL z 0.14.185 są częścią tego wydania.

## Weryfikacja i zakres wniosków

Pełny zestaw: 2237/2237 testów w Linuksie/WSL, bez pominiętych. 21 nowych testów oceny kontekstu przeszło również w Windows. [Zapis weryfikacji](benchmarks/CONTEXT_SELECTION_0_14_186_VALIDATION.json).

Regresje sprawdzają brak pozornej korelacji jasności emitowanej przez lampę, zależność przy wejściach w jasnym i ciemnym otoczeniu, późne i przyszłe pomiary, pochodzenie akcji, ograniczenie skanu, ponowną ocenę Rebuild oraz stały schemat kolejnych checkpointów.

Test procesu treningowego na syntetycznej historii przekaźnika Shelly obejmuje wejście, pobyt nieruchomy, krótki zanik radaru oraz krótką wizytę. Osobny test kuchni sprawdza utrzymanie warunku jasności. Są to testy rzeczywistego izolowanego HistoryManager i zapisanej polityki, bez wywołań HA; nie zastępują obserwacji w zainstalowanej łazience.

Zmiany w 0.14.167 mogły wpływać na ten agent przez automatyczne dodanie jasności radaru, a zachowanie starego schematu podczas Rebuild mogło utrwalać taki dobór. Dostarczony eksport dotyczył importu historii w 0.14.184 i nie zawierał późniejszych błędnych predykcji. Dlatego nie przypisujemy wszystkich skoków ze zdjęcia jednej potwierdzonej przyczynie.

## Zastosowanie

1. Zainstaluj 0.14.186 i zrestartuj dodatek.
2. Wykonaj Rebuild wyłącznie agenta `shellyplus1pm-441793a613bc` i pozostaw go w Shadow.
3. Sprawdź wejścia, nieruchomy pobyt, krótkie wizyty oraz krótkie zaniki radaru. Jasność nie powinna być wybrana automatycznie, jeśli nie wykazano jej związku z akcją.
4. Oceń wyniki Shadow na rzeczywistej historii przed przełączeniem na Control.
