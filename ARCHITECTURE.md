# Architektura 0.10.0

Aktualny opis modułów, modelu domu, intencji, wykonania, uczenia i migracji: [docs/ARCHITECTURE_0_9.md](docs/ARCHITECTURE_0_9.md).

Warstwa 0.10: [eksperymenty kontekstowe](docs/EXPERIMENTS_PL.md). `experiments.py` utrzymuje osobny diagonalny bandyta ze stabilnymi nazwami cech, trwałe wyniki i limity. `Engine` przygotowuje próby i obserwuje wyniki; `Executor` weryfikuje token i rezerwuje budżet przed wywołaniem HA. Historyczna polityka i jej schemat pozostają zgodne z 0.9.2. Wykonanie nadal rozpoczyna `fast_queue_main.py`, zachowując kolejkę FIFO i szybki profil urządzeń z aktualnego `main`.
