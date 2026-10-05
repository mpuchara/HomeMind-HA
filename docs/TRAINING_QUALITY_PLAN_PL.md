# Plan poprawy jakości treningu

Cel: historyczne automatyzacje dostarczają początkowych demonstracji, a agent
uczy się potrzeb mieszkańców i ma przewyższać automatyzację. Zgodność z historią
jest diagnostyką odtwarzania, nie dowodem tej przewagi.

## Stan kodu przed zmianą

- HistoryManager odkrywa sensory przez balanced_presence_driver_score oraz
  numeric_activity_driver_score, osobno dla krawędzi ON/OFF. Obsługuje sensory
  poprzedzające zmianę, lokalne podtrzymanie oraz kauzalny replay.
- _effective_fast_dwell_end ogranicza podtrzymanie po przeciwnej krawędzi sensora;
  nie stanowi samodzielnego dowodu, że mieszkaniec przestał potrzebować światła.
- normalized_dwell_sample_mass ogranicza powtarzane próbki jednego okresu.
- Correct, wykrywanie sprzecznych etykiet, AutomaticRewardJournal i zachowawczy
  Offline RL już istnieją. Nie trzeba budować tych mechanizmów drugi raz.
- DeferredUpdates stosuje wagi od razu w kolejności prequential. Finalna iteracja
  jest pusta: nie ma utraty wag ani powtórnego uczenia holdoutu.
- Dotychczas późna ręczna zmiana światła mogła nagradzać poprzednią decyzję.
  Benchmark Correct zawierał także ujemnie ocenione działania historyczne.

## Wykonywany zakres: fundamenty jakości

1. Rozdzielić wiarygodność źródła od użyteczności działania i budżetu próbek.
   Użytkownik: 1.0; rozpoznana automatyzacja: 0.5; nieznane źródło: 0.25.
   Sygnał upstream mnoży te wagi przez 0.35. Własne polecenia pozostają wykluczone.
   Znany user_id uzupełnia tylko brakujące pochodzenie, nigdy own_command.
2. Zachować karę za szybką korektę. Późniejszą ręczną zmianę automatycznego stanu
   światła do 90 s potraktować jako niejasną, reward=0, bez uczenia i benchmarku.
   To nie jest automatyczne stwierdzenie błędu. Okno jest konfigurowalne 8–300 s.
3. Rozdzielić rzeczywisty czas do zmiany użytkownika od czasu podtrzymania
   skróconego krawędzią sensora. Stosować tę samą regułę w replay recent/long-memory.
4. Wyłączyć reward<=0 z historycznych wzorców poprawności benchmarku Correct.
   Zachować wykluczenie punktów Teach i sprawdzanie regresji na innych przykładach.
5. Dodać testy wag, krótkiego ręcznego użycia, spóźnionej korekty, sensorowego
   skrócenia okresu, budżetu podtrzymania i filtracji benchmarku. Uruchomić
   istniejące testy replay, Correct, provenance i treningu. Podnieść TRAINING_REVISION
   do v21, ponieważ zmienia się znaczenie dowodów treningowych.

Wagi są hipotezą startową. Zmiany dotyczą przyszłego treningu; istniejące modele
nie zyskują nowego zachowania bez ponownego Train/Rebuild. Nie zmieniamy kontraktu
cech ani nie wymuszamy automatycznego rebuild przy starcie.

## Wynik wdrożenia fundamentów (2026-10-05)

Wdrożono punkty 1–5 zakresu fundamentów. Zaliczone: 126 testów jakości,
Agent Training vNext, Candidate Correct, nagród, provenance oraz wersji 120/130;
osobno 15 testów przepustowości i batchowania replay. Nowy zestaw zawiera 12 testów,
w tym rzeczywisty izolowany worker: ręczne OFF 20 s po automatycznym ON zapisuje
reward=0, powiększa licznik ambiguous_user_override i nie dostarcza wzorca uczenia.
Worker zapisuje modele Ridge i TinyMLP.

Benchmark feature_snapshot_full_replay: experiences, Ridge, TinyMLP i return_value
mają zgodność z cache/bez cache; 301→255 rekonstrukcji cech. Compileall i diff-check
przeszły. Szerszy uruchomiony zestaw obejmujący testy nadzoru procesów (wersja 080)
przerwał się przez KeyboardInterrupt na Windows; nie jest zaliczony. Pełne CI/Linux
i pomiary na HA/Raspberry Pi pozostają niewykonane. Poniższe etapy zostały następnie wdrożone w 0.14.141; wyniki syntetyczne nie są deklaracją przewagi na rzeczywistej instalacji.

## Kolejne etapy wymagające danych i osobnych eksperymentów

### Powtarzalność warunkowa i wyjątki

W obrębie podobnego kontekstu mierzyć liczbę niezależnych zdarzeń/dni, zgodność
akcji i skutków oraz niepewność. Zwykła częstość ON/OFF nie jest wiarygodnością.
Zachować rzadkie jawne preferencje. Sprzeczne demonstracje kierować do analizy
brakujących sensorów, a nie usuwać przez globalne odrzucanie odstających próbek.
Wyznaczanie wag odbywa się wyłącznie z przeszłości względem ocenianej decyzji.

### Włącz / utrzymaj / wyłącz

Ocenić czy istniejące features i Home Context rozróżniają wejście, siedzenie bez
ruchu, wyjście i niedostępny sensor. Brak ruchu nie wystarcza do wyłączenia.
Zweryfikowana obecność ma silniej chronić przed przedwczesnym OFF niż krótkim
zbędnym ON. Wyniki przyszłe służą ocenie, nie wejściom modelu.

### Walidacja przewagi nad automatyzacją

Przygotować kontrolowane scenariusze z niezależną prawdą preferencji: opóźniony
ON, fałszywy ruch, osoba nieruchoma, wyjście, awaria sensora i poprawny rzadki wyjątek.
Porównać automat i agenta na tych samych wejściach, ale w niezależnych przebiegach.
Sam Shadow na historycznej trajektorii nie odtwarza skutków alternatywnych działań.
Mierzyć czas oczekiwania, zbędny czas ON, OFF podczas używania, interwencje i chatter.
Stroić wagi na wcześniejszych dniach; oceniać na późniejszych, z rozdzieleniem dni
i zdarzeń. Raportować liczebność oraz niepewność. Nie zastępować testu przewagi
historyczną accuracy ani proxy Offline RL.

## Kryterium ukończenia dalszej strategii

Agent musi wykazać poprawę wskaźników potrzeb mieszkańców względem automatyzacji,
utrzymać rzadkie poprawne wyjątki, generalizować Correct poza oznaczony punkt i
nie pogorszyć bezpieczeństwa OFF. Bez danych z domu nie deklarujemy optymalnych
wag ani potwierdzonej przewagi rzeczywistej instalacji.

## Ukończenie etapów w 0.14.141

1. Powtarzalność warunkowa: ConditionalPatternMemory ma najwyżej 256 kontekstów
i 64 zdarzenia na kontekst. Używa skwantowanych wartości wybranych sensorów,
bez celu sterowanego i zegara. Jeden głos na akcję/kontekst/dzień ogranicza chatty
sensory. Trzy niezależne dni uruchamiają osłabianie mniejszości, z limitem 0.2.
Jawne etykiety użytkownika zachowują siłę. Do zapytania mogą wejść tylko skutki
dostępne wcześniej niż chwila predykcji; bieżący i przyszły skutek są wyłączone.
Pamięć zapisuje się z checkpointem i jest resetowana przy zmianie schematu/akcji.

2. Włącz/utrzymaj/wyłącz: light_dwell_reward rozróżnia zajęty pokój, potwierdzoną
nieobecność, konflikt i nieznany wynik. Brak ruchu nie zastępuje czujnika occupancy.
Potwierdzone OFF podczas obecności otrzymuje -1; fałszywe ON przy ciągłej potwierdzonej
nieobecności -0.6. Przerwa w obserwacji lub zbyt wiele zdarzeń wyłącza wniosek
o nieobecności. To ocena wyników treningu, nie dodatkowy wykonawca poleceń.

3. Walidacja przewagi: benchmark_training_quality stosuje produkcyjną matematykę
Ridge/dowodów oraz osobne stany/timery obu kontrolerów. Pięć scenariuszy na trzech
seedach obejmuje siedzenie bez ruchu, błędny ruch, wyjście, opóźnione wejście,
awarię radaru i rzadką jawną potrzebę. Korekty występują tylko w danych treningowych.
Każdy scenariusz ma własne kryterium bezpieczeństwa i kosztu, z niezależną prawdą
potrzeby mieszkańca. Nie stosujemy historycznej zgodności jako prawdy preferencji.
Benchmark wymaga reprezentacji cech z interakcjami, już dostępnej w kodzie.

4. Wydanie: CI zawiera nowy benchmark; workflow publikacji uruchamia się dopiero
po sukcesie całego Validate HomeMind na aktualnym main. Pakowanie weryfikuje
wersje, obecność modułów, ZIP CRC i manifest SHA-256. Publikowane są wersjonowane
ZIP-y repozytorium i katalogu głównego dodatku oraz sumy kontrolne.

Pozostaje walidacja terenowa i strojenie wag na danych gospodarstwa domowego.
Implementacja i testy wszystkich etapów nie zastępują tego pomiaru.
