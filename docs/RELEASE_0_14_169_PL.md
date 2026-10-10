# HomeMind Adaptive AI 0.14.169

Próg w **Explore → Dodatkowa jasność** można edytować. Po wyborze czujnika domyślna wartość pochodzi z istniejącej automatyzacji lub wytrenowanego agenta używającego dokładnie tej samej encji jasności. Nie ma już arbitralnego progu 40 dla czujnika bez znanej konfiguracji.

## Wybór i edycja

1. Zainstaluj aktualizację i uruchom dodatek ponownie.
2. Otwórz **Explore → Dodatkowa jasność**, wybierz czujnik i cel pomijania ON przy wystarczającej jasności.
3. Formularz podstawi próg i pokaże jego źródło. W razie różnych wartości możesz wybrać inne źródło z listy. Pole progu pozostaje edytowalne.
4. Ustawienia zostaną zapisane w nowym Candidate. Trening, przyszłe porównanie w Shadow i warunki Promote pozostają takie jak w 0.14.168. Dotychczasowy Live nie jest zmieniany samym otwarciem lub edycją formularza.

Pierwszeństwo ma zapisany próg wybranej generacji, potem aktualna automatyzacja danego urządzenia, następnie preferencja innego wytrenowanego Live agenta, a potem automatyzacje innych urządzeń. Ostatnio odczytane oraz wyłączone automatyzacje są źródłami zapasowymi i mają opis pochodzenia. Ręczne zmiany nie są zastępowane podpowiedzią; przełączenie czujnika i powrót zachowuje roboczą edycję.

Z automatyzacji kopiowana jest granica warunku liczbowego **poniżej progu** dla stanu czujnika. Domyślna histereza takiego odniesienia wynosi zero, aby nie przesunąć granicy. Jeśli kilka warunków AND ogranicza ten sam czujnik, używana jest najniższa granica. Z konfiguracji agenta kopiowane są także jego histereza i maksymalny wiek pomiaru; wszystkie te pola można zmienić.

Próg będący encją pomocniczą, np. `input_number`, jest rozwiązywany do odczytanej wartości. Jest to kopia na czas tworzenia Candidate: późniejsza zmiana źródła nie aktualizuje automatycznie preferencji. API Explore również potrafi użyć znalezionego odniesienia przy pominiętym polu progu; jawnie podana wartość ma pierwszeństwo.

## Ograniczenia i walidacja

Nie są przenoszone wartości z innej encji pomiarowej ani zapisanej preferencji o innej jednostce. Nie tłumaczymy OR/NOT, szablonów, pomiarów atrybutów ani warunków wyłącznie powyżej progu na prostą preferencję unikania jasnego ON. Gdy nie ma odpowiedniego źródła, wpisz próg ręcznie.

To podpowiedź z już używanej konfiguracji, a nie automatyczne uczenie optymalnego progu. Czujnik kuchenny nadal musi reprezentować jasność na schodach, a przyszłe wyniki w Shadow sprawdzają nową preferencję. Preferencja nie wymusza wcześniejszego OFF podczas pobytu; świeżość, ręczne polecenia i warunki Control pozostają bez zmian.

Dodano 13 regresji obejmujących źródła, priorytety, jednostki, granice i strukturę warunków, zapis Candidate oraz rzeczywisty kod formularza JavaScript. Test formularza sprawdza podpowiedź, ręczną edycję, wybór alternatywy, powrót do wcześniej edytowanego czujnika i wysłaną konfigurację. Pełny zestaw obejmuje 2022 testy. Wyszukiwanie źródeł działa przy otwieraniu lub wysyłaniu Explore i nie dodaje skanowania do ścieżki decyzji agenta.
