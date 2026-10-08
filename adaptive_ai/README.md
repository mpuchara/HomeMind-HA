# HomeMind-HA / Adaptive AI 0.14.147

0.14.147: zbiorczy zapis obserwacji Sensor Tournament, przeliczanie ocen tylko dla nowych przykładów oraz pomiary całej obsługi decyzji. Aktualizacja nie wymaga Rebuild.


Cztery akcje każdego agenta: Shadow/Control, Wrong decision, Settings, Teach.

Wrong decision zmienia nauczone Desired. Teach pozwala uczyć na historii: powiększ wykres, wybierz moment i podaj prawidłowy stan. Cofnij ostatnią naukę w Teach lub Settings. W Shadow nie ma poleceń; w Control poprawione Desired steruje przez Executor.

Dawne Desired jest oznaczoną rekonstrukcją obecnym modelem. Zapis rzeczywistych predykcji rozpoczyna się od 0.11.0. Modele, archiwum i ustawienia pozostają zgodne.

[Obsługa](DOCS.md) · [Zmiany](CHANGELOG.md)
