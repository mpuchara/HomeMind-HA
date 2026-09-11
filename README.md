# HomeMind / Adaptive AI 0.7.2

## Poprawka wykonania poleceń w 0.7.2

Ta aktualizacja z 0.7.1 zachowuje model, dobór sensorów, progi oraz rewizję nauki.
Nie wymaga Rebuild ani ponownego trenowania. Nie zmieniaj poprawnie działającej predykcji.

Usuwa błąd, w którym potwierdzenie własnego polecenia z HA `user_id` stawało się
ręcznym nadpisaniem i blokowało Control na 5 minut. Zamiar polecenia jest zapisywany
przed wysłaniem, a konteksty odpowiedzi są rozpoznawane jako własne. Anonimowe zmiany
stanu nie zakładają już automatycznie ręcznego nadpisania.

Stare zapisane blokady, których pochodzenia nie można potwierdzić, nie są odtwarzane
po aktualizacji. Nowe jednoznaczne ręczne zmiany użytkownika zachowują pierwszeństwo.
Ponowne kliknięcie Control jawnie oddaje sterowanie agentowi i zwalnia ręczną blokadę.

Osiem niezależnych wykonawców obsługuje różne encje; każde urządzenie ma najwyżej jedno
zadanie w toku. Polling HA odbywa się osobno. Starszy odczyt REST lub zdarzenie nie
nadpisuje nowszego stanu. Światło może zastąpić niepotwierdzone przeciwne polecenie
najnowszą decyzją, nadal respektując skonfigurowany minimalny odstęp i czas stabilizacji.
Control nadal wyłącza rozpoznane automatyzacje i zachowuje swoje pozostałe bramki.

Test wykonawczy sprawdza 20 cykli ON/OFF z `user_id` w potwierdzeniach (40 poleceń,
bez fałszywych ręcznych blokad). Nie jest to pomiar opóźnienia fizycznych lamp w HA.
W diagnostyce są źródło ostatniej zmiany, czas wywołania HA oraz czas potwierdzenia.


Local Home Assistant app that learns preferred device settings from Recorder history and
live human corrections. Shared control lifecycle, device-specific settling times, calibrated
action confidence, and direct Home Assistant service calls.

New in 0.7: immediate manual demonstrations in Shadow, persistent manual priority, separate
acknowledgement and settling, timing/settings editor, device-step-aware limits, strict cooldowns,
causal historical snapshots, action-specific validation, and readable Python sources.

See [Polish installation guide](INSTALACJA_PL.md), [release notes](RELEASE_0_7.md),
[architecture](adaptive_ai/ARCHITECTURE.md) and [test report](TEST_REPORT.md).

This is a Supervisor app, not a custom_components integration. New agents start in Shadow.
Keep existing data when upgrading. A locally installed copy does not automatically inherit
the data of an app installed from a GitHub repository.

Run regression tests with `python -m unittest discover -s tests -v`.
The learner is a contextual bandit with delayed preference feedback, not a learned thermal
plant or a guarantee that all automations can safely be replaced.
