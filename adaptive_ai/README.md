# HomeMind-HA / Adaptive AI 0.14.152

0.14.152: zapis modelu domu nie blokuje sensorów; obserwacje puli i jakości kontekstu oraz nauka kandydatów korzystają z zapisu w tle. Bez Rebuild. Przy restarcie oczekujące dane są zapisywane.


Cztery akcje każdego agenta: Shadow/Control, Wrong decision, Settings, Teach.

Wrong decision zmienia nauczone Desired. Teach pozwala uczyć na historii: powiększ wykres, wybierz moment i podaj prawidłowy stan. Cofnij ostatnią naukę w Teach lub Settings. W Shadow nie ma poleceń; w Control poprawione Desired steruje przez Executor.

Dawne Desired jest oznaczoną rekonstrukcją obecnym modelem. Zapis rzeczywistych predykcji rozpoczyna się od 0.11.0. Modele, archiwum i ustawienia pozostają zgodne.

[Obsługa](DOCS.md) · [Zmiany](CHANGELOG.md)
