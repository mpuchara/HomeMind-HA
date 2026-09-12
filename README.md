# HomeMind Adaptive AI for Home Assistant

**Dokumentacja dla wersji 0.7.12**

Adaptive AI to lokalna aplikacja Home Assistant, która uczy się sposobu sterowania urządzeniami na podstawie historii z Recorder, bieżącego kontekstu domu i późniejszych korekt użytkownika. Nie generuje automatyzacji YAML. Po zakwalifikowaniu agenta może bezpośrednio wywoływać usługi Home Assistant i sterować urządzeniem.

> **Status:** experimental. Najpierw używaj trybu **Shadow** i porównaj zachowanie agenta z dotychczasowymi automatyzacjami. Tryb **Control** włączaj dopiero po weryfikacji modelu i ścieżki sterowania.

## Najważniejsze cechy

- działa lokalnie, bez chmury i bez zewnętrznego API AI,
- importuje historię z Home Assistant Recorder,
- automatycznie wykrywa aktywnie używane urządzenia sterowalne,
- traktuje istniejące automatyzacje HA jako ważny punkt odniesienia i źródło kontekstu,
- może rozważać praktycznie wszystkie parsowalne encje HA jako wejścia modelu,
- wyklucza wejścia należące do urządzeń sterowalnych oraz encje z jednoznacznie elektrycznymi jednostkami, np. `W`, `V`, `A`, `VA`, `Wh`, `kWh`,
- dla szybkich urządzeń, np. świateł, używa krótkich szeregów czasowych, domyślnie około `1 s / 3 s / 10 s`,
- po pełnym przejściu historii kwalifikuje tylko agentów z **Behaviour benchmark > 78%**,
- słabsi agenci przechodzą w **PAUSED**, aby ograniczyć CPU,
- `Resume` kontynuuje naukę od zapisanego kursora,
- `Rebuild` wykonuje pełną przebudowę agenta od początku i powinien być używany np. po dodaniu nowego sensora.

## Dokumentacja

1. [Instalacja i aktualizacja](docs/INSTALLATION_PL.md)
2. [Szybki start](docs/QUICK_START_PL.md)
3. [Obsługa aplikacji i agentów](docs/USER_GUIDE_PL.md)
4. [Jak działa Adaptive AI krok po kroku](docs/HOW_IT_WORKS_PL.md)
5. [Lifecycle: TRAINING → SHADOW / PAUSED](docs/AGENT_LIFECYCLE_PL.md)
6. [Ustawienia i parametry](docs/SETTINGS_REFERENCE_PL.md)
7. [Diagnostyka i rozwiązywanie problemów](docs/TROUBLESHOOTING_PL.md)
8. [Bezpieczeństwo, ograniczenia i zasady Control](docs/SAFETY_AND_LIMITATIONS_PL.md)

## Obsługiwane cele sterowania

| Domena HA | Sterowana właściwość |
|---|---|
| `light` | ON/OFF, jasność |
| `switch` | ON/OFF |
| `input_boolean` | ON/OFF |
| `climate` | temperatura zadana |
| `cover` | pozycja |
| `fan` | ON/OFF, procent prędkości |
| `number`, `input_number` | wartość |
| `media_player` | ON/OFF, głośność |
| `humidifier` | wilgotność zadana |
| `water_heater` | temperatura zadana |
| `select`, `input_select` | wybrana opcja |

Obsługa danej encji zależy również od tego, jakie atrybuty i usługi faktycznie udostępnia integracja Home Assistant.

## Repozytorium

Repozytorium aplikacji:

`https://github.com/mpuchara/HomeMind-HA`

Oficjalne informacje o aplikacjach Home Assistant:

- https://www.home-assistant.io/apps/
- https://www.home-assistant.io/common-tasks/os/#installing-a-third-party-app-repository

## W skrócie: jak zacząć

1. Dodaj repozytorium do **Settings → Apps → Install app → ⋮ → Repositories**.
2. Zainstaluj **Adaptive AI** i uruchom aplikację.
3. Pozostaw dotychczasowe automatyzacje HA aktywne.
4. Poczekaj, aż aplikacja zaimportuje historię i agenci zakończą **TRAINING**.
5. Otwórz kartę agenta i sprawdź **Behaviour benchmark**, **Selected context** oraz **Primary behavioural driver**.
6. Agent z wynikiem **>78%** przechodzi do **QUALIFIED + Shadow**. Słabszy przechodzi do **PAUSED**.
7. Dla dobrego agenta użyj **Verify control**.
8. Dopiero po obserwacji w Shadow włącz **Control**.


## Dokumentacja techniczna

- [Architektura](ARCHITECTURE.md)
- [Changelog](adaptive_ai/CHANGELOG.md)
- [Release notes 0.7](RELEASE_0_7.md)
- [Raport testów](TEST_REPORT.md)
