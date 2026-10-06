# Adaptive AI 0.14.143 — uczenie stanu światła z liczbowego radaru

Naprawa obejmuje właściwą ścieżkę treningową i predykcję używaną przez dodatek,
także gdy światłem steruje przekaźnik `switch.*`. Poprzednie zmiany dotyczące
nieruchomej obecności nie obejmowały wszystkich warstw tej ścieżki.

Model szybkiego urządzenia binarnego uczy teraz żądanego stanu ON/OFF z
zaakceptowanych przykładów. Dotychczasowa suma dodatnich nagród dla osobnych
akcji mogła preferować OFF również dla dodatniej energii charakterystycznej
dla obecności. Klasyfikator uczy granicy z danych danego agenta, bez wspólnego
progu energii dla wszystkich radarów. Ograniczone nieliniowe funkcje względem
rozkładu danych pozwalają zachować słabszy sygnał nieruchomego celu również
przy częściowo błędnych etykietach i mocniejszych impulsach wejścia. Oddzielne statystyki wsparcia,
niepewności, kalibracji i ujemny feedback pozostają częścią polityki.

ON i OFF mają równą łączną masę w dopasowaniu klasyfikatora; wewnątrz klasy
zachowane są wagi źródła, jakości wyniku, powtarzalności i podtrzymania stanu.
Do 128 przykładów na stan jest próbkowanych z całej zaakceptowanej historii,
a do 8 ostatnich zapewnia dostępność nowych korekt. Jeden przykład nie jest
liczony dwukrotnie. Dopasowanie jest ograniczone do 160 iteracji co 64
aktualizacje oraz na granicy walidacji, końcu treningu i przy mocnej korekcie.
Ujemna nagroda nie tworzy niezaobserwowanej pozytywnej etykiety przeciwnej akcji.
Potwierdzenia własnych poleceń nadal nie są niezależnymi demonstracjami.

Nowy kontrakt cech 3 obejmuje liczbowe energie LD2410, odległości cm/m/mm oraz
neutralne metadane zdrowych źródeł również dla przekaźników. Automatyczny dobór
dołącza ograniczony zestaw kanałów tego samego radaru. Zachowuje pierwszeństwo
wejść automatyzacji, limity wymiarów i jawny ręczny wybór encji. Binarne flagi OFF nie dowodzą nieobecności, gdy
kanały liczbowe tego samego radaru nadal raportują sygnał: mogą działać
poniżej skonfigurowanego progu wykrywania. To nie tworzy etykiety obecności;
zachowuje niepewność i pozwala uczyć ON/OFF z zaobserwowanych przykładów.

Kwalifikacja wymaga teraz także poprawnego utrzymywania ON i OFF na zamrożonym
modelu w późniejszym fragmencie historii. Jeden pobyt to jeden głos, zaliczony
tylko przy poprawnych wszystkich wybranych chwilach wewnątrz niego. Każda klasa
musi przekroczyć próg kwalifikacji. Wynik karty uwzględnia słabszy z wyników
przełączeń i utrzymania. Turniej TinyMLP porównuje te same całe pobyty i także
wymaga jakości utrzymania; same poprawne impulsy przy wejściu nie wystarczą.

## Aktualizacja

Zainstaluj 0.14.143 z repozytorium dodatku Home Assistant albo paczki addon-root.
Następnie wykonaj **Rebuild** agenta. Przebudowa zachowuje wcześniejszy dobór
wejść, aktualizuje pusty szablon cech i uczy nowe współczynniki. Zapisane modele
kontraktów 1/2 nie są reinterpretowane podczas zwykłego ładowania.

Oceń w Shadow wejście, dłuższe siedzenie, ruch przy umywalce i wyjście. W
diagnostyce sprawdź ocenę utrzymania ON/OFF. Energia i odległość pomagają
rozróżnić sytuacje, lecz nie są samodzielnym dowodem fizycznej obecności.

## Weryfikacja

Test procesu izolowanego workera, archiwum SQLite i zapisanego modelu obejmuje
240 okresów z liczbową energią tła, impulsem wejścia, nieruchomym celem oraz
wyjściem. Ta sama ścieżka sprawdza przebudowę starszego szablonu oraz turniej
TinyMLP. Osobny przebieg obejmuje dodatnią energię i błędne binarne flagi OFF. Dodatkowe regresje obejmują jednostki, źródła radaru, zapis i odczyt
modelu, zamrożone decyzje, ograniczenie pamięci i ujemny feedback. Osobna regresja odwraca co siódmą etykietę i sprawdza
rozpoznanie dominujących wzorców pustego pokoju, nieruchomego celu oraz wejścia. Wynik
syntetyczny nie jest pomiarem jakości na konkretnej instalacji.
