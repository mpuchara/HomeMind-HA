# Wydanie 0.11.0

Baza: main 00bb66a (0.10.9). Zachowano wcześniejsze poprawki startu, braku zapętlonego obserwatora DOM i podwójnych kart.

## Obsługa

Na karcie są cztery akcje: przełącznik Shadow/Control, Wrong decision, Settings i Teach. Ze stanu Paused przełącznik najpierw włącza Shadow. W Settings są Pause, Train, Resume, Rebuild model, Eksperymenty, Delete, cofanie i pełna diagnostyka wraz z dotychczasowymi ustawieniami.

Wrong decision odwraca aktualne Desired dla ON/OFF; dla innych nastaw prosi o wartość. Nie zmienia Current bezpośrednią usługą. Zapisana wskazówka zmienia Desired w pasującym kontekście. W Shadow służy do obserwacji, a w Control przechodzi przez Executor. Kwalifikacja, przegląd urządzenia, świeżość danych, potwierdzanie i limity pozostają aktywne. Ręczna etykieta jest jawną instrukcją użytkownika; jej wykonanie nie wymaga przekroczenia statystycznego progu pewności modelu bazowego. Karta pokazuje pochodzenie decyzji.

W Teach wybierz daty (do 31 dni jednocześnie), przybliż kółkiem lub przyciskami +/− albo przeciągnij zakres. Kliknięcie wybiera czas. Można też wpisać dokładną datę z sekundami i użyć Sprawdź punkt. Podaj poprawne Desired i naciśnij Zapisz Desired. Niepełny kontekst blokuje zapis zamiast podstawiać dzisiejsze dane. Pomarańczowe punkty oznaczają aktywne korekty. Dla wyborów kategorycznych wartość oznacza indeks opcji; ich zmiana lub przestawienie unieważnia dopasowanie starej etykiety.

Cofnij ostatnią naukę działa w Teach i Settings, osobno dla każdego agenta. Usuwa najnowszą aktywną etykietę z Wrong decision lub Teach; kolejne kliknięcia cofają wcześniejsze. Nie przywraca całej starej kopii modelu, nie kasuje późniejszej fizycznej nauki i nie jest cofnięciem fizycznego polecenia. W Control zwykła pętla może potem zmienić urządzenie zgodnie z odzyskaną decyzją. Korekty zapisane starymi przyciskami przed 0.11.0 pozostają w bazowym modelu i nie mają selektywnego undo.

## Wykres a dawne Desired

Current pochodzi z lokalnego archiwum HA. Przerywana linia Desired to odtworzenie obecnego modelu na dawnym kontekście, z uwzględnieniem aktywnych korekt. Nie jest przedstawiana jako faktyczna predykcja starej wersji aplikacji. Nie używamy dzisiejszego modelu obecności domu do wytwarzania pozornie historycznych cech; historyczna predykcja bez tego kontekstu może różnić się od dawnej rzeczywistej decyzji.

Od 0.11.0 decyzje są zapisywane po zmianie lub co 30 s. Opcjonalna szara linia pokazuje zarejestrowane Desired i nie łączy długich przerw. Retencja wynosi 31 dni. Wykres ma do 1000 punktów, a odczyt Recorded do 2000; informuje o redukcji. Gęste strumienie radarów przechodzą na próbkowanie zapytaniami po indeksie, z zachowaniem krawędzi Current w przedziałach. Sprawdzenie konkretnego punktu zawsze pobiera dokładny stan as-of i wymagane opóźnienia; agregacja wykresu nie zmienia etykiety.

## Nauka i wydajność

Do 256 aktywnych etykiet agenta znajduje się w osobnym dzienniku SQLite i pamięci podręcznej. Dopasowanie używa nazw cech, czasu i zmian sygnałów; nie przenosi instrukcji ON na stan czujnika OFF ani na brakujące dane. Zmiana zestawu cech lub konfiguracji może spowodować brak dopasowania; korekta pozostaje w dzienniku. Prawdziwa nowsza ręczna zmiana urządzenia wycofuje sprzeczne etykiety pasujące do tego kontekstu.

Lekki endpoint /api/live czyta Current z pamięci strumienia HA i konfigurację bez obliczania rekomendacji, historii lub kwalifikacji. Interfejs pyta co około 500 ms po poprzedniej odpowiedzi, bez nakładania żądań, z timeoutem. Pierwsze karty mogą powstać przed ciężką diagnostyką. Nie jest to gwarancja 500 ms reakcji sprzętu: pozostają opóźnienia sieci, HA, urządzenia i fallback REST. Rekomendacje nie trzymają już blokady odbioru stanów przez cały swój przebieg.

## Weryfikacja

208 testów, w tym 21 nowych testów backendu historii/undo/Control i test uruchomienia lekkiego widoku przy zablokowanym statusie. Test z 2000 wcześniejszych przykładów odtwarza słabość starego +1/−1 i skuteczność nowej korekty w tym samym kontekście. Sprawdzono SQLite, restart, cofanie przy niezależnej nauce, brak wycieku przyszłych danych, ruch ON/OFF, temperaturę, przestawienie opcji oraz brak pomijania kwalifikacji Control.

Przeglądarka lokalna: cztery akcje na karcie, Settings, przybliżenie, wybór punktu, zapis Desired OFF→ON, cofnięcie ON→OFF i Wrong decision w Shadow bez zmiany Current. Użyto symulowanego urządzenia, bez dostępu do domu użytkownika. Docker i Raspberry Pi nie były mierzone lokalnie.
