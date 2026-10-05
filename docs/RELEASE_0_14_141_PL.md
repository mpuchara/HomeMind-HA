# Adaptive AI 0.14.141 — jakość nauki historycznej

Agent traktuje historię automatyzacji jako demonstracje o ograniczonej wiarygodności.
Powtarzalność jest oceniana w podobnym kontekście wybranych sensorów, z ograniczeniem
głosów z jednego dnia. Rzadkie jawne preferencje użytkownika zachowują pełną wagę.

Nowa ocena skutków dla binarnych świateł chroni przed wyłączeniem przy potwierdzonej
obecności i osłabia błędne włączenia przy zweryfikowanej nieobecności w całym okresie.
Brak ruchu, brak danych oraz sprzeczność sensorów nie dowodzą nieobecności.
Sensory wykorzystywane do oceny obecności muszą mieć zgodne przypisanie do obszaru
HA; źródła z innych pomieszczeń nie potwierdzają potrzeby ani nieobecności.

Ręczna zmiana światła po krótkim okresie może oznaczać korektę lub nową potrzebę.
Do domyślnie 90 s nie nagradzamy poprzedniego automatycznego stanu, jeśli nie da się
rozstrzygnąć tych znaczeń. Bardzo szybka korekta pozostaje sygnałem negatywnym.

## Aktualizacja i instalacja

W Home Assistant odśwież repozytorium dodatków
https://github.com/mpuchara/HomeMind-HA i zaktualizuj Adaptive AI do 0.14.141.
Wydanie zawiera też dwa ZIP-y:

- `repository.zip`: pełne repozytorium, w tym katalog `adaptive_ai`;
- `addon-root.zip`: pliki dodatku bez dodatkowego katalogu nadrzędnego, do lokalnego
  katalogu `/addons/adaptive_ai/`.

Przed aktualizacją wykonaj kopię dodatku i danych HA. Zachowaj istniejący katalog
danych; nie odinstalowuj dodatku w celu zastosowania zasad treningu.
Po aktualizacji uruchom ręczny Train/Rebuild wybranych agentów i oceń je w Shadow.
Istniejący model nie zmieni wiedzy wyłącznie przez aktualizację programu.
Nie włączamy automatycznie Control ani pełnego treningu podczas restartu.

## Ustawienia

- `training_pattern_weighting_enabled`: domyślnie `true`;
- `training_pattern_min_days`: domyślnie `3`, zakres 1–14;
- `training_light_outcome_enabled`: domyślnie `true`;
- `historical_light_ambiguous_override_seconds`: domyślnie `90`, zakres 8–300.

Wagi wiarygodności startowej: użytkownik 1.0, rozpoznana automatyzacja 0.5,
nieznane źródło 0.25. Sygnały upstream są dodatkowo mnożone przez 0.35.
Waga mniejszości w powtarzalnym kontekście ma dolny limit 0.2.
Są to jawne założenia początkowe, nie optymalne wagi wyznaczone dla konkretnego domu.
Diagnostyka modelu zawiera `training_quality`, a audyt treningu sekcję `quality`.

## Walidacja i granice wyniku

Testy jednostkowe obejmują przyczynowość, kontynuację pamięci, ograniczenia RAM,
różne źródła dowodów, sprzeczne sensory, braki obserwacji i zachowanie wyjątków.
Test rzeczywistego izolowanego workera zapisuje Ridge i TinyMLP z nową oceną
niejasnego zdarzenia. CI obejmuje Python 3.11/3.13, obraz dodatku i istniejące
benchmarki runtime, wraz z nowym benchmarkiem jakości.

Nowy syntetyczny benchmark wykonuje 15 niezależnych porównań z wadliwą automatyzacją
na pięciu scenariuszach i trzech seedach. W każdym sprawdza brak regresji fałszywych
włączeń i przedwczesnego OFF, a nie tylko średni wynik. Używa produkcyjnej matematyki
Ridge i oceny dowodów, jawnych korekt oraz reprezentacji cech z interakcjami.
To test komponentów uczenia, nie pełnego Executor ani realnej instalacji.
Historyczna accuracy i proxy Offline RL nadal nie są dowodem przewagi w domu.
