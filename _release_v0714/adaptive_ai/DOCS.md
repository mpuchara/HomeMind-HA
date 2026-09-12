# Adaptive AI — instrukcja użytkownika 0.7.12

Adaptive AI uczy się na historii Home Assistant Recorder i bieżącym kontekście domu. Po pełnym treningu agent jest kwalifikowany na podstawie historycznego benchmarku zachowania.

## Pierwsze uruchomienie

1. **Nie wyłączaj istniejących automatyzacji.** Są punktem odniesienia dla uczenia.
2. Poczekaj na zakończenie `TRAINING` i dojście kursora do danych bieżących.
3. Otwórz kartę agenta i sprawdź:
   - `Behaviour benchmark`,
   - `Candidate confidence`,
   - `Per-action benchmark`,
   - `Selected context`,
   - `Primary behavioural driver`,
   - `Context candidates screened`.
4. Agent z benchmarkiem **>78%** i wystarczającą liczbą próbek przechodzi do `QUALIFIED + Shadow`.
5. Słabszy agent przechodzi do `PAUSED` i nie zużywa normalnie CPU na bieżący trening/inference.
6. Obserwuj `Desired` w Shadow i porównaj z zachowaniem istniejącej automatyki.
7. Uruchom `Verify control`.
8. Dopiero po weryfikacji przełącz wybrany agent do `Control`.

## Resume i Rebuild

- **Resume** — kontynuuje uczenie od zapisanego kursora; nie czyści modelu ani dotychczasowych doświadczeń.
- **Rebuild** — zeruje model i benchmark danego agenta, odświeża potrzebną historię i przechodzi ją od początku. Użyj np. po dodaniu nowego sensora, który powinien mieć wpływ na politykę.

## Jakie encje mogą być wejściem

Domyślnie kandydatem może być praktycznie każda parsowalna encja HA, m.in. presence/motion, ESPHome radar, camera/AI score, dane telefonu, `person`, `device_tracker`, samochód, pogoda, helpery, template sensors i encje wirtualne.

Wykluczane są aktuatory oraz encje z jednoznacznie elektryczną jednostką (`W`, `V`, `A`, `VA`, `var`, `Wh`, `kWh`, `Ah`, `Ω` itd.). Filtr działa po jednostce, nie po nazwie: `Still Energy` w `%` pozostaje dozwolony.

## Pełna dokumentacja

- [Instalacja i aktualizacja](../docs/INSTALLATION_PL.md)
- [Szybki start](../docs/QUICK_START_PL.md)
- [Obsługa aplikacji](../docs/USER_GUIDE_PL.md)
- [Jak działa Adaptive AI](../docs/HOW_IT_WORKS_PL.md)
- [Lifecycle agentów](../docs/AGENT_LIFECYCLE_PL.md)
- [Ustawienia](../docs/SETTINGS_REFERENCE_PL.md)
- [Troubleshooting](../docs/TROUBLESHOOTING_PL.md)
- [Bezpieczeństwo i ograniczenia](../docs/SAFETY_AND_LIMITATIONS_PL.md)
