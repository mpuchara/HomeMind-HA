# Testy wydania 0.11.0

208 testów unittest. [Pełny wynik](docs/TEST_RESULTS_0_11_0.txt) · [Opis wydania](docs/RELEASE_0_11_0.md) · [Audyt ręcznej nauki RL](docs/AUDIT_MANUAL_LEARNING_0_11_0.md).

Nowe scenariusze: efektywna korekta po 2000 przeciwnych próbek, cofanie bez utraty niezależnej nauki, zachowanie po restarcie/treningu, Shadow bez usług, Control przez Executor, unieważnianie intentu po undo, kwalifikacja Control, historyczne as-of bez przyszłego kontekstu, gęste radary, temperatury i zmiana opcji, odczyt live podczas transakcji SQLite, start kart przy zablokowanym ciężkim statusie.

Lokalna przeglądarka potwierdziła cztery akcje, Settings, wykres, powiększanie, wybór momentu, naukę i cofanie. Polecenia urządzeń są symulowane. Kontrole pakietu obejmują Python, wszystkie JS, symulator, mikrobenchmark, wersje i integralność ZIP. Nie testowano lokalnie Docker ani fizycznego HA.
