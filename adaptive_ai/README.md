# HomeMind-HA / Adaptive AI 0.14.168

0.14.168: dodatkowy czujnik jasności i osobna preferencja nowych włączeń przez Explore → Candidate → Shadow. Próg podaje użytkownik; zmiana nie gasi już zapalonego światła.


Cztery akcje każdego agenta: Shadow/Control, Wrong decision, Settings, Teach.

Wrong decision zmienia nauczone Desired. Teach pozwala uczyć na historii: powiększ wykres, wybierz moment i podaj prawidłowy stan. Cofnij ostatnią naukę w Teach lub Settings. W Shadow nie ma poleceń; w Control poprawione Desired steruje przez Executor.

Dawne Desired jest oznaczoną rekonstrukcją obecnym modelem. Zapis rzeczywistych predykcji rozpoczyna się od 0.11.0. Modele, archiwum i ustawienia pozostają zgodne.

[Obsługa](DOCS.md) · [Zmiany](CHANGELOG.md)
