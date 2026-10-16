# HomeMind 0.14.210: mniej obliczeń między kolejnymi decyzjami agenta

Sześć eksportów z 16.10.2026, 21:37:48–21:39:42 czasu polskiego, pochodzi
z 0.14.208. Ostatni eksport pokazuje średnio 69 ms do rozpoczęcia pracy nad
celem i 155 ms do decyzji. Niedawne p95 wynosi odpowiednio 508 i 623 ms.
Nie ma próbek rzeczywistych usług HA ani pasującego potwierdzenia stanu.
Pomiary dotyczą więc obsługi zdarzeń i decyzji, nie reakcji fizycznej lampy.

Szczegółowy ślad ujawnia także dłuższe zatrzymania: zadanie celu trwało
10,03 s, w tym 9,83 s synchronicznej obserwacji Sensor Tournament po decyzji.
Drugi wątek sterowania zajmował się analogiczną obserwacją przez 8,59 s.
W tym czasie kolejne zdarzenie może czekać na zwolnienie swojego celu.
Występują również kilkusekundowe zapisy i checkpointy SQLite.

Profilowanie odtworzonej puli 96 sensorów, z 96 przykładami na sensor,
wskazało powtarzane sprawdzanie tych samych wartości podczas obliczania
korelacji. Ocena najpierw filtrowała pary, potem kopiowała je do nowych
list i ponownie walidowała w funkcji korelacji, osobno dla każdej grupy.

Poprawka waliduje etykietę raz na przykład, a wartość cechy raz na grupę.
Korelacja korzysta z już sprawdzonych par. Dla pełnego okna 96 przykładów
i dziesięciu grup liczba walidacji maleje z 3840 do 1056. Pozostają dotychczasowe kopie
przykładów, kolejność sumowania, obsługa braków, pokrycie, rozstrzyganie
remisów i dokładne wyniki. Każda niezależna zmiana celu nadal odświeża
ocenę oraz przykłady. Przedziały obserwacji, wagi modeli, progi promocji,
kontrole sum SHA, zabezpieczenia HA, limity pamięci i zapisu, WAL oraz
retencja 14 dni zachowują dotychczasowe działanie.

To ograniczenie kosztu obserwatora wykonywanego po decyzji. Nie usuwa
oczekiwania na dysk ani wszystkich źródeł długich opóźnień. Nie zmienia
schematu modelu i nie wymaga Full Rebuild.

Bazą tej gałęzi jest 0.14.208 i PR #325. Równoległa, nieukończona w chwili
rozpoczęcia pracy gałąź 0.14.209 nie została dołączona do tej paczki.

Dowody i walidacja znajdują się w plikach
`docs/benchmarks/SCREENING_REACTION_0_14_210_*.json`.
Benchmark porównuje osobno uruchomione źródła 0.14.208 i tej wersji,
pełną pulę, predykcje oraz faktyczne uczenie czterech kandydatów po nowych
etykietach. Wynik na komputerze testowym nie jest prognozą czasu działania
Raspberry Pi, sieci HA ani fizycznego urządzenia.

W trzech parach na Linuksie sama ocena statystyczna przyspieszyła 1,43–1,57x
(mediana 1,47x, około 32% mniej czasu). Cała obserwacja z uczeniem po nowej
etykiecie trwała 136,6–148,7 ms przed zmianą i 128,5–135,4 ms po zmianie;
mediana przyspieszenia to 1,086x, czyli około 8% mniej czasu. Pełne oceny,
wiersze puli, predykcje oraz wyuczone parametry czterech kandydatów mają
identyczne SHA-256. Każdy kandydat rzeczywiście uczył się na 12 nowych
etykietach. Te wyniki zastępują wstępny pomiar prostszego obserwatora.
