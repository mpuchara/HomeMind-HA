# Adaptive AI 0.10.9 — 2026-09-14

Baza: najnowszy main 9d28c07 (0.10.8 HF1). Zachowano poprawkę zawieszania interfejsu i startu procesu.

## Dwa rodzaje uczenia

- **Naucz:** etykieta prawidłowego Desired w aktualnym kontekście. ON/OFF odwraca najnowsze Desired pod blokadą celu, a nie Current. Przy braku predykcji użytkownik podaje 0 lub 1. Dla nastaw liczbowych lub indeksów opcji podaje wartość; backend stosuje zakres i krok urządzenia.
- **Naucz / popraw:** dotychczasowa bezpośrednia korekta fizycznego Current wraz z nauką. ON/OFF odwraca aktualny stan urządzenia, również w Shadow.

Oba przyciski są stale widoczne w pasku akcji poza zwijanymi szczegółami. Naucz działa w Shadow, Control i Paused. Podczas aktywnego treningu historycznego zwraca jasny komunikat, aby powtórzyć po zakończeniu; nie zgłasza pozornego zapisania nauki.

Naucz zapisuje dodatnią ocenę wskazanej wartości i ujemną błędnej predykcji oraz szeroki kontekst. Aktualizuje i utrwala model. Nie wysyła usługi HA, nie przejmuje automatyzacji, nie zmienia trybu ani blokady manualnej i nie ocenia oczekującej akcji fizycznej. Gdy taka akcja lub jej wynik oczekuje, promocja nowych cech jest odraczana, żeby nie zmienić znaczenia zapisanych wektorów. Kolejne korekty mogą wykorzystać zebrany kontekst.

Desired na karcie pokazuje rzeczywistą predykcję po aktualizacji modelu, a nie wymuszoną na stałe wartość. Jedna wskazówka może nie przeważyć dużej ilości wcześniejszej nauki. Jest to uczenie preferencji w kontekście, nie trwała reguła ani ręczna nastawa. W Control zwykła pętla sterowania nadal może wykonywać decyzje; Shadow służy do nauki bez autonomicznych poleceń.

## Przyczyna podwójnych kart

Wyszukiwarka zapamiętywała starszą funkcję renderującą z app.js, podczas gdy polling używał nowszego renderera p0.js. Stary renderer zastępował HTML listy, pozostawiając nowe karty w pamięci. Kolejny polling dopisywał je obok starszych kart. Wyszukiwanie teraz wybiera aktualny renderer w chwili zdarzenia, a p0 usuwa obce karty i używa stabilnych węzłów o znormalizowanym ID. Osobno poprawiono CSS: display:flex nadpisywał atrybut hidden, przez co odfiltrowane karty nadal były widoczne.

## Weryfikacja

186 testów unittest; osiem nowych testów backendu z rzeczywistą polityką i SQLite oraz test uzgadniania kart. Test uruchomienia JS sprawdza dynamiczny wybór renderera przy wyszukiwaniu. Zachowane testy startu procesu i zabezpieczenie przed zapętlonym obserwatorem DOM.

W lokalnej przeglądarce z symulowanym urządzeniem: Naucz zmienia Desired OFF→ON, pozostawiając Current OFF; Naucz / popraw zmienia Current OFF→ON. Wyszukiwanie, brak wyników i kolejne odświeżenia sprawdzono na rzeczywistych skryptach i CSS. Polecenia fizyczne były symulowane; nie zmieniano urządzeń użytkownika.

Pełny raport: TEST_RESULTS_0_10_9.txt. Kompilacja Pythona, składnia wszystkich JS, symulator, mikrobenchmark i integralność ZIP są sprawdzane przez tools/release_checks.py. Lokalnie nie wykonano testu Docker ani pomiarów Raspberry Pi; workflow CI buduje obraz i sprawdza jego gotowość.
