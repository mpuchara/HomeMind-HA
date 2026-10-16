# HomeMind 0.14.207: historia Current i długi pobyt przy włączonym świetle

Zgłoszenie dotyczy lampy faktycznie świecącej w ciemnej kuchni. Krótkie OFF/ON
na żółtej linii są decyzjami Shadow. Wykres Current powinien niezależnie pokazać
rzeczywiste ON w czasie obecności i OFF poza nim.

## Current

Wykres korzystał wyłącznie z osobnego archiwum stanów targetu. Gdy to archiwum
pozostawało za bieżącą obserwacją, nadal przedłużał dawny OFF, choć
decision_history.current zawierało już fizyczne ON odczytane przez Engine.
Teraz łączymy te dwa strumienie rzeczywistych obserwacji według czasu. Dokładny
event HA wygrywa przy identycznym timestampie. Późniejszy rzeczywisty OFF
kończy wcześniejszy ON. Obejmuje to bufory przed zapisem, generację Live,
Candidate z bezpośrednim rodzicem oraz inspektor punktu Correct.

Nie podstawiamy Desired jako Current. Nie odtwarzamy modelu wstecz i nie
przepisujemy zapisanych decyzji Shadow. Stare impulsy Desired pozostają
widoczne jako to, co agent rzeczywiście zdecydował. Oś wykresu ma etykiety
ON/OFF i czasu. Pole current_source opisuje źródło rzeczywistego stanu.

Sam zrzut nie pozwala potwierdzić dokładnej zawartości bazy na urządzeniu.
Opisany przypadek opóźnionego archiwum jest odtworzony na rzeczywistym readerze
obu wersji, z jawnie zapisanym fizycznym Current=1 i impulsami Desired.

## Długie ON i światło własne żarówki

Live Engine zachowuje 24 surowe próbki na encję. Po ich wyparciu znikał odczyt
jasności sprzed włączenia żarówki albo dowód początku bieżącego ON. To zmieniało
znaną jasność otoczenia w brak danych mimo dalszej obecności i ciągłego świecenia.

Po udowodnieniu początku ON i odczytu sprzed akcji zachowujemy małą obserwację
jasności niezależnie od surowej kolejki. Klucz zawiera target, sensor i semantykę
raw/lux; użycie wymaga identycznego HA last_changed oraz zgodnych czasów zdarzenia
i dostępnej wiedzy. Cache ma maksymalnie 256 wpisów, zawiera wyłącznie małe,
wyliczone obserwacje i zwraca własne kopie. Nie powiększamy surowej historii ani
nie dodajemy odczytów SQL do wnioskowania.

Nowe OFF/ON, restart z lampą już ON, brak znacznika lub brak zaobserwowanego
odczytu sprzed akcji nie pozwalają pożyczyć starej jasności. W takim przypadku
wynik nadal jest nieznany. Pamięć nie szacuje zmian światła dziennego podczas
ciągłego ON: zachowuje dotychczasową semantykę odczytu sprzed akcji.
HA opisuje last_changed jako zmianę stanu, niezależną od samych zmian atrybutów:
[State object](https://www.home-assistant.io/docs/configuration/state_object/).

Liczniki dostępne są w eksporcie runtime pod
engine.deferred_persistence.photometric_baselines. Poprawka zwiększa ciągłość
zaobserwowanego wejścia, bez reinterpretacji kolumn, zmiany wersji kontraktu
cech i zerowania wag istniejącego modelu.

## Ciągłe potwierdzenia obecności

Dziesięciominutowy test wykrył dodatkowe zanikanie prawdopodobieństwa obecności:
nowe zdarzenia z tym samym ON nie odnawiały świeżości, bo zegar był zakotwiczony
w pierwszym stanie ON. Stan nadal ma pierwotne state_since_ts, ale świeżość
stanowych źródeł radar_occupancy i occupancy_binary jest teraz liczona od
najnowszego rzeczywistego event_ts. Gdy potwierdzono tylko ponowny odbiór tego
samego starego zdarzenia, zmienia się wyłącznie komunikacja i źródło nadal wygasa.
PIR zachowuje dotychczasowe wygaszanie sygnału ruchu. OFF, niedostępność i przyszłe
zdarzenia są osobno objęte testami. Nie jest to reguła utrzymywania lampy ON bez
nowych dowodów obecności.

## Krótkie decyzje OFF

Istniejące sześć sekund potwierdzania statystycznej decyzji OFF obejmowało
historical_policy_bootstrap, ale pomijało hybrid_tiny_mlp_ridge_guard. Teraz
obie ścieżki wyuczone wymagają tego samego potwierdzenia, gdy rzeczywista lampa
już świeci. ON pozostaje natychmiastowe. Utrzymująca się decyzja OFF przechodzi
po sześciu sekundach. Polecenia użytkownika, preferencje i eksperymenty nadal
omijają filtr. Wykres pokazuje efektywną decyzję z runtime, bez wygładzania jej
tylko na potrzeby prezentacji.

## Walidacja

tools/verify_correct_lighting.py uruchamia niezależne źródła 0.14.206 i 0.14.207:

| Odtworzony przypadek | 0.14.206 | 0.14.207 |
| --- | --- | --- |
| Fizyczne ON w logu, stare OFF w archiwum | Current=OFF | Current=ON |
| Jasność 20 przed ON, wyparcie surowych próbek | nieznana | nadal 20 |
| Chwilowy OFF z Tiny MLP przy świecącej lampie | OFF od razu | ON podczas potwierdzania |

Po poprawieniu świeżości obecności scenariusz z narzędzia benchmark_kitchen_lighting używa rzeczywiście
wytrenowanego i ponownie załadowanego modelu, ciemnego pomieszczenia, obecności
stacjonarnej i zmiennego światła własnego. Obejmuje 10 minut oraz oryginalny
limit 24 surowych próbek. Sprawdzamy predykcje samego modelu i dostępność
jasności, bez zastępowania akcji stałą regułą ON. Są to dane syntetyczne;
model użytkownika i jego dane z instalacji nie były dostępne.

Weryfikacja przeglądarkowa używa produkcyjnego JavaScriptu, CSS, readera historii
i inspektora Correct z danymi syntetycznymi. Pokazuje ciągłe fizyczne ON pomiędzy
OFF przed i po pobycie, niezależne zapisane impulsy Shadow i Current: ON w
inspektorze. Raport końcowy jest w docs/benchmarks/CORRECT_LIGHTING_0_14_207_*.json.

Full Rebuild nie jest wymagany do uruchomienia poprawki. Nie ma jeszcze
potwierdzenia na urządzeniu użytkownika ani jego istniejącym wytrenowanym modelu.

W dziesięciominutowym scenariuszu zapisany model ma 571 sprawdzanych predykcji
po początkowym okresie wejścia: zero OFF i zero brakujących obserwacji jasności,
przy utrzymujących się nowych potwierdzeniach obecności. Bez poprawki świeżości,
ale już z zachowaną jasnością, ten sam scenariusz dawał 372 błędne OFF. Wynik
opisuje ten syntetyczny model i strumień, nie model użytkownika.

Jeśli aktualizacja uruchamia aplikację przy lampie już włączonej, nie wymyślamy
odczytu sprzed ON. Retencja zacznie działać po zaobserwowaniu prawdziwego odczytu
przy zgaszonej lampie i następnego włączenia, np. przy kolejnym wyjściu i wejściu
do kuchni. Nie wymaga to ponownego treningu.

Końcowa walidacja: 2502 testy Linux Python 3.12.3 / NumPy 2.3.3, bez pominięć,
w tym 28 nowych regresji. Kompilacja modułów, składnia wszystkich dostarczanych
JS, produkcyjny entrypoint ready i chronione wywołanie Control do atrapy HA przeszły.
