# Dokumentacja Adaptive AI 0.7.12

Dokumentacja użytkownika dla HomeMind Adaptive AI w wersji **0.7.12**.

## Zacznij tutaj

1. [Instalacja i aktualizacja](INSTALLATION_PL.md)
2. [Szybki start](QUICK_START_PL.md)
3. [Obsługa aplikacji i agentów](USER_GUIDE_PL.md)
4. [Jak działa Adaptive AI krok po kroku](HOW_IT_WORKS_PL.md)
5. [Lifecycle agentów: TRAINING → SHADOW / PAUSED](AGENT_LIFECYCLE_PL.md)
6. [Ustawienia i parametry](SETTINGS_REFERENCE_PL.md)
7. [Diagnostyka i rozwiązywanie problemów](TROUBLESHOOTING_PL.md)
8. [Bezpieczeństwo i ograniczenia](SAFETY_AND_LIMITATIONS_PL.md)

## Dokumentacja techniczna

- [Architektura](../ARCHITECTURE.md)
- [Changelog](../adaptive_ai/CHANGELOG.md)
- [Release notes 0.7](../RELEASE_0_7.md)
- [Raport testów](../TEST_REPORT.md)

## Najważniejszy workflow

```text
Home Assistant Recorder + bieżące stany
                ↓
             TRAINING
                ↓
      pełny benchmark historii
                ↓
        >78%          ≤78%
          ↓              ↓
      QUALIFIED        PAUSED
          ↓              ↓
        Shadow        brak bieżącego
          ↓           treningu/inference
   Verify control         ↓
          ↓             Resume
       Control
```

`Resume` kontynuuje od zapisanego kursora. `Rebuild` zeruje model danego agenta i wykonuje pełną indeksację od początku; używaj go m.in. po dodaniu ważnego nowego sensora.
