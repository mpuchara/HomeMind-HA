# Obsługa 0.9.0

1. Przypisz encje/czujniki do obszarów w rejestrze HA. Kolejność mapowania: obszar encji, obszar urządzenia, jawny fallback entity_area_mapping, np. {"binary_sensor.stairs_motion":"stairs","light.stairs":"stairs"}. Nazwy encji nie służą do zgadywania pomieszczeń.
2. W Home Intelligence opcjonalnie uruchom bootstrap historii. Panel pokazuje postęp, ETA i anulowanie. Bootstrap nie steruje urządzeniami.
3. Wybierz agenta i uruchom Train. Resume kontynuuje od zapisanego punktu; Rebuild odtwarza model z archiwum. Ciężkie zadania są wykonywane pojedynczo. Po restarcie nic nie trenuje się samoczynnie.
4. Po kwalifikacji sprawdź Shadow. Porównuj Desired z zachowaniem, oceniaj confidence, support i novelty osobno. Próg benchmarku domyślnie wynosi 78%, z wymaganym minimalnym pokryciem próbek.
5. Włącz Control. Executor przejmuje rozpoznane automatyzacje i realizuje aktualny stan docelowy przez usługi HA. Niespełnione warunki są opisane przy intencji.
6. ACCEPTED oznacza przyjęcie wywołania usługi, a ACK zgodną zmianę stanu urządzenia. Timeout i retry nie są potwierdzeniem wykonania.
7. Ręczna zmiana nastawy uczy preferencji i uruchamia hold. Czas zależy od profilu/ustawienia. Potwierdzenie własnego wywołania nie jest ręczną korektą.

Dla światła na schodach sprawdzaj przede wszystkim ostatnią intencję, stan ACK oraz powód ewentualnej blokady. Nie trzeba zmieniać poprawnie dobranych sensorów, aby naprawić rozpoznawanie własnych poleceń.

Export inference pobiera parametry potrzebne do predykcji i objaśnienia. Pełny stan uczenia pozostaje w bazie. Ustawienia policy_half_life_days i home_model_half_life_days określają tempo wygaszania starej wiedzy (domyślnie 30 i 45 dni). Wersja 0.9 nie wykonuje losowych prób mikroeksploracji.

W Home Intelligence prawdopodobieństwo obecności, przewidywane przyjście/wyjście oraz pewność modelu domu są oddzielone od pewności polityki. Prognoza nie jest gwarancją wejścia osoby do pomieszczenia.
