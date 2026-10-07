# Adaptive AI 0.14.144

Eksport agenta łazienki ujawnił schemat z kanałami radaru kuchennego i głównym czujnikiem obecności z kuchni. Radar automatyzacji nie miał wpisu obszaru. Rebuild zachowywał pełny poprzedni schemat, a nowe kanały trafiały poza limit. Osobny proces treningowy nie instalował selektora automatyzacji używanego w Live.

Ta wersja przekazuje procesowi treningowemu aktualny opis automatyzacji oraz instaluje ten sam selektor. Rebuild z pustymi wagami rezerwuje czujnik automatyzacji i jego kanały przed skorelowanymi sensorami. Przy braku wpisu urządzenia rozpoznaje dokładną rodzinę nazw kanałów radaru; znany konflikt obszaru nadal blokuje dopasowanie. Ręczna lista wejść pozostaje respektowana. Dodatkowe sensory nadal mogą być oceniane w turnieju na przyszłych danych.

Migracja schematu zachowuje wersję znaczenia cech i przenosi również klasyfikator pożądanego stanu, jego ograniczony zbiór próbek i kary negatywnych korekt. Eksport diagnostyczny pokazuje wersję aplikacji, kontrakt cech i gotowość klasyfikatora.

## Instalacja i sprawdzenie

Zainstaluj wersję 0.14.144, uruchom ponownie dodatek i wykonaj pełny Rebuild agenta. Pozostaw tryb Shadow podczas testu. Sprawdź wybrane wejścia: powinny zaczynać się od radaru automatyzacji i jego kanałów, również gdy nie mają obszaru w HA. Dla aktualnej rodziny ESPEN4 są to energia nieruchoma/ruchoma i odległość celu nieruchomego/ruchomego.

Nie przepisujemy wag istniejącego modelu w miejscu. Aktualizacja wymaga Rebuild, żeby zmienić stary dobór wejść. Wspólna prognoza obecności nadal wymaga poprawnego mapowania obszarów HA; dopasowanie nazw kanałów nie przypisuje im obszaru.

Regresja integracyjna odtwarza pełny stary schemat z obcym radarem, wildcard i brak obszaru właściwego radaru. Sprawdza trwały model po rzeczywistym treningu w osobnym procesie oraz wejście, nieruchomy pobyt i wyjście. To test syntetyczny struktury ujawnionej przez eksport. Załączony eksport obejmuje historyczne korekty, nie cały przebieg nowego wykresu, więc nie potwierdza usunięcia każdego spadku OFF w domu.
