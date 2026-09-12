# Jak działa Adaptive AI – krok po kroku

## 1. Home Assistant jest źródłem stanu i historii

Adaptive AI korzysta z dwóch ścieżek:

- **bieżący stan/realtime** – zdarzenia `state_changed` przez WebSocket,
- **historia** – Recorder Home Assistant.

Dane są kopiowane do lokalnej bazy Adaptive AI, dzięki czemu późniejsze przebudowy modeli nie muszą za każdym razem odpytywać Recordera o wszystko od zera.

## 2. Wykrywane są urządzenia, którymi można sterować

Aktualnie wspierane są m.in. światła, przełączniki, climate, rolety, wentylatory, number/input_number, media_player, humidifier, water_heater i select/input_select.

Dla jednej encji może istnieć więcej niż jedna potencjalna właściwość, np. dla światła ON/OFF i jasność.

## 3. Budowany jest szeroki candidate pool kontekstu

Wersja 0.7.12 stosuje zasadę **broad context**: prawie każda parsowalna encja HA może udowodnić, że jest predykcyjna.

Przykłady dopuszczonych kandydatów:

- `binary_sensor` presence/motion,
- ESPHome LD2411: Presence, Still Energy %, Move Energy %, odległości,
- camera / AI score bez jednostki elektrycznej,
- dane telefonu,
- `person` i `device_tracker`,
- samochód,
- pogoda,
- sun,
- helpery i template sensors,
- wirtualne encje,
- nietypowe własne sensory,
- kalendarz lub inne stany, jeżeli można je sparsować,
- cechy czasu generowane przez aplikację.

### Twarde wykluczenia

#### A. Sterowalne urządzenia

Aktuator nie może być wejściem innego agenta. Adaptive AI wyklucza sterowalne encje oraz encje powiązane z wykrytym urządzeniem sterowalnym, aby uniknąć skrótów typu:

`light.kitchen ON → prawdopodobnie light.stairs ON`.

Model ma uczyć się przyczyny, a nie kopiować stan innego aktuatora.

#### B. Telemetria elektryczna po jednostce

Wykluczana jest pojedyncza encja, gdy `unit_of_measurement` jednoznacznie oznacza pomiar elektryczny, np.:

- V, mV, kV,
- A, mA,
- W, kW,
- VA,
- var,
- Wh, kWh,
- Ah,
- Hz,
- ohm.

Nazwa encji nie ma znaczenia. Przykładowo:

- `Still Energy` z `%` → **dozwolone**,
- `camera power score` bez `W` → **dozwolone**,
- `sensor.x` z jednostką `W` → **wykluczone**.

Nie jest wykluczane całe urządzenie tylko dlatego, że jedna z jego encji raportuje W/V/A.

## 4. Aplikacja wybiera mały zestaw najbardziej użytecznych wejść

Szeroki candidate pool jest używany głównie w trakcie indeksacji. Do pracy realtime nie trafiają setki encji.

Model wybiera najbardziej predykcyjne sygnały dla konkretnego targetu.

W UI:

- `Context candidates screened` = ile encji rozważono,
- `Selected context` = co naprawdę weszło do modelu.

## 5. Dla szybkich świateł liczy się krótki szereg czasowy

Dla binarnych świateł/switchy agent utrzymuje kompaktowy kontekst, domyślnie maksymalnie około 8 najważniejszych encji.

Dla każdej istotnej cechy model może uwzględniać:

```text
wartość teraz
zmiana względem ~1 s
zmiana względem ~3 s
zmiana względem ~10 s
```

Dzięki temu rozróżnia np. świeże wejście do pokoju od obecności trwającej już długo.

## 6. Wykrywany jest behavioural driver

Adaptive AI analizuje, które zmiany kontekstu historycznie poprzedzały przełączenia targetu.

Przykład:

```text
kitchen_presence OFF → ON
              150 ms
IKEA light     OFF → ON

kitchen_presence ON → OFF
                ...
IKEA light      ON → OFF
```

Jeżeli relacja jest powtarzalna, sensor może zostać oznaczony jako **Primary behavioural driver** i dostać zarezerwowane miejsce w kontekście, nawet jeśli nazwa lub `area_id` nie są idealnie dopasowane.

## 7. Automatyzacje HA są wskazówką i benchmarkiem

Adaptive AI skanuje istniejące automatyzacje, aby poznać:

- jakie encje były triggerami,
- jakie encje występowały w warunkach,
- które automatyzacje sterowały targetem.

Nie kopiuje automatyzacji 1:1. Są one structural prior / wskazówką do szukania istotnego kontekstu.

Ostateczny benchmark jest porównywany przede wszystkim z **rzeczywistym zachowaniem targetu w historii**. Dzięki temu sterowanie przez grupę, script lub device target nie powinno zerować wyniku tylko dlatego, że trudno przypisać każdą zmianę do jednej automatyzacji.

## 8. Offline contextual RL

Dla kolejnych fragmentów historii powstają przykłady:

```text
kontekst → akcja/stan targetu → ocena
```

Model jest lokalną polityką contextual RL opartą o rodzinę LinUCB. Uczy się przewidywać stan/nastawę, która historycznie była akceptowana w podobnym kontekście.

Historyczny reward bierze pod uwagę m.in. czas utrzymania stanu i szybkie korekty użytkownika.

## 9. Benchmark na danych chronologicznie odłożonych

Model nie powinien oceniać sam siebie na tych samych przykładach, na których właśnie się nauczył. Dlatego część danych jest traktowana jako chronologiczny held-out benchmark.

Dla binarnych urządzeń benchmark jest zbalansowany między ON i OFF.

Domyślnie kandydat musi:

- mieć co najmniej 12 próbek benchmarkowych,
- mieć pokrycie obu klas dla binarnego celu,
- uzyskać **więcej niż 78%**.

## 10. Kwalifikacja

```text
pełna historia → bieżące dane
           ↓
Behaviour benchmark
           ↓
  >78%              ≤78%
    ↓                  ↓
QUALIFIED            PAUSED
    ↓
  Shadow
```

PAUSED ogranicza zużycie CPU przez słabe modele.

## 11. Realtime inference

Dla zakwalifikowanego agenta zmiana wybranej encji kontekstu wybudza inference niemal natychmiast. Domyślny debounce to 25 ms.

Cel dla szybkiego światła nie polega na przewidywaniu ruchu kilkanaście sekund w przyszłość. Chodzi o reakcję na **pierwszy sygnał przyczynowy**, np. radar obecności, wystarczająco szybko, aby światło włączyło się przed ręcznym naciśnięciem włącznika.

## 12. Kalibracja Live confidence

Bieżący confidence nie pochodzi wyłącznie z matematycznej przewagi jednej akcji nad drugą.

Aplikacja pokazuje m.in.:

- `Structural confidence`,
- held-out backtest accuracy,
- confidence ceiling,
- support,
- novelty.

Finalne Live confidence jest ograniczane przez historyczną kalibrację, aby model nie pokazywał np. 95% tylko dlatego, że jego wewnętrzny score jest mocny.

## 13. Control

W Control przed wysłaniem komendy sprawdzane są m.in.:

- kwalifikacja agenta,
- confidence,
- history support,
- novelty,
- cooldown/action interval,
- pending feedback i timing urządzenia,
- konflikt z automatyzacjami,
- priorytet ręcznej zmiany użytkownika.

Dopiero wtedy wywoływana jest usługa Home Assistant.
