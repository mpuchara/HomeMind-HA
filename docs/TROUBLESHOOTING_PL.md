# Diagnostyka i rozwiązywanie problemów

## Agent ma niski benchmark

Najpierw rozwiń **Control diagnostics & learning**.

Sprawdź kolejno:

1. `Selected context` – czy jest tam sensor, który faktycznie sterował urządzeniem?
2. `Primary behavioural driver` – czy wskazuje logiczną przyczynę?
3. `Per-action benchmark` – problem dotyczy ON, OFF czy obu?
4. `Benchmark samples` – czy liczba próbek jest wystarczająca?
5. `Context candidates screened` – czy aplikacja w ogóle rozważyła szeroki zbiór wejść?
6. `Electrical-unit inputs excluded` – czy filtr nie usunął ważnej encji?
7. `Controllable-device inputs excluded` – czy wejście nie jest częścią aktuatora?

### Przykład: światło IKEA i radar LD2411

Dla prostego układu, w którym `binary_sensor.kitchen_presence` sterował żarówką, oczekuj obecności tej encji w `Selected context` i wysokiej wartości w behavioural driver.

`Still Energy` lub `Move Energy` z jednostką `%` są dozwolone. Słowo `energy` w nazwie nie powoduje wykluczenia.

## Ważny sensor nie trafia do Selected context

1. upewnij się, że encja ma historię w Recorder,
2. upewnij się, że jej stan jest parsowalny,
3. sprawdź jej `unit_of_measurement`,
4. jeśli sensor został dodany niedawno – użyj **Rebuild**, nie Resume,
5. sprawdź `Context candidates screened`,
6. opcjonalnie ustaw go ręcznie w `Encje kontekstu` razem z innymi ważnymi wejściami i przebuduj agenta.

## Sensor ma nazwę Energy/Power, ale nie jest elektryczny

W 0.7.12 nazwa nie jest filtrem.

Przykłady, które powinny zostać dopuszczone:

- LD2411 `Still Energy` `%`,
- LD2411 `Move Energy` `%`,
- camera score bez jednostki,
- AI detection `points`,
- template sensor o nazwie `power_score` bez `W/VA/...`.

Wykluczenie następuje po elektrycznej jednostce pomiaru.

## Shelly 3EM

Encje raportujące `W`, `V`, `A`, `Wh/kWh`, `VA` itd. są ignorowane jako wejścia do uczenia.

Inna, nieelektryczna encja tego samego urządzenia może pozostać kandydatem, jeśli sama nie jest aktuatorem i ma użyteczny stan.

## Agent kończy PAUSED

To oczekiwane, gdy:

- benchmark ≤78%,
- jest za mało próbek,
- dla binarnego targetu nie ma wystarczającego pokrycia ON i OFF.

Jeżeli po kilku dniach pojawiły się nowe dane – użyj **Resume**.

Jeżeli zmieniły się sensory – użyj **Rebuild**.

## Rebuild za każdym razem zaczyna od zera

Tak ma działać. Rebuild oznacza pełny reset kursora i modelu danego agenta.

Jeśli chcesz kontynuować od dotychczasowego punktu, użyj **Resume**.

## CPU jest wysokie

Podczas pełnego historycznego indeksowania to może być normalne.

Po zakończeniu:

- słabi agenci przechodzą do PAUSED,
- tylko zakwalifikowane polityki są normalnie utrzymywane,
- realtime używa małego Selected context, a nie całego domu.

Jeżeli CPU pozostaje wysokie po zakończeniu wszystkich treningów:

- sprawdź, czy nie trwa `Rebuild`,
- sprawdź fazę w panelu historii,
- sprawdź liczbę QUALIFIED agentów,
- przejrzyj logi aplikacji.

## Desired jest poprawne, ale urządzenie reaguje wolno

Sprawdź osobno:

- `Czas wywołania HA`,
- `Potwierdzenie urządzenia`,
- sieć Zigbee/Thread/Wi-Fi,
- timing urządzenia,
- action interval,
- settling time.

Model może podjąć decyzję szybko, ale fizyczne urządzenie może ją potwierdzić później.

## Desired jest opóźnione

Sprawdź:

- `Last context trigger`,
- czy właściwy sensor jest w `Selected context`,
- czy agent jest QUALIFIED, a nie TRAINING/PAUSED,
- czy WebSocket realtime jest aktywny,
- czy fast agent używa krótkich serii 1/3/10 s.

## Control jest niedostępny

Control wymaga:

- zakończonego pełnego benchmarku,
- stanu QUALIFIED,
- wyniku >78%.

Jeżeli agent jest PAUSED, użyj Resume lub Rebuild zależnie od przyczyny.

## Konflikt z automatyzacją

Karta pokaże ostrzeżenie, jeśli istnieje aktywna automatyzacja sterująca tą samą encją.

Przejście do Control może wyłączyć całą taką automatyzację. Przed zatwierdzeniem sprawdź, czy nie obsługuje ona także innych urządzeń.

## Po przejściu z Control do Shadow stara automatyzacja nie działa

To zachowanie jest zamierzone: Shadow nie włącza ponownie automatyzacji wyłączonych podczas przejęcia Control.

Włącz je ręcznie w Home Assistant, jeśli chcesz wrócić do starego sterowania.
