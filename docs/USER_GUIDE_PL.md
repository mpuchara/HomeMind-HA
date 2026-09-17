# Adaptive AI — aktualna obsługa

Ten dokument opisuje bieżący produkt. Starsze wersje instrukcji pozostają w repo jako dokumenty historyczne.

## Główny ekran

Aplikacja uruchamia interfejs HTTP przed cięższą inicjalizacją runtime. Status startu pokazuje, czy gotowe są baza, Engine, realtime Home Assistant, historia i workery. Normalnie stan HA jest odbierany przez WebSocket; REST pozostaje ścieżką resynchronizacji/fallbacku.

## Karta Live

### Current
Aktualna wartość targetu z Home Assistant.

### Desired
Bieżąca decyzja polityki. W Shadow jest wyłącznie predykcją. W Control może przejść dalej do `ActionIntent`, ale dopiero Executor może wysłać usługę HA.

### Decision strength
Starsze pole nazywane `confidence` pozostaje kompatybilne w API, ale nie jest prezentowane jako prawdopodobieństwo komfortu. To siła decyzji wynikająca m.in. z separacji akcji, niepewności modelu i pokrycia kontekstu.

### Presence probability / Forecast uncertainty
To osobne wielkości modelu percepcji. Jeżeli pole jest prawdziwym prawdopodobieństwem, jego kalibracja jest oceniana na niezależnych przyszłych epizodach (Brier/reliability). Nie należy go utożsamiać z Decision strength.

### Data coverage / Held-out policy quality
Pokrycie mówi, ile odpowiednich dowodów ma model. Jakość polityki opisuje wynik na odłożonych danych. Dla bezpieczeństwa ON i OFF są oceniane osobno.

## Shadow, Control i Paused

### Shadow
- polityka wykonuje inference,
- Desired jest widoczne,
- może powstawać future evidence i porównanie Candidate,
- **żadna komenda fizyczna nie jest wysyłana**.

Shadow to proxy kontrfaktyczne: pokazuje, co model chciałby zrobić. Nie dowodzi fizycznego skutku niewykonanej akcji.

### Control
Control przechodzi dodatkowe zabezpieczenia niezależne od rewardu:
- ważna kwalifikacja,
- wystarczające osobne dowody dla ON/OFF,
- legal action mask,
- support/novelty i abstain,
- manual override,
- cooldown/min dwell,
- ownership/lease współdzielonego urządzenia,
- aktualność modelu i konfiguracji,
- ACK oraz bezpieczeństwo Executora.

Dla binarnego Control bieżący kontrakt statystyczny wymaga co najmniej 20 held-out próbek na akcję i 95% dolnej granicy Wilsona powyżej progu 78% dla każdego kierunku. Sam wysoki procent accuracy nie wystarcza.

### Paused
Normalne realtime inference/trening jest wyłączone lub ograniczone. Brak historii nie powoduje automatycznego obniżenia progów; system powinien pokazać brak dowodu i korzystać z fallback/Shadow.

## Live i Candidate

Live jest aktualną stabilną generacją. Candidate jest izolowanym bezpośrednim dzieckiem konkretnego rodzica i pozostaje Shadow do jawnej, atomowej promocji.

Karta generacji udostępnia:
- **Autonomous** — rozwój dziecka z dostępnych danych,
- **Correct** — jawna korekta na obserwowanej historii,
- **Explore** — kontrolowane badanie, w tym targeted sensor,
- **Change decision** — natychmiastowa jawna informacja o decyzji w konkretnym kontekście,
- **Promote** — osobny krok lifecycle,
- **Discard** — odrzucenie dziecka bez niszczenia rodzica.

Dalsza nauka Candidate tworzy kolejne dziecko. Feedback nie może przechodzić przez granicę niewłaściwego parent generation.

## Correct i Change decision

**Correct** jest etykietą użytkownika dla konkretnego obserwowanego kontekstu/Desired. Nie jest zwykłym rewardem „+1”. Model dziecka musi spełnić zaznaczone korekty i przejść regresję względem ważnych anchorów.

**Change decision** opisuje bieżącą preferencję/oczekiwaną decyzję. Brak feedbacku nie oznacza akceptacji. Negatywna ocena jednej akcji nie wymyśla automatycznie poprawnej przeciwnej akcji.

Trwała instrukcja użytkownika nie wygasa tak jak statystyka z historii.

## Explore i eksperymenty

Explore korzysta z istniejącej infrastruktury eksperymentów. Próba ma wersjonowany `TrialRecord`: hipotezę, dostępne akcje i propensity, przypisaną akcję, dispatch/ACK, źródła outcome, wynik i status aplikacji do konkretnego dziecka.

Candidate nie przejmuje fizycznego sterowania. Jeżeli Explore wymaga fizycznej próby, jej właścicielem pozostaje root Live oraz istniejący Executor. Brak outcome pozostaje nieznany zamiast być zamieniany na sukces/porażkę.

## Promocja

Promocja nie opiera się na jednym `promotable=true`. Runtime składa listę nazwanych wyników walidacji. Każde veto pozostaje widoczne; kolejność modułów nie może go usunąć.

Dane do wyboru challengera i finalnej oceny są rozdzielone. Po selection zbierany jest fixed future test; regularne odpytywanie UI nie wydłuża go opportunistycznie. Same poprawne OFF nie kwalifikują ON.

Promote wykonuje atomowy swap modelu/generacji/trybu/ownership. W razie błędu poprzedni snapshot jest odtwarzany.

## Cold start i dryf

Nowy dom lub agent bez historii pozostaje w fallback/Shadow i raportuje brak danych. System może zasugerować niewielką liczbę opcjonalnych pytań użytkownikowi, ale odpowiedź nie omija zabezpieczeń.

Monitor dryfu rozróżnia:
- awarię sensora,
- zmianę topologii/przeniesienie sensora,
- zmianę zwyczaju,
- nową preferencję.

Pogorszenie tworzy izolowanego Candidate. Live nie jest natychmiast resetowany. Po promocji monitorowane jest odzyskanie jakości i możliwy rollback.

## DeviceAgent i współdzielone urządzenia

Kilka encji HA może reprezentować jeden fizyczny zasób. HomeMind używa `device_id` lub jawnego mappingu, a nie friendly name. Power i brightness jednej lampy współdzielą arbiter, manual override i lease. Wydanie brightness jest spójną pojedynczą komendą; 0% oznacza OFF.

Nieopisane `switch/number/select` nie dostają autonomii tylko dlatego, że są zapisywalne. HVAC i rolety mają inną dynamikę niż szybkie światło i wymagają odpowiedniego modelu procesu.

## Co oznacza „RL” w HomeMind

W produkcie występuje kilka różnych mechanizmów, których nie należy mieszać:

- **historyczna demonstracja**: obserwowane zachowanie targetu; służy do bootstrapu/uczenia, ale nie jest prawdą o komforcie,
- **contextual bandit**: aktualizuje tylko logowaną/wykonaną akcję; niewybrana akcja ma nieznany reward,
- **model obecności**: osobna percepcja/prognoza z własną kalibracją,
- **Correct/preference**: jawna informacja użytkownika,
- **experiment/TrialRecord**: kontrolowana próba z outcome attribution,
- **Shadow**: kontrfaktyczna predykcja/proxy,
- **physical outcome**: obserwowany skutek faktycznie wysłanej komendy po Executorze.

ACK potwierdza wykonanie transportowe, a nie komfort. Replay automatyzacji nie jest etykietą potrzeby światła.

## Benchmark produktu F24

Repo zawiera deterministyczny benchmark produktu z ukrytą obecnością i ukrytą potrzebą światła. Obserwacje mają delay, noise, missingness, różne formaty oraz sprzężenie `light -> lux`. Scenariusze obejmują m.in. jednego/dwóch domowników, rozwidlenie, bezruch, brak przyjścia, quick return, dzień/noc, ręczną zmianę, fałszywy/przeniesiony sensor i zmianę zwyczaju.

Porównywane są stała automatyzacja, bieżący runtime, full-ridge Shadow oraz ostrożny fallback na oddzielnych train/validation/future danych i wielu seedach.

Benchmark nie promuje modelu. Brak poprawy lub niespełnione kryteria są prawidłowym wynikiem. Wynik syntetyczny nie zastępuje testu na realnym Home Assistant ani fizycznego M&V.
