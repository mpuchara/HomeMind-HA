# HomeMind 0.14.213: świeżość kafelków Shadow i użyteczny cache RAM

Trzy eksporty 0.14.212 z 17.10.2026, 16:11–16:12, pokazują około
196–211 MB RSS oraz 60–81% ostatniego użycia CPU. Zapis jest dopuszczony:
przydział dysku wynosi teraz 70%, bez aktywnej pauzy. Główna decyzja zajmuje
średnio około 122 ms, z ostatnim p95 około 238–254 ms. Pasywne Candidate
mają tylko siedem historycznych próbek event-to-decision i zero świeżych;
ich historyczne 5,77 s nie jest bieżącym pomiarem wszystkich agentów.

Użytkownik potwierdził, że opóźnienie widać na bieżących kafelkach.
Eksporty nie mierzą jeszcze czasu renderowania przeglądarki ani opóźnienia
gotowej decyzji do kafelka. Nie potwierdzają też przyczyny różnicy wartości
Desired i Current dla konkretnej zmiany. Sama niska zajętość RAM nie dowodzi,
że przydzielenie większej ilości pamięci przyspieszy predykcję.

## Dostarczenie ukończonej decyzji

Główna pętla publikuje mały, kompletny rekord decyzji w RAM przed pracą
opcjonalnych obserwatorów. Kafelek pobiera Desired, confidence i czas z tego
samego ukończonego rekordu. Current nadal pochodzi z rzeczywistego stanu HA.
Nowy Current nie jest przepisywany do Desired, a stare Desired nie jest
ukrywane, gdy nowa predykcja jeszcze trwa.

`/api/live` obsługuje `after=<revision>&wait_ms=1500`. Odpowiedź pojawia się
po nowej decyzji lub zmianie fizycznego celu. Przeglądarka ponawia oczekiwanie
po 100 ms zamiast czekać na kolejny cykl 500 ms. W spokojnym domu ogranicza
to również liczbę pustych odczytów. Starszy serwer lub błąd zachowuje polling
500 ms. Oczekiwanie nie trzyma blokady Engine ani połączenia SQLite.

Powiadomienie to licznik z losową epoką restartu, bez historii zdarzeń.
Maksymalnie osiem żądań czeka do 1,5 s; nadmiar otrzymuje zwykły snapshot
i wraca do pollingu. Token jest pobierany przed snapshotem, aby nie zgubić
powiadomienia opublikowanego w trakcie odczytu. UI nadal korzysta z istniejącego
limitu odczytu 2,5 s. Ingress i przeglądarka mogą dodać własny czas.

Kafelek wyjaśnia dwie różne sytuacje: „Updating decision”, gdy Current
zmieniło się po ostatniej ukończonej decyzji, oraz „Confirming OFF” z czasem,
gdy model zaproponował OFF, ale trwa istniejące potwierdzenie statystyczne.
Potwierdzenie OFF nie zostało skrócone. Nie jest to zmiana jakości wyuczonego
modelu ani deklaracja wcześniejszego przewidywania zdarzeń fizycznych.

## RAM i metadane

Worker Candidate korzysta z istniejącej puli połączeń i natywnego cache stron
SQLite, z dotychczasowym budżetem, kontrolą dostępnej pamięci i limitów cgroup.
Przestaje ponownie otwierać zimne połączenia do każdego odczytu metadanych.
Każdy wewnętrzny `conn()` nadal ma osobny commit/rollback, a połączenie wraca
do puli przed oczekiwaniem workera. Nie jest przechowywana stara transakcja
czy wynik SQL. Zmiana z innego połączenia jest widoczna w następnym odczycie.

Raz założona instrumentacja Root policy nie odpytuje ponownie tabeli generacji
przy każdym kolejnym `engine.policy()`. Nowy obiekt polityki nadal przechodzi
początkową klasyfikację. Format modeli, uczenie, promocja, dispatch Control,
Shadow bez usług HA, limity pamięci/dysku/WAL i retencja pozostają w mocy.
Nie zwiększamy sztucznie RSS ani nie rezerwujemy całej wolnej pamięci.

## Kontrola efektu

Nowe eksporty mają `engine.live_cards` oraz ślady `live_card_snapshot`, które
oddzielają zwykłe oczekiwanie na zmianę od kosztu utworzenia snapshotu.
Zawierają czas ukończonej decyzji, wartości Current/Desired, informację o nowszym
Current i potwierdzaniu OFF. Pozwala to odróżnić opóźnienie dostarczenia od
zachowania polityki przy następnej obserwacji problemu.

`tools/benchmark_live_delivery.py` porównuje niezależne źródła z prawdziwym
HTTP localhost, kontrolowaną publikacją gotowej decyzji, klasyfikacją Root
i rzeczywistą pętlą workera Candidate. Wynik nie obejmuje obliczenia modelu,
renderowania przeglądarki, proxy Ingress, HA ani fizycznego ACK. Test Node
wykonuje produkcyjne skrypty kafelków, wraz z wyjaśnieniami stanu.
Pełne wyniki znajdują się w `docs/benchmarks/LIVE_DELIVERY_0_14_213_*.json`.

Walidacja końcowa: **2566 testów OK**, bez pominięć, Python 3.12.3 i NumPy
2.3.3. W trzech naprzemiennych parach niezależnych źródeł odpowiedź lokalnego
API po kontrolowanej publikacji między cyklami starego pollingu przestała
czekać około 450 ms. To pomiar transportu HTTP localhost, bez 100-ms przerwy
ponowienia klienta, Ingress i renderowania, więc nie jest wynikiem całego UI.
We wszystkich parach Desired pozostało identyczne.

24 wywołania rozgrzanej Root policy wymagały jednego odczytu generacji zamiast
24. Worker wykorzystał jeden cache stron SQLite i 23 ponowne użycia przez
24 przebiegi; dla tej próby jego cel wynosił 64 MB w istniejącym budżecie
256 MB. Cel cache nie oznacza rzeczywistego RSS. Test z niezależnym writerem
potwierdził świeżość po commicie. Rzeczywisty Engine dostarczył 32 zdarzenia
do Shadow Executor bez usług HA. Control z GC dostarczył 64 zdarzenia i jedną
usługę do atrapy HA. Start wersji 0.14.213, kompilacja, 21 skryptów JS oraz
zgodność 589 znormalizowanych plików kodu przeszły kontrolę.

Po aktualizacji i restarcie warto przeładować stronę, aby wczytać nowe skrypty.
Full Rebuild nie jest potrzebny.
