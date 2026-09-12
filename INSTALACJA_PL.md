# Instalacja 0.7.12

Zaktualizuj App bez usuwania `/data`. Zmiana `TRAINING_REVISION` wykona jednorazowe odświeżenie historii wszystkich dopuszczonych encji kontekstowych, a następnie przebuduje modele z zachowaniem workflow TRAINING → SHADOW (>78%) / PAUSED.

Po zakończeniu indeksacji rozwiń agenta i sprawdź `Context candidates screened`, `Selected context` oraz `Primary behavioural driver`. Encje z jednostkami elektrycznymi (W/V/A/VA/Wh itd.) nie powinny pojawić się w Selected context; pozostałe encje mogą zostać wybrane, jeśli historia pokazuje ich wartość predykcyjną.
