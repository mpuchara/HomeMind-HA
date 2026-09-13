# Adaptive AI 0.9.2 — obsługa

Po aktualizacji zachowaj dane aplikacji. Migracja archiwizuje niezgodne modele i ustawia NEEDS_RETRAIN/paused. Archiwum sensorów, feedback, agenci i ustawienia pozostają zachowane. Restart nie uruchamia automatycznego odtwarzania historii.

Przypisz obszary encjom i urządzeniom w HA. Jeśli rejestr nie zawiera obszaru, entity_area_mapping może podać jawny fallback jako JSON, np. {"binary_sensor.motion":"stairs","light.stairs":"stairs"}. Model nie zgaduje pokoi po nazwach encji.

Home Intelligence pokazuje wspólne prognozy obecności i przejścia. Bootstrap historii uruchom ręcznie; można go anulować. Train/Resume/Rebuild agentów i bootstrap współdzielą jedno ciężkie zadanie, aby ograniczać pamięć.

Wybierz agenta, wykonaj Train i sprawdź wynik kwalifikacji. Shadow pokazuje stan docelowy, confidence, support, novelty i intencję bez wywoływania usług HA. Po ocenie uruchom Control.

Control wyłącza wykryte automatyzacje sterujące tym samym celem i sprawdza ich stan. Dynamiczne skrypty i zewnętrzne kontrolery mogą wymagać osobnego wyłączenia. Po opuszczeniu Control automatyzacje nie są przywracane automatycznie.

ACCEPTED oznacza przyjęcie usługi, ACK potwierdzenie zgodnego stanu. Executor sprawdza aktualność intencji, dostępność, właściciela celu, kwalifikację, confidence/support/novelty, ręczny hold oraz ograniczenia czasu urządzenia. Własne ACK nie jest ręczną zmianą użytkownika.

Ręczna korekta ma silną ujemną nagrodę i czasowo przejmuje sterowanie. Samo ACK nie daje nagrody. Antycypacja zyskuje premię po obserwowanym przyjściu; brak przyjścia może ją ukarać po pełnym oknie przy działających sensorach.

Wiedza wygasa domyślnie o połowę w 30 dni (polityka) lub 45 dni (dom). Te wartości zmienisz w konfiguracji. Eksport inferencji zawiera mały stan potrzebny do predykcji; pełny stan uczenia pozostaje w bazie. Mikroeksploracja jest wyłączona w 0.9.

Aktualizacja z ZIP wymaga podmiany całego katalogu źródeł istniejącej lokalnej aplikacji i Rebuild, nie samego restartu. Nowa lokalna aplikacja nie przejmuje automatycznie danych wersji z repozytorium. Wykonaj kopię przed podmianą.
