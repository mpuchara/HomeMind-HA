# Bezpieczeństwo i ograniczenia

## Projekt jest eksperymentalny

Adaptive AI może bezpośrednio sterować urządzeniami Home Assistant. Traktuj Control jak nową warstwę automatyki, którą trzeba zweryfikować na każdym typie urządzenia.

## Zalecana ścieżka wdrożenia

```text
historia → TRAINING → QUALIFIED → Shadow → Verify control → Control
```

Nie pomijaj Shadow dla nowych lub przebudowanych polityk.

## Urządzenia wysokiego wpływu

Szczególną ostrożność zachowaj dla:

- ogrzewania i chłodzenia,
- bojlerów / water heater,
- zamków i urządzeń bezpieczeństwa,
- rolet w miejscach, gdzie ruch może powodować ryzyko,
- urządzeń o dużej mocy,
- urządzeń wpływających na zdrowie lub bezpieczeństwo.

Nie wszystkie takie domeny są obecnie celami Adaptive AI, ale zasada pozostaje ta sama: model historyczny nie zastępuje niezależnych zabezpieczeń fizycznych i systemowych.

## Automatyzacje HA i Control

Control może wyłączyć rozpoznaną automatyzację sterującą tą samą encją oraz zatrzymać jej trwające akcje.

Ważne:

- wyłączana jest **cała automatyzacja**, nie tylko jedna akcja,
- jeśli automatyzacja steruje kilkoma urządzeniami, pozostałe akcje również przestaną się wykonywać,
- powrót do Shadow nie przywraca jej automatycznie.

## Ręczne sterowanie

Jawna zmiana użytkownika w Home Assistant może otrzymać priorytet nad agentem przez skonfigurowany czas `manual_hold_seconds`.

Fizyczny przycisk lub zmiana bez `context.user_id` może być trudniejsza do jednoznacznego zaklasyfikowania jako ręczna interwencja.

## Model nie jest modelem fizycznym domu

Adaptive AI uczy się zależności zachowania. Nie należy zakładać, że zna pełną fizykę budynku, bezpieczeństwo instalacji albo wszystkie ograniczenia urządzenia.

## Confidence nie jest gwarancją

Nawet 95% confidence nie oznacza 95% gwarancji poprawności przyszłych decyzji. To wskaźnik oparty na historycznej separacji akcji, walidacji i podobieństwie do znanych kontekstów.

Dlatego Control posiada dodatkowe bramki support/novelty i powinien być monitorowany.

## Dane lokalne

Aplikacja przechowuje historię i modele lokalnie w swojej przestrzeni `/data`. Nie wymaga zewnętrznego serwera AI.

Przed aktualizacją lub eksperymentami z bazą wykonaj backup.
