# Szybki start

Ten scenariusz pozwala bezpiecznie przejść od instalacji do pierwszego agenta w Control.

## 1. Nie wyłączaj istniejących automatyzacji

Na początku Adaptive AI ma nauczyć się zachowania, które już działa w domu. Dotychczasowe automatyzacje są więc wartościowym benchmarkiem.

Podczas **TRAINING** i **Shadow** zostaw je aktywne.

## 2. Poczekaj na indeksację historii

Na górze UI znajduje się panel historii. Obserwuj:

- postęp indeksacji,
- liczbę zarchiwizowanych zmian,
- liczbę encji,
- zakres dni historii,
- fazę pracy.

Agenci podczas pełnego przejścia historii mają stan **TRAINING**. W tym czasie nie wykonują normalnego sterowania realtime.

## 3. Sprawdź wynik agenta

Po dojściu do bieżących danych:

- **Behaviour benchmark >78%** i wystarczająca liczba próbek → `QUALIFIED`, agent trafia do `Shadow`,
- wynik niższy lub za mało danych → `PAUSED`.

Dla urządzeń binarnych, np. światła, benchmark jest zbalansowany między ON i OFF. Model nie może zdać tylko dlatego, że przez większość czasu światło jest wyłączone.

## 4. Otwórz szczegóły agenta

Najważniejsze pola:

- **Candidate confidence** – końcowy historyczny benchmark przed kwalifikacją,
- **Behaviour benchmark** – wynik odwzorowania zachowania historycznego,
- **Per-action benchmark** – osobna skuteczność ON i OFF,
- **Selected context** – encje faktycznie używane przez politykę,
- **Primary behavioural driver** – najsilniejszy historyczny sygnał,
- **Context candidates screened** – ilu kandydatów sprawdzono i ilu wybrano,
- **Historical cursor** – dokąd dotarła indeksacja.

Dla światła sterowanego np. radarem obecności oczekuj, że właściwy `binary_sensor...presence` pojawi się w **Selected context**.

## 5. Obserwuj Shadow

Shadow:

- wykonuje inference,
- pokazuje `Current` i `Desired`,
- nie wysyła komend do urządzenia.

Porównaj zachowanie Desired z dotychczasową automatyką przez kilka rzeczywistych cykli.

Przykład światła:

```text
presence OFF → ON
       ↓
Desired OFF → ON
       ↓
stara automatyzacja włącza światło
       ↓
Current OFF → ON
```

Przy wyjściu analogicznie sprawdź OFF.

## 6. Verify control

Przycisk **Verify control** wysyła do urządzenia jego aktualną wartość przez tę samą ścieżkę usług HA, której używa Control.

Celem jest sprawdzenie dostępu do usługi bez intencjonalnej zmiany nastawy.

Sprawdź:

- `Last HA service`,
- `Czas wywołania HA`,
- `Potwierdzenie urządzenia`,
- brak błędu usługi.

## 7. Włącz Control

Włącz dopiero gdy:

- agent jest `QUALIFIED`,
- benchmark jest wiarygodny,
- Selected context ma sens,
- Shadow odpowiada oczekiwanemu zachowaniu,
- Verify control działa.

**Uwaga:** Control próbuje wyłączyć rozpoznane automatyzacje HA sterujące tą samą encją i zatrzymać ich bieżące akcje. Wyłączona może zostać cała automatyzacja, również gdy steruje kilkoma urządzeniami. Powrót do Shadow nie włącza jej automatycznie ponownie.
