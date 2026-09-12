# Adaptive AI 0.8.0

Adaptive AI to lokalna aplikacja Home Assistant, która uczy się historycznego sposobu sterowania urządzeniami i po kwalifikacji może bezpośrednio wykonywać usługi Home Assistant. Nie generuje automatyzacji YAML i nie wymaga zewnętrznego API AI.

> **Status: experimental.** Najpierw używaj trybu **Shadow**. Tryb **Control** włączaj dopiero po sprawdzeniu benchmarku, wybranych wejść modelu oraz `Verify control`.

## Jak zacząć

1. Zainstaluj aplikację zgodnie z [instrukcją instalacji](../docs/INSTALLATION_PL.md).
2. Pozostaw istniejące automatyzacje aktywne podczas uczenia.
3. Wybierz konkretnego agenta i uruchom **Train**. W trybie low-memory tylko jeden agent trenuje się jednocześnie.
4. Wynik **Behaviour benchmark >78%** kwalifikuje agenta do `QUALIFIED + Shadow`; słabszy agent przechodzi do `PAUSED`.
5. Sprawdź `Selected context` i `Primary behavioural driver`.
6. Uruchom `Verify control`.
7. Dopiero po obserwacji w Shadow przełącz wybrany agent do Control.

## Kontekst uczenia

Wersja 0.8.0 pozwala praktycznie każdej parsowalnej encji Home Assistant konkurować jako kandydat na wejście modelu: presence, radar, kamera/AI score, telefon, osoby, samochód, pogoda, helpery, template sensors, encje wirtualne i inne dane dostępne w HA.

Twardo wykluczane są:

- bezpośrednio sterowalne aktuatory; sensory ESPHome (`sensor`/`binary_sensor`) pozostają kandydatami nawet wtedy, gdy współdzielą urządzenie z encjami konfiguracyjnymi lub aktuatorami,
- pojedyncze encje z jednoznacznie elektryczną `unit_of_measurement`, np. `W`, `V`, `A`, `VA`, `var`, `Wh`, `kWh`, `Ah`, `Ω`.

Sama nazwa `energy` lub `power` **nie** powoduje wykluczenia. Przykładowo radarowe `Still Energy` w `%` pozostaje dopuszczonym kandydatem.

## Dokumentacja

- [Pełny indeks dokumentacji](../docs/README.md)
- [Szybki start](../docs/QUICK_START_PL.md)
- [Obsługa agentów](../docs/USER_GUIDE_PL.md)
- [Jak działa model](../docs/HOW_IT_WORKS_PL.md)
- [Lifecycle TRAINING / SHADOW / PAUSED](../docs/AGENT_LIFECYCLE_PL.md)
- [Troubleshooting](../docs/TROUBLESHOOTING_PL.md)
- [Bezpieczeństwo](../docs/SAFETY_AND_LIMITATIONS_PL.md)
